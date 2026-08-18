"""SQLAlchemy implementation of the review persistence boundary."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import CoverageStatus, ExecutionStatus
from domain.models import ReviewRequest
from persistence.models import OutboxEventRecord, ReviewRunRecord, ReviewTaskRecord
from services.reviews import (
    IdempotencyConflictError,
    ReviewPersistenceError,
    ReviewSubmissionResult,
)


class SqlAlchemyReviewRepository:
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
        request: ReviewRequest,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> ReviewSubmissionResult:
        with self._sessions() as session:
            try:
                existing = self._find_existing(session, idempotency_key)
                if existing is not None:
                    return self._existing_result(existing, request_fingerprint)

                now = self._clock()
                review_run_id = str(self._uuid_factory())
                review_task_id = str(self._uuid_factory())
                outbox_event_id = str(self._uuid_factory())

                session.add_all(
                    [
                        ReviewRunRecord(
                            id=review_run_id,
                            review_version_key=request.review_version_key,
                            installation_id=request.installation_id,
                            repository_id=request.repository_id,
                            repository=request.repository,
                            pull_request_number=request.pull_request_number,
                            head_sha=request.head_sha,
                            execution_status=ExecutionStatus.QUEUED.value,
                            review_conclusion=None,
                            coverage_status=CoverageStatus.UNKNOWN.value,
                            idempotency_key=idempotency_key,
                            request_fingerprint=request_fingerprint,
                            created_at=now,
                            updated_at=now,
                        ),
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
                                "review_version_key": request.review_version_key,
                            },
                            occurred_at=now,
                            publish_attempts=0,
                        ),
                    ]
                )
                session.commit()
                return ReviewSubmissionResult(
                    review_run_id=review_run_id,
                    review_task_id=review_task_id,
                    review_version_key=request.review_version_key,
                    execution_status=ExecutionStatus.QUEUED,
                    accepted_at=now,
                    created=True,
                )
            except IntegrityError as exc:
                session.rollback()
                try:
                    existing = self._find_existing(session, idempotency_key)
                except SQLAlchemyError as lookup_exc:
                    raise ReviewPersistenceError(
                        "review request could not be reconciled after a conflict"
                    ) from lookup_exc
                if existing is None:
                    raise ReviewPersistenceError(
                        "review request violated a database constraint"
                    ) from exc
                return self._existing_result(existing, request_fingerprint)
            except IdempotencyConflictError:
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise ReviewPersistenceError(
                    "review request could not be persisted"
                ) from exc

    @staticmethod
    def _find_existing(
        session: Session,
        idempotency_key: str,
    ) -> tuple[ReviewRunRecord, ReviewTaskRecord] | None:
        statement = (
            select(ReviewRunRecord, ReviewTaskRecord)
            .join(
                ReviewTaskRecord,
                ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
            )
            .where(ReviewRunRecord.idempotency_key == idempotency_key)
        )
        row = session.execute(statement).one_or_none()
        if row is None:
            return None
        return row[0], row[1]

    @staticmethod
    def _existing_result(
        existing: tuple[ReviewRunRecord, ReviewTaskRecord],
        request_fingerprint: str,
    ) -> ReviewSubmissionResult:
        review_run, review_task = existing
        if review_run.request_fingerprint != request_fingerprint:
            raise IdempotencyConflictError(
                "idempotency key was already used for another request"
            )
        accepted_at = review_run.created_at
        if accepted_at.tzinfo is None:
            accepted_at = accepted_at.replace(tzinfo=UTC)
        return ReviewSubmissionResult(
            review_run_id=review_run.id,
            review_task_id=review_task.id,
            review_version_key=review_run.review_version_key,
            execution_status=ExecutionStatus(review_run.execution_status),
            accepted_at=accepted_at,
            created=False,
        )
