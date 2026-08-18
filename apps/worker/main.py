"""Process entry point for the single-concurrency database worker."""

from dataclasses import dataclass
from datetime import timedelta
import logging
import os
import signal
import socket
from threading import Event

from domain.enums import WorkerStatus
from persistence.database import Database
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from services.task_queue import ReviewTaskLease, ReviewTaskQueue, TaskQueueError


LOGGER = logging.getLogger("openreviewer.worker")


def _positive_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    worker_id: str
    poll_interval: timedelta
    lease_duration: timedelta

    @classmethod
    def from_environment(cls) -> "WorkerSettings":
        worker_id = os.environ.get(
            "OPENREVIEWER_WORKER_ID",
            f"{socket.gethostname()}:{os.getpid()}",
        ).strip()
        if not worker_id or len(worker_id) > 200:
            raise ValueError("OPENREVIEWER_WORKER_ID must contain 1 to 200 characters")
        poll_seconds = _positive_float(
            os.environ.get("OPENREVIEWER_WORKER_POLL_SECONDS", "2"),
            "OPENREVIEWER_WORKER_POLL_SECONDS",
        )
        lease_seconds = _positive_float(
            os.environ.get("OPENREVIEWER_WORKER_LEASE_SECONDS", "30"),
            "OPENREVIEWER_WORKER_LEASE_SECONDS",
        )
        if lease_seconds <= poll_seconds * 2:
            raise ValueError("worker lease duration must exceed twice the poll interval")
        return cls(
            worker_id=worker_id,
            poll_interval=timedelta(seconds=poll_seconds),
            lease_duration=timedelta(seconds=lease_seconds),
        )


class WorkerRuntime:
    def __init__(
        self,
        queue: ReviewTaskQueue,
        settings: WorkerSettings,
        *,
        stop_event: Event | None = None,
    ) -> None:
        self._queue = queue
        self._settings = settings
        self._stop_event = stop_event or Event()

    @property
    def stop_event(self) -> Event:
        return self._stop_event

    def run(self) -> None:
        worker_id = self._settings.worker_id
        self._queue.record_heartbeat(worker_id, WorkerStatus.STARTING)
        LOGGER.info("Worker 已启动，等待数据库任务")
        try:
            while not self._stop_event.is_set():
                self.run_once()
                self._stop_event.wait(self._settings.poll_interval.total_seconds())
        finally:
            try:
                self._queue.record_heartbeat(worker_id, WorkerStatus.STOPPING)
            except TaskQueueError:
                LOGGER.exception("Worker 停止状态写入失败")
            LOGGER.info("Worker 已停止")

    def run_once(self) -> bool:
        worker_id = self._settings.worker_id
        recovered = self._queue.recover_expired_leases()
        if recovered:
            LOGGER.warning("已恢复 %s 个租约超时任务", recovered)

        self._queue.record_heartbeat(worker_id, WorkerStatus.IDLE)
        lease = self._queue.claim_next(worker_id, self._settings.lease_duration)
        if lease is None:
            return False

        self._queue.record_heartbeat(worker_id, WorkerStatus.BUSY, lease.task_id)
        try:
            self._advance_to_supported_boundary(lease)
            LOGGER.info("任务 %s 已进入 waiting_for_ci", lease.task_id)
        except Exception as exc:
            LOGGER.exception("任务 %s 处理失败", lease.task_id)
            try:
                self._queue.retry_or_fail(lease, str(exc))
            except TaskQueueError:
                LOGGER.exception("任务 %s 的失败状态无法持久化", lease.task_id)
        finally:
            self._queue.record_heartbeat(worker_id, WorkerStatus.IDLE)
        return True

    def _advance_to_supported_boundary(self, lease: ReviewTaskLease) -> None:
        """Stop honestly at the boundary before GitHub/CI integration exists."""

        self._queue.mark_waiting_for_ci(lease)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("OPENREVIEWER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = WorkerSettings.from_environment()
    database = Database.from_environment()
    runtime = WorkerRuntime(
        SqlAlchemyReviewTaskQueue(database.sessions),
        settings,
    )

    def stop_worker(_signum: int, _frame: object) -> None:
        runtime.stop_event.set()

    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)
    try:
        runtime.run()
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
