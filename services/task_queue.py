"""Application boundary for leasing and advancing review tasks."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from domain.enums import WorkerStatus


class TaskQueueError(RuntimeError):
    """A durable queue operation could not be completed."""


class TaskLeaseLostError(TaskQueueError):
    """A worker tried to mutate a task after losing its lease."""


@dataclass(frozen=True, slots=True)
class ReviewTaskLease:
    task_id: str
    review_run_id: str
    worker_id: str
    attempt_count: int
    lease_expires_at: datetime


class ReviewTaskQueue(Protocol):
    def record_heartbeat(
        self,
        worker_id: str,
        worker_status: WorkerStatus,
        current_task_id: str | None = None,
    ) -> None: ...

    def recover_expired_leases(self) -> int: ...

    def claim_next(self, worker_id: str, lease_duration: timedelta) -> ReviewTaskLease | None: ...

    def renew_lease(
        self,
        lease: ReviewTaskLease,
        lease_duration: timedelta,
    ) -> ReviewTaskLease: ...

    def mark_waiting_for_ci(self, lease: ReviewTaskLease) -> None: ...

    def retry_or_fail(self, lease: ReviewTaskLease, error: str) -> None: ...

    def heartbeat_is_fresh(self, worker_id: str, max_age: timedelta) -> bool: ...
