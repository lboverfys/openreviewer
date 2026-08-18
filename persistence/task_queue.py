"""PostgreSQL-backed task leasing, recovery, retry and heartbeat storage."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ExecutionStatus, WorkerStatus
from persistence.models import (
    OutboxEventRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
    WorkerHeartbeatRecord,
)
from services.task_queue import (
    ReviewTaskLease,
    TaskLeaseLostError,
    TaskQueueError,
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class SqlAlchemyReviewTaskQueue:
    """Durable queue that uses row locks instead of an in-memory broker."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], UUID] | None = None,
        retry_base_seconds: int = 5,
        retry_cap_seconds: int = 300,
    ) -> None:
        if retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be positive")
        if retry_cap_seconds < retry_base_seconds:
            raise ValueError("retry_cap_seconds must not be less than the base")
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory or uuid4
        self._retry_base_seconds = retry_base_seconds
        self._retry_cap_seconds = retry_cap_seconds

    def record_heartbeat(
        self,
        worker_id: str,
        worker_status: WorkerStatus,
        current_task_id: str | None = None,
    ) -> None:
        now = self._clock()
        with self._sessions() as session:
            try:
                heartbeat = session.get(WorkerHeartbeatRecord, worker_id)
                if heartbeat is None:
                    heartbeat = WorkerHeartbeatRecord(
                        worker_id=worker_id,
                        status=worker_status.value,
                        current_task_id=current_task_id,
                        started_at=now,
                        last_seen_at=now,
                    )
                    session.add(heartbeat)
                else:
                    heartbeat.status = worker_status.value
                    heartbeat.current_task_id = current_task_id
                    heartbeat.last_seen_at = now
                session.commit()
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("worker heartbeat could not be persisted") from exc

    def recover_expired_leases(self) -> int:
        now = self._clock()
        with self._sessions() as session:
            try:
                statement = (
                    select(ReviewTaskRecord)
                    .where(
                        ReviewTaskRecord.execution_status
                        == ExecutionStatus.RUNNING.value,
                        ReviewTaskRecord.lease_expires_at.is_not(None),
                        ReviewTaskRecord.lease_expires_at <= now,
                    )
                    .order_by(ReviewTaskRecord.lease_expires_at.asc())
                    .with_for_update(skip_locked=True)
                )
                expired_tasks = list(session.scalars(statement))
                for task in expired_tasks:
                    self._reschedule_or_fail(
                        session,
                        task,
                        now,
                        "Worker 租约超时，任务已进入恢复流程",
                        event_suffix="lease-expired",
                    )
                session.commit()
                return len(expired_tasks)
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("expired task leases could not be recovered") from exc

    def claim_next(
        self,
        worker_id: str,
        lease_duration: timedelta,
    ) -> ReviewTaskLease | None:
        if lease_duration.total_seconds() <= 0:
            raise ValueError("lease_duration must be positive")
        now = self._clock()
        lease_expires_at = now + lease_duration
        with self._sessions() as session:
            try:
                statement = (
                    select(ReviewTaskRecord)
                    .where(
                        ReviewTaskRecord.execution_status
                        == ExecutionStatus.QUEUED.value,
                        ReviewTaskRecord.available_at <= now,
                    )
                    .order_by(
                        ReviewTaskRecord.priority.desc(),
                        ReviewTaskRecord.available_at.asc(),
                        ReviewTaskRecord.created_at.asc(),
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                task = session.scalar(statement)
                if task is None:
                    session.commit()
                    return None

                run = session.get(ReviewRunRecord, task.review_run_id)
                if run is None:
                    raise TaskQueueError("review task points to a missing review run")

                task.execution_status = ExecutionStatus.RUNNING.value
                task.attempt_count += 1
                task.lease_owner = worker_id
                task.lease_expires_at = lease_expires_at
                task.last_error = None
                task.updated_at = now
                run.execution_status = ExecutionStatus.RUNNING.value
                run.updated_at = now
                self._add_event(
                    session,
                    task,
                    "review.task.running",
                    f"running:{task.attempt_count}",
                    now,
                )
                session.commit()
                return ReviewTaskLease(
                    task_id=task.id,
                    review_run_id=task.review_run_id,
                    worker_id=worker_id,
                    attempt_count=task.attempt_count,
                    lease_expires_at=lease_expires_at,
                )
            except TaskQueueError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("the next review task could not be claimed") from exc

    def renew_lease(
        self,
        lease: ReviewTaskLease,
        lease_duration: timedelta,
    ) -> ReviewTaskLease:
        if lease_duration.total_seconds() <= 0:
            raise ValueError("lease_duration must be positive")
        now = self._clock()
        renewed_until = now + lease_duration
        with self._sessions() as session:
            try:
                task = self._locked_owned_task(session, lease, now)
                task.lease_expires_at = renewed_until
                task.updated_at = now
                session.commit()
                return ReviewTaskLease(
                    task_id=lease.task_id,
                    review_run_id=lease.review_run_id,
                    worker_id=lease.worker_id,
                    attempt_count=lease.attempt_count,
                    lease_expires_at=renewed_until,
                )
            except TaskLeaseLostError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("the review task lease could not be renewed") from exc

    def mark_waiting_for_ci(self, lease: ReviewTaskLease) -> None:
        now = self._clock()
        with self._sessions() as session:
            try:
                task = self._locked_owned_task(session, lease, now)
                run = session.get(ReviewRunRecord, task.review_run_id)
                if run is None:
                    raise TaskQueueError("review task points to a missing review run")

                task.execution_status = ExecutionStatus.WAITING_FOR_CI.value
                task.lease_owner = None
                task.lease_expires_at = None
                task.updated_at = now
                run.execution_status = ExecutionStatus.WAITING_FOR_CI.value
                run.updated_at = now
                self._add_event(
                    session,
                    task,
                    "review.waiting_for_ci",
                    f"waiting-for-ci:{task.attempt_count}",
                    now,
                )
                session.commit()
            except (TaskLeaseLostError, TaskQueueError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError(
                    "the review task could not enter the CI waiting state"
                ) from exc

    def retry_or_fail(self, lease: ReviewTaskLease, error: str) -> None:
        now = self._clock()
        safe_error = error.strip()[:4000] or "Worker 处理任务时发生未知错误"
        with self._sessions() as session:
            try:
                task = self._locked_owned_task(session, lease, now)
                self._reschedule_or_fail(
                    session,
                    task,
                    now,
                    safe_error,
                    event_suffix=f"attempt-{task.attempt_count}",
                )
                session.commit()
            except (TaskLeaseLostError, TaskQueueError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("the failed review task could not be recorded") from exc

    def heartbeat_is_fresh(self, worker_id: str, max_age: timedelta) -> bool:
        now = self._clock()
        with self._sessions() as session:
            try:
                heartbeat = session.get(WorkerHeartbeatRecord, worker_id)
                if heartbeat is None:
                    return False
                return now - _as_utc(heartbeat.last_seen_at) <= max_age
            except SQLAlchemyError as exc:
                raise TaskQueueError("worker heartbeat could not be checked") from exc

    @staticmethod
    def _locked_owned_task(
        session: Session,
        lease: ReviewTaskLease,
        now: datetime,
    ) -> ReviewTaskRecord:
        statement = (
            select(ReviewTaskRecord)
            .where(
                ReviewTaskRecord.id == lease.task_id,
                ReviewTaskRecord.review_run_id == lease.review_run_id,
                ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
                ReviewTaskRecord.lease_owner == lease.worker_id,
                ReviewTaskRecord.attempt_count == lease.attempt_count,
                ReviewTaskRecord.lease_expires_at.is_not(None),
                ReviewTaskRecord.lease_expires_at > now,
            )
            .with_for_update()
        )
        task = session.scalar(statement)
        if task is None:
            raise TaskLeaseLostError("the worker no longer owns this review task")
        return task

    def _reschedule_or_fail(
        self,
        session: Session,
        task: ReviewTaskRecord,
        now: datetime,
        error: str,
        *,
        event_suffix: str,
    ) -> None:
        run = session.get(ReviewRunRecord, task.review_run_id)
        if run is None:
            raise TaskQueueError("review task points to a missing review run")

        task.last_error = error
        task.lease_owner = None
        task.lease_expires_at = None
        task.updated_at = now
        if task.attempt_count >= task.max_attempts:
            task.execution_status = ExecutionStatus.FAILED.value
            run.execution_status = ExecutionStatus.FAILED.value
            event_type = "review.task.failed"
            event_key = f"failed:{event_suffix}"
        else:
            task.execution_status = ExecutionStatus.QUEUED.value
            run.execution_status = ExecutionStatus.QUEUED.value
            task.available_at = now + self._retry_delay(task.attempt_count)
            event_type = "review.task.retry_scheduled"
            event_key = f"retry:{event_suffix}"
        run.updated_at = now
        self._add_event(session, task, event_type, event_key, now)

    def _retry_delay(self, attempt_count: int) -> timedelta:
        exponent = max(0, attempt_count - 1)
        seconds = min(
            self._retry_cap_seconds,
            self._retry_base_seconds * (2**exponent),
        )
        return timedelta(seconds=seconds)

    def _add_event(
        self,
        session: Session,
        task: ReviewTaskRecord,
        event_type: str,
        key_suffix: str,
        occurred_at: datetime,
    ) -> None:
        session.add(
            OutboxEventRecord(
                id=str(self._uuid_factory()),
                event_key=f"{event_type}:{task.id}:{key_suffix}",
                aggregate_type="review_run",
                aggregate_id=task.review_run_id,
                event_type=event_type,
                payload={
                    "review_run_id": task.review_run_id,
                    "review_task_id": task.id,
                    "attempt_count": task.attempt_count,
                },
                occurred_at=occurred_at,
                publish_attempts=0,
            )
        )
