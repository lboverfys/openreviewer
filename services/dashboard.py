"""Read models used by the authenticated operational dashboard."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from domain.enums import ExecutionStatus, WorkerStatus


class DashboardPersistenceError(RuntimeError):
    """Dashboard data could not be read from durable storage."""


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ReviewListItem:
    review_run_id: str
    review_task_id: str
    repository: str
    pull_request_number: int
    head_sha: str
    execution_status: ExecutionStatus
    attempt_count: int
    max_attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredWorkerHeartbeat:
    worker_id: str
    status: WorkerStatus
    current_task_id: str | None
    started_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class DashboardData:
    total_reviews: int
    status_counts: Mapping[ExecutionStatus, int]
    recent_reviews: tuple[ReviewListItem, ...]
    latest_worker: StoredWorkerHeartbeat | None


class DashboardRepository(Protocol):
    def load(self, limit: int) -> DashboardData: ...


@dataclass(frozen=True, slots=True)
class WorkerSnapshot:
    configured: bool
    online: bool
    worker_id: str | None
    status: WorkerStatus | None
    current_task_id: str | None
    started_at: datetime | None
    last_seen_at: datetime | None


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    generated_at: datetime
    total_reviews: int
    status_counts: Mapping[ExecutionStatus, int]
    worker: WorkerSnapshot
    recent_reviews: tuple[ReviewListItem, ...]


class DashboardService:
    def __init__(
        self,
        repository: DashboardRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        worker_online_window: timedelta = timedelta(seconds=15),
    ) -> None:
        if worker_online_window.total_seconds() <= 0:
            raise ValueError("worker_online_window must be positive")
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))
        self._worker_online_window = worker_online_window

    def snapshot(self, limit: int = 50) -> DashboardSnapshot:
        if not 1 <= limit <= 100:
            raise ValueError("dashboard limit must be between 1 and 100")
        now = self._clock().astimezone(UTC)
        data = self._repository.load(limit)
        heartbeat = data.latest_worker
        if heartbeat is None:
            worker = WorkerSnapshot(
                configured=False,
                online=False,
                worker_id=None,
                status=None,
                current_task_id=None,
                started_at=None,
                last_seen_at=None,
            )
        else:
            worker = WorkerSnapshot(
                configured=True,
                online=now - as_utc(heartbeat.last_seen_at)
                <= self._worker_online_window,
                worker_id=heartbeat.worker_id,
                status=heartbeat.status,
                current_task_id=heartbeat.current_task_id,
                started_at=as_utc(heartbeat.started_at),
                last_seen_at=as_utc(heartbeat.last_seen_at),
            )
        complete_counts = {
            status: int(data.status_counts.get(status, 0))
            for status in ExecutionStatus
        }
        return DashboardSnapshot(
            generated_at=now,
            total_reviews=data.total_reviews,
            status_counts=complete_counts,
            worker=worker,
            recent_reviews=data.recent_reviews,
        )
