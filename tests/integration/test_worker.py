from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from apps.worker.main import WorkerRuntime, WorkerSettings
from domain.enums import ExecutionStatus, WorkerStatus
from domain.models import ReviewRequest
from persistence.database import Database
from persistence.models import (
    Base,
    OutboxEventRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
    WorkerHeartbeatRecord,
)
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from services.reviews import ReviewService
from services.task_queue import TaskLeaseLostError


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


TEST_TASK_AVAILABLE_AT = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path):
    path = (tmp_path / "worker.sqlite3").as_posix()
    configured = Database.connect(f"sqlite:///{path}")
    Base.metadata.create_all(configured.engine)
    try:
        yield configured
    finally:
        configured.dispose()


def submit_review(database: Database, key: str = "worker-test") -> str:
    result = ReviewService(
        SqlAlchemyReviewRepository(database.sessions)
    ).submit(
        ReviewRequest(
            installation_id=10,
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=128,
            head_sha="a" * 40,
        ),
        key,
    )
    # Keep task availability independent of the wall clock used by CI.
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, result.review_task_id)
        assert task is not None
        task.available_at = TEST_TASK_AVAILABLE_AT
        session.commit()
    return result.review_task_id


def test_worker_claims_one_task_and_stops_at_waiting_for_ci(
    database: Database,
) -> None:
    task_id = submit_review(database)
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    queue = SqlAlchemyReviewTaskQueue(database.sessions, clock=MutableClock(now))
    runtime = WorkerRuntime(
        queue,
        WorkerSettings(
            worker_id="worker-1",
            poll_interval=timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        ),
    )

    assert runtime.run_once() is True

    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, task.review_run_id)
        heartbeat = session.get(WorkerHeartbeatRecord, "worker-1")
        assert task.execution_status == ExecutionStatus.WAITING_FOR_CI.value
        assert run.execution_status == ExecutionStatus.WAITING_FOR_CI.value
        assert task.attempt_count == 1
        assert task.lease_owner is None
        assert task.lease_expires_at is None
        assert heartbeat.status == WorkerStatus.IDLE.value
        assert heartbeat.current_task_id is None
        assert (
            session.scalar(select(func.count()).select_from(OutboxEventRecord))
            == 3
        )


def test_failed_attempt_is_retried_with_backoff_and_old_lease_is_rejected(
    database: Database,
) -> None:
    task_id = submit_review(database, "retry-test")
    clock = MutableClock(datetime(2026, 8, 18, 12, 0, tzinfo=UTC))
    queue = SqlAlchemyReviewTaskQueue(
        database.sessions,
        clock=clock,
        retry_base_seconds=5,
    )
    first = queue.claim_next("worker-1", timedelta(seconds=30))
    assert first is not None

    queue.retry_or_fail(first, "temporary failure")
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        assert task.execution_status == ExecutionStatus.QUEUED.value
        assert task.last_error == "temporary failure"

    assert queue.claim_next("worker-1", timedelta(seconds=30)) is None
    clock.value += timedelta(seconds=5)
    second = queue.claim_next("worker-1", timedelta(seconds=30))
    assert second is not None
    assert second.attempt_count == 2

    with pytest.raises(TaskLeaseLostError):
        queue.mark_waiting_for_ci(first)


def test_expired_final_lease_marks_task_and_run_failed(database: Database) -> None:
    task_id = submit_review(database, "expired-test")
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        task.max_attempts = 1
        session.commit()

    clock = MutableClock(datetime(2026, 8, 18, 12, 0, tzinfo=UTC))
    queue = SqlAlchemyReviewTaskQueue(database.sessions, clock=clock)
    lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert lease is not None
    clock.value += timedelta(seconds=31)

    with pytest.raises(TaskLeaseLostError):
        queue.mark_waiting_for_ci(lease)

    assert queue.recover_expired_leases() == 1

    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, task.review_run_id)
        assert task.execution_status == ExecutionStatus.FAILED.value
        assert run.execution_status == ExecutionStatus.FAILED.value
        assert "租约超时" in task.last_error


def test_worker_heartbeat_freshness_is_observable(database: Database) -> None:
    clock = MutableClock(datetime(2026, 8, 18, 12, 0, tzinfo=UTC))
    queue = SqlAlchemyReviewTaskQueue(database.sessions, clock=clock)
    queue.record_heartbeat("worker-1", WorkerStatus.IDLE)

    assert queue.heartbeat_is_fresh("worker-1", timedelta(seconds=15)) is True
    clock.value += timedelta(seconds=16)
    assert queue.heartbeat_is_fresh("worker-1", timedelta(seconds=15)) is False
