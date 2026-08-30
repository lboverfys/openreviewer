"""基于 SQLAlchemy 的运行就绪、Outbox 和保留期实现。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ExecutionStatus, WorkerStatus
from domain.security import redact_sensitive
from persistence.models import (
    AdminSessionRecord,
    FindingEvaluationRecord,
    GitHubWebhookDeliveryRecord,
    ModelHttpCallRecord,
    OutboxEventRecord,
    PullRequestVersionRecord,
    ReviewPlanRecord,
    ReviewQuotaBucketRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
    WorkerHeartbeatRecord,
)
from services.operations import (
    CleanupResult,
    MetricsSnapshot,
    OperationsError,
    OutboxEvent,
    ReadinessSnapshot,
    RetentionCutoffs,
)

TERMINAL_REVIEW_STATUSES = (
    ExecutionStatus.COMPLETED.value,
    ExecutionStatus.FAILED.value,
    ExecutionStatus.TIMED_OUT.value,
    ExecutionStatus.CANCELLED.value,
    ExecutionStatus.SUPERSEDED.value,
    ExecutionStatus.REJECTED.value,
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _affected_rows(result: object) -> int:
    if not isinstance(result, CursorResult):
        raise OperationsError("数据库写操作未返回影响行数")
    rowcount = result.rowcount
    return rowcount if rowcount is not None and rowcount > 0 else 0


class SqlAlchemyOperationsRepository:
    """所有查询都有固定次数或显式批次上限的运维仓储。"""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))

    def readiness(
        self,
        expected_revision: str,
        worker_max_age: timedelta,
    ) -> ReadinessSnapshot:
        now = self._clock()
        try:
            with self._sessions() as session:
                session.execute(select(1)).scalar_one()
        except SQLAlchemyError:
            return ReadinessSnapshot(database=False, migration=False, worker=False)

        try:
            with self._sessions() as session:
                revisions = set(
                    session.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalars()
                )
        except SQLAlchemyError:
            return ReadinessSnapshot(database=True, migration=False, worker=False)

        migration_ready = revisions == {expected_revision}
        try:
            with self._sessions() as session:
                worker_ready = (
                    session.scalar(
                        select(WorkerHeartbeatRecord.worker_id)
                        .where(
                            WorkerHeartbeatRecord.last_seen_at
                            >= now - worker_max_age,
                            WorkerHeartbeatRecord.status
                            != WorkerStatus.STOPPING.value,
                        )
                        .limit(1)
                    )
                    is not None
                )
        except SQLAlchemyError:
            worker_ready = False
        return ReadinessSnapshot(
            database=True,
            migration=migration_ready,
            worker=worker_ready,
        )

    def metrics(self, worker_max_age: timedelta) -> MetricsSnapshot:
        now = self._clock()
        try:
            with self._sessions() as session:
                review_counts: dict[str, int] = dict(
                    session.execute(
                        select(
                            ReviewRunRecord.execution_status,
                            func.count(ReviewRunRecord.id),
                        ).group_by(ReviewRunRecord.execution_status)
                    ).tuples().all()
                )
                task_counts: dict[str, int] = dict(
                    session.execute(
                        select(
                            ReviewTaskRecord.execution_status,
                            func.count(ReviewTaskRecord.id),
                        ).group_by(ReviewTaskRecord.execution_status)
                    ).tuples().all()
                )
                model_http_call_counts: dict[str, int] = dict(
                    session.execute(
                        select(
                            ModelHttpCallRecord.status,
                            func.count(ModelHttpCallRecord.id),
                        ).group_by(ModelHttpCallRecord.status)
                    ).tuples().all()
                )
                fresh_workers = int(
                    session.scalar(
                        select(func.count())
                        .select_from(WorkerHeartbeatRecord)
                        .where(
                            WorkerHeartbeatRecord.last_seen_at
                            >= now - worker_max_age,
                            WorkerHeartbeatRecord.status
                            != WorkerStatus.STOPPING.value,
                        )
                    )
                    or 0
                )
                pending_count, oldest_at, maximum_attempts = session.execute(
                    select(
                        func.count(OutboxEventRecord.id),
                        func.min(OutboxEventRecord.occurred_at),
                        func.max(OutboxEventRecord.publish_attempts),
                    ).where(OutboxEventRecord.published_at.is_(None))
                ).one()
                claimable_count, oldest_claimable_at = session.execute(
                    select(
                        func.count(ReviewTaskRecord.id),
                        func.min(ReviewTaskRecord.created_at),
                    ).where(
                        ReviewTaskRecord.execution_status.in_(
                            (
                                ExecutionStatus.QUEUED.value,
                                ExecutionStatus.WAITING_FOR_CI.value,
                                ExecutionStatus.READY_FOR_REVIEW.value,
                            )
                        ),
                        ReviewTaskRecord.available_at <= now,
                    )
                ).one()
        except SQLAlchemyError as exc:
            raise OperationsError("运维指标暂时不可用") from exc

        oldest_age = (
            max(0.0, (now - _as_utc(oldest_at)).total_seconds())
            if oldest_at is not None
            else 0.0
        )
        oldest_claimable_age = (
            max(0.0, (now - _as_utc(oldest_claimable_at)).total_seconds())
            if oldest_claimable_at is not None
            else 0.0
        )
        return MetricsSnapshot(
            review_counts=review_counts,
            task_counts=task_counts,
            model_http_call_counts=model_http_call_counts,
            fresh_workers=fresh_workers,
            pending_outbox_events=int(pending_count or 0),
            outbox_oldest_age_seconds=oldest_age,
            outbox_max_publish_attempts=int(maximum_attempts or 0),
            claimable_tasks=int(claimable_count or 0),
            oldest_claimable_task_age_seconds=oldest_claimable_age,
        )

    def claim_outbox(
        self,
        owner: str,
        *,
        batch_size: int,
        lease_duration: timedelta,
    ) -> tuple[OutboxEvent, ...]:
        if not owner or len(owner) > 200:
            raise ValueError("outbox owner must contain 1 to 200 characters")
        if not 1 <= batch_size <= 500:
            raise ValueError("outbox batch_size must be between 1 and 500")
        if lease_duration <= timedelta(0):
            raise ValueError("outbox lease_duration must be positive")

        now = self._clock()
        try:
            with self._sessions() as session, session.begin():
                rows = tuple(
                    session.scalars(
                        select(OutboxEventRecord)
                        .where(
                            OutboxEventRecord.published_at.is_(None),
                            OutboxEventRecord.next_publish_attempt_at <= now,
                            or_(
                                OutboxEventRecord.publish_lease_owner.is_(None),
                                OutboxEventRecord.publish_lease_expires_at <= now,
                            ),
                        )
                        .order_by(
                            OutboxEventRecord.occurred_at,
                            OutboxEventRecord.id,
                        )
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                )
                expires_at = now + lease_duration
                for row in rows:
                    row.publish_lease_owner = owner
                    row.publish_lease_expires_at = expires_at
                    row.publish_attempts += 1
                return tuple(
                    OutboxEvent(
                        id=row.id,
                        event_key=row.event_key,
                        aggregate_type=row.aggregate_type,
                        aggregate_id=row.aggregate_id,
                        event_type=row.event_type,
                        payload=dict(row.payload),
                        occurred_at=_as_utc(row.occurred_at),
                        publish_attempts=row.publish_attempts,
                    )
                    for row in rows
                )
        except SQLAlchemyError as exc:
            raise OperationsError("Outbox 事件领取失败") from exc

    def complete_outbox(self, owner: str, event_ids: tuple[str, ...]) -> int:
        if not event_ids:
            return 0
        now = self._clock()
        try:
            with self._sessions() as session, session.begin():
                result = session.execute(
                    update(OutboxEventRecord)
                    .where(
                        OutboxEventRecord.id.in_(event_ids),
                        OutboxEventRecord.published_at.is_(None),
                        OutboxEventRecord.publish_lease_owner == owner,
                    )
                    .values(
                        published_at=now,
                        publish_lease_owner=None,
                        publish_lease_expires_at=None,
                        last_publish_error=None,
                    )
                )
                return _affected_rows(result)
        except SQLAlchemyError as exc:
            raise OperationsError("Outbox 发布确认失败") from exc

    def release_outbox(
        self,
        owner: str,
        event_ids: tuple[str, ...],
        *,
        retry_delay: timedelta,
        error: str,
    ) -> int:
        if not event_ids:
            return 0
        if retry_delay <= timedelta(0):
            raise ValueError("outbox retry_delay must be positive")
        safe_error = redact_sensitive(error)
        if not isinstance(safe_error, str):
            safe_error = "publisher failed"
        now = self._clock()
        try:
            with self._sessions() as session, session.begin():
                result = session.execute(
                    update(OutboxEventRecord)
                    .where(
                        OutboxEventRecord.id.in_(event_ids),
                        OutboxEventRecord.published_at.is_(None),
                        OutboxEventRecord.publish_lease_owner == owner,
                    )
                    .values(
                        publish_lease_owner=None,
                        publish_lease_expires_at=None,
                        next_publish_attempt_at=now + retry_delay,
                        last_publish_error=safe_error[:1000],
                    )
                )
                return _affected_rows(result)
        except SQLAlchemyError as exc:
            raise OperationsError("Outbox 发布失败状态保存失败") from exc

    def cleanup(
        self,
        cutoffs: RetentionCutoffs,
        *,
        batch_size: int,
    ) -> CleanupResult:
        if not 1 <= batch_size <= 2000:
            raise ValueError("cleanup batch_size must be between 1 and 2000")
        try:
            with self._sessions() as session, session.begin():
                published_outbox_events = self._delete_published_outbox(
                    session,
                    cutoffs.published_outbox,
                    batch_size,
                )
                admin_sessions = self._delete_admin_sessions(
                    session,
                    cutoffs.admin_sessions,
                    batch_size,
                )
                worker_heartbeats = self._delete_worker_heartbeats(
                    session,
                    cutoffs.worker_heartbeats,
                    batch_size,
                )
                webhook_deliveries = self._delete_webhooks(
                    session,
                    cutoffs.webhooks,
                    batch_size,
                )
                review_runs = self._delete_review_runs(
                    session,
                    cutoffs.reviews,
                    batch_size,
                )
                pull_request_versions = self._delete_pull_request_versions(
                    session,
                    cutoffs.reviews,
                    batch_size,
                )
                quota_buckets = (
                    self._delete_quota_buckets(
                        session,
                        cutoffs.quota_buckets,
                        batch_size,
                    )
                    if cutoffs.quota_buckets is not None
                    else 0
                )
                finding_evaluations = (
                    self._delete_finding_evaluations(
                        session,
                        cutoffs.finding_evaluations,
                        batch_size,
                    )
                    if cutoffs.finding_evaluations is not None
                    else 0
                )
        except SQLAlchemyError as exc:
            raise OperationsError("保留期清理失败") from exc
        return CleanupResult(
            published_outbox_events=published_outbox_events,
            admin_sessions=admin_sessions,
            worker_heartbeats=worker_heartbeats,
            webhook_deliveries=webhook_deliveries,
            review_runs=review_runs,
            pull_request_versions=pull_request_versions,
            quota_buckets=quota_buckets,
            finding_evaluations=finding_evaluations,
        )

    @staticmethod
    def _delete_finding_evaluations(
        session: Session,
        cutoff: datetime,
        batch_size: int,
    ) -> int:
        """按裁决时间分批清理过期评测快照。"""

        ids = session.scalars(
            select(FindingEvaluationRecord.finding_id)
            .where(FindingEvaluationRecord.adjudicated_at < cutoff)
            .order_by(
                FindingEvaluationRecord.adjudicated_at,
                FindingEvaluationRecord.finding_id,
            )
            .limit(batch_size)
        ).all()
        if not ids:
            return 0
        # 删除时再次带上时间条件；若并行裁决刚更新了同一行，不能把新样本
        # 当作旧 ID 一并删除。
        result = session.execute(
            delete(FindingEvaluationRecord).where(
                FindingEvaluationRecord.finding_id.in_(ids),
                FindingEvaluationRecord.adjudicated_at < cutoff,
            )
        )
        return _affected_rows(result)

    @staticmethod
    def _delete_quota_buckets(
        session: Session,
        cutoff: datetime,
        batch_size: int,
    ) -> int:
        ids = session.scalars(
            select(ReviewQuotaBucketRecord.id)
            .where(ReviewQuotaBucketRecord.window_start < cutoff)
            .order_by(
                ReviewQuotaBucketRecord.window_start,
                ReviewQuotaBucketRecord.id,
            )
            .limit(batch_size)
        ).all()
        if not ids:
            return 0
        result = session.execute(
            delete(ReviewQuotaBucketRecord).where(
                ReviewQuotaBucketRecord.id.in_(ids)
            )
        )
        return _affected_rows(result)

    @staticmethod
    def _delete_published_outbox(
        session: Session,
        cutoff: datetime,
        batch_size: int,
    ) -> int:
        ids = (
            select(OutboxEventRecord.id)
            .where(
                OutboxEventRecord.published_at.is_not(None),
                OutboxEventRecord.published_at < cutoff,
            )
            .order_by(OutboxEventRecord.published_at, OutboxEventRecord.id)
            .limit(batch_size)
        )
        result = session.execute(
            delete(OutboxEventRecord).where(OutboxEventRecord.id.in_(ids))
        )
        return _affected_rows(result)

    @staticmethod
    def _delete_admin_sessions(
        session: Session,
        cutoff: datetime,
        batch_size: int,
    ) -> int:
        ids = (
            select(AdminSessionRecord.session_hash)
            .where(AdminSessionRecord.expires_at < cutoff)
            .order_by(AdminSessionRecord.expires_at, AdminSessionRecord.session_hash)
            .limit(batch_size)
        )
        result = session.execute(
            delete(AdminSessionRecord).where(AdminSessionRecord.session_hash.in_(ids))
        )
        return _affected_rows(result)

    @staticmethod
    def _delete_worker_heartbeats(
        session: Session,
        cutoff: datetime,
        batch_size: int,
    ) -> int:
        ids = (
            select(WorkerHeartbeatRecord.worker_id)
            .where(WorkerHeartbeatRecord.last_seen_at < cutoff)
            .order_by(
                WorkerHeartbeatRecord.last_seen_at,
                WorkerHeartbeatRecord.worker_id,
            )
            .limit(batch_size)
        )
        result = session.execute(
            delete(WorkerHeartbeatRecord).where(WorkerHeartbeatRecord.worker_id.in_(ids))
        )
        return _affected_rows(result)

    @staticmethod
    def _delete_webhooks(
        session: Session,
        cutoff: datetime,
        batch_size: int,
    ) -> int:
        ids = (
            select(GitHubWebhookDeliveryRecord.delivery_id)
            .where(GitHubWebhookDeliveryRecord.received_at < cutoff)
            .order_by(
                GitHubWebhookDeliveryRecord.received_at,
                GitHubWebhookDeliveryRecord.delivery_id,
            )
            .limit(batch_size)
        )
        result = session.execute(
            delete(GitHubWebhookDeliveryRecord).where(
                GitHubWebhookDeliveryRecord.delivery_id.in_(ids)
            )
        )
        return _affected_rows(result)

    @staticmethod
    def _delete_review_runs(
        session: Session,
        cutoff: datetime,
        batch_size: int,
    ) -> int:
        pending_outbox = exists().where(
            OutboxEventRecord.aggregate_type == "review_run",
            OutboxEventRecord.aggregate_id == ReviewRunRecord.id,
            OutboxEventRecord.published_at.is_(None),
        )
        retained_webhook = exists().where(
            GitHubWebhookDeliveryRecord.review_run_id == ReviewRunRecord.id
        )
        ids = (
            select(ReviewRunRecord.id)
            .where(
                ReviewRunRecord.execution_status.in_(TERMINAL_REVIEW_STATUSES),
                ReviewRunRecord.workflow_status.in_(TERMINAL_REVIEW_STATUSES),
                ReviewRunRecord.created_at < cutoff,
                ~pending_outbox,
                ~retained_webhook,
            )
            .order_by(ReviewRunRecord.created_at, ReviewRunRecord.id)
            .limit(batch_size)
        )
        result = session.execute(
            delete(ReviewRunRecord).where(ReviewRunRecord.id.in_(ids))
        )
        return _affected_rows(result)

    @staticmethod
    def _delete_pull_request_versions(
        session: Session,
        cutoff: datetime,
        batch_size: int,
    ) -> int:
        active_run = exists().where(
            ReviewRunRecord.review_version_key
            == PullRequestVersionRecord.review_version_key
        )
        plan = exists().where(
            ReviewPlanRecord.pull_request_version_id == PullRequestVersionRecord.id
        )
        webhook = exists().where(
            GitHubWebhookDeliveryRecord.pull_request_version_id
            == PullRequestVersionRecord.id
        )
        ids = (
            select(PullRequestVersionRecord.id)
            .where(
                PullRequestVersionRecord.last_seen_at < cutoff,
                ~active_run,
                ~plan,
                ~webhook,
            )
            .order_by(
                PullRequestVersionRecord.last_seen_at,
                PullRequestVersionRecord.id,
            )
            .limit(batch_size)
        )
        result = session.execute(
            delete(PullRequestVersionRecord).where(
                PullRequestVersionRecord.id.in_(ids)
            )
        )
        return _affected_rows(result)
