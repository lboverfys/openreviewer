"""SQLAlchemy queries for the operational dashboard read model."""

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ExecutionStatus, WorkerStatus
from persistence.models import (
    ReviewRunRecord,
    ReviewTaskRecord,
    WorkerHeartbeatRecord,
)
from services.dashboard import (
    DashboardData,
    DashboardPersistenceError,
    ReviewListItem,
    StoredWorkerHeartbeat,
    as_utc,
)


class SqlAlchemyDashboardRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def load(self, limit: int) -> DashboardData:
        with self._sessions() as session:
            try:
                total_reviews = int(
                    session.scalar(
                        select(func.count()).select_from(ReviewRunRecord)
                    )
                    or 0
                )
                grouped_counts = session.execute(
                    select(
                        ReviewRunRecord.execution_status,
                        func.count(ReviewRunRecord.id),
                    ).group_by(ReviewRunRecord.execution_status)
                )
                status_counts = {
                    ExecutionStatus(status): int(count)
                    for status, count in grouped_counts
                }

                rows = session.execute(
                    select(ReviewRunRecord, ReviewTaskRecord)
                    .join(
                        ReviewTaskRecord,
                        ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
                    )
                    .order_by(ReviewRunRecord.created_at.desc())
                    .limit(limit)
                )
                reviews = tuple(
                    ReviewListItem(
                        review_run_id=run.id,
                        review_task_id=task.id,
                        repository=run.repository,
                        pull_request_number=run.pull_request_number,
                        head_sha=run.head_sha,
                        execution_status=ExecutionStatus(run.execution_status),
                        attempt_count=task.attempt_count,
                        max_attempts=task.max_attempts,
                        last_error=task.last_error,
                        created_at=as_utc(run.created_at),
                        updated_at=as_utc(run.updated_at),
                    )
                    for run, task in rows
                )

                heartbeat = session.scalar(
                    select(WorkerHeartbeatRecord)
                    .order_by(WorkerHeartbeatRecord.last_seen_at.desc())
                    .limit(1)
                )
                stored_worker = (
                    StoredWorkerHeartbeat(
                        worker_id=heartbeat.worker_id,
                        status=WorkerStatus(heartbeat.status),
                        current_task_id=heartbeat.current_task_id,
                        started_at=as_utc(heartbeat.started_at),
                        last_seen_at=as_utc(heartbeat.last_seen_at),
                    )
                    if heartbeat is not None
                    else None
                )
                return DashboardData(
                    total_reviews=total_reviews,
                    status_counts=status_counts,
                    recent_reviews=reviews,
                    latest_worker=stored_worker,
                )
            except (SQLAlchemyError, ValueError) as exc:
                raise DashboardPersistenceError(
                    "dashboard data could not be loaded"
                ) from exc
