"""已接受 GitHub Webhook 投递的原子 SQLAlchemy 持久化实现。"""

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import CoverageStatus, ExecutionStatus
from domain.models import PullRequestWebhook
from domain.security import ErrorCode, SafeError
from persistence.models import (
    GitHubInstallationRecord,
    GitHubWebhookDeliveryRecord,
    OutboxEventRecord,
    PullRequestVersionRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.repository_policy import repository_policy_snapshot
from services.webhooks import (
    WebhookDeliveryConflictError,
    WebhookPersistenceError,
    WebhookSubmissionResult,
)


class SqlAlchemyGitHubWebhookRepository:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory or uuid4

    def create_or_get(
        self,
        event: PullRequestWebhook,
        payload_sha256: str,
    ) -> WebhookSubmissionResult:
        with self._sessions() as session:
            try:
                existing = self._find_existing(session, event.delivery_id)
                if existing is not None:
                    return self._existing_result(
                        event.delivery_id, existing, payload_sha256
                    )

                now = self._clock()
                policy = repository_policy_snapshot(session, event.repository)
                review_run_id = str(self._uuid_factory())
                review_task_id = str(self._uuid_factory())
                outbox_event_id = str(self._uuid_factory())
                version_id = str(
                    uuid5(NAMESPACE_URL, f"openreviewer:{event.review_version_key}")
                )
                self._upsert_installation(session, event.installation_id, now)
                self._upsert_version(session, version_id, event, now)

                session.add(
                    ReviewRunRecord(
                        id=review_run_id,
                        review_version_key=event.review_version_key,
                        installation_id=event.installation_id,
                        repository_id=event.repository_id,
                        repository=event.repository,
                        repository_policy=policy,
                        pull_request_number=event.pull_request_number,
                        head_sha=event.head_sha,
                        execution_status=ExecutionStatus.QUEUED.value,
                        review_conclusion=None,
                        coverage_status=CoverageStatus.UNKNOWN.value,
                        idempotency_key=self._idempotency_key(event.delivery_id),
                        request_fingerprint=payload_sha256,
                        created_at=now,
                        updated_at=now,
                    )
                )
                # 这些模型没有 ORM relationship，显式刷新才能保证外键父记录先落库。
                session.flush()
                session.add_all(
                    [
                        ReviewTaskRecord(
                            id=review_task_id,
                            review_run_id=review_run_id,
                            execution_status=ExecutionStatus.QUEUED.value,
                            priority=100,
                            attempt_count=0,
                            max_attempts=3,
                            available_at=now,
                            created_at=now,
                            updated_at=now,
                        ),
                        OutboxEventRecord(
                            id=outbox_event_id,
                            event_key=f"review.requested:{review_run_id}",
                            aggregate_type="review_run",
                            aggregate_id=review_run_id,
                            event_type="review.requested",
                            payload={
                                "review_run_id": review_run_id,
                                "review_task_id": review_task_id,
                                "review_version_key": event.review_version_key,
                                "source": "github_webhook",
                            },
                            occurred_at=now,
                            publish_attempts=0,
                        ),
                    ]
                )
                session.flush()
                session.add(
                    GitHubWebhookDeliveryRecord(
                        delivery_id=event.delivery_id,
                        event_type=event.event_type,
                        action=event.action.value,
                        payload_sha256=payload_sha256,
                        installation_id=event.installation_id,
                        pull_request_version_id=version_id,
                        review_run_id=review_run_id,
                        review_task_id=review_task_id,
                        received_at=now,
                    )
                )
                session.commit()
                return WebhookSubmissionResult(
                    delivery_id=event.delivery_id,
                    review_run_id=review_run_id,
                    review_task_id=review_task_id,
                    review_version_key=event.review_version_key,
                    execution_status=ExecutionStatus.QUEUED,
                    accepted_at=now,
                    created=True,
                )
            except IntegrityError as exc:
                session.rollback()
                try:
                    existing = self._find_existing(session, event.delivery_id)
                except SQLAlchemyError as lookup_exc:
                    raise self._persistence_error() from lookup_exc
                if existing is None:
                    raise self._persistence_error() from exc
                return self._existing_result(
                    event.delivery_id, existing, payload_sha256
                )
            except WebhookDeliveryConflictError:
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise self._persistence_error() from exc

    @staticmethod
    def _find_existing(
        session: Session,
        delivery_id: str,
    ) -> tuple[str, str, str, str, str, datetime] | None:
        statement = (
            select(
                GitHubWebhookDeliveryRecord.payload_sha256,
                GitHubWebhookDeliveryRecord.review_run_id,
                GitHubWebhookDeliveryRecord.review_task_id,
                PullRequestVersionRecord.review_version_key,
                ReviewRunRecord.execution_status,
                GitHubWebhookDeliveryRecord.received_at,
            )
            .join(
                PullRequestVersionRecord,
                PullRequestVersionRecord.id
                == GitHubWebhookDeliveryRecord.pull_request_version_id,
            )
            .join(
                ReviewRunRecord,
                ReviewRunRecord.id == GitHubWebhookDeliveryRecord.review_run_id,
            )
            .where(GitHubWebhookDeliveryRecord.delivery_id == delivery_id)
        )
        row = session.execute(statement).one_or_none()
        return tuple(row) if row is not None else None

    @staticmethod
    def _existing_result(
        delivery_id: str,
        existing: tuple[str, str, str, str, str, datetime],
        payload_sha256: str,
    ) -> WebhookSubmissionResult:
        (
            stored_hash,
            review_run_id,
            review_task_id,
            review_version_key,
            execution_status,
            received_at,
        ) = existing
        if stored_hash != payload_sha256:
            raise WebhookDeliveryConflictError(
                SafeError(
                    code=ErrorCode.WEBHOOK_DELIVERY_CONFLICT,
                    safe_message="GitHub delivery ID 已被不同请求体使用",
                    retryable=False,
                )
            )
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=UTC)
        return WebhookSubmissionResult(
            delivery_id=delivery_id,
            review_run_id=review_run_id,
            review_task_id=review_task_id,
            review_version_key=review_version_key,
            execution_status=ExecutionStatus(execution_status),
            accepted_at=received_at,
            created=False,
        )

    @staticmethod
    def _idempotency_key(delivery_id: str) -> str:
        digest = sha256(delivery_id.encode("utf-8")).hexdigest()
        return f"github-delivery:{digest}"

    @staticmethod
    def _upsert_installation(
        session: Session,
        installation_id: int,
        now: datetime,
    ) -> None:
        values = {"id": installation_id, "created_at": now, "last_seen_at": now}
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = postgresql_insert(GitHubInstallationRecord).values(**values)
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[GitHubInstallationRecord.id],
                    set_={"last_seen_at": now},
                )
            )
            return
        elif dialect == "sqlite":
            sqlite_statement = sqlite_insert(GitHubInstallationRecord).values(**values)
            session.execute(
                sqlite_statement.on_conflict_do_update(
                    index_elements=[GitHubInstallationRecord.id],
                    set_={"last_seen_at": now},
                )
            )
            return
        else:
            existing = session.get(GitHubInstallationRecord, installation_id)
            if existing is None:
                session.add(GitHubInstallationRecord(**values))
            else:
                existing.last_seen_at = now
            return

    @staticmethod
    def _upsert_version(
        session: Session,
        version_id: str,
        event: PullRequestWebhook,
        now: datetime,
    ) -> None:
        values = {
            "id": version_id,
            "review_version_key": event.review_version_key,
            "installation_id": event.installation_id,
            "repository_id": event.repository_id,
            "repository": event.repository,
            "pull_request_number": event.pull_request_number,
            "head_sha": event.head_sha,
            "first_seen_at": now,
            "last_seen_at": now,
        }
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = postgresql_insert(PullRequestVersionRecord).values(**values)
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[PullRequestVersionRecord.review_version_key],
                    set_={"last_seen_at": now},
                )
            )
            return
        elif dialect == "sqlite":
            sqlite_statement = sqlite_insert(PullRequestVersionRecord).values(**values)
            session.execute(
                sqlite_statement.on_conflict_do_update(
                    index_elements=[PullRequestVersionRecord.review_version_key],
                    set_={"last_seen_at": now},
                )
            )
            return
        else:
            existing = session.scalar(
                select(PullRequestVersionRecord).where(
                    PullRequestVersionRecord.review_version_key
                    == event.review_version_key
                )
            )
            if existing is None:
                session.add(PullRequestVersionRecord(**values))
            else:
                existing.last_seen_at = now
            return

    @staticmethod
    def _persistence_error() -> WebhookPersistenceError:
        return WebhookPersistenceError(
            SafeError(
                code=ErrorCode.WEBHOOK_PERSISTENCE_UNAVAILABLE,
                safe_message="Webhook 投递暂时无法持久化",
                retryable=True,
            )
        )
