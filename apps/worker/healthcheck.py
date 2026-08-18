"""Container healthcheck for the database worker heartbeat."""

from datetime import timedelta
import os

from apps.worker.main import WorkerSettings
from persistence.database import Database
from persistence.task_queue import SqlAlchemyReviewTaskQueue


def main() -> None:
    settings = WorkerSettings.from_environment()
    database = Database.from_environment()
    try:
        queue = SqlAlchemyReviewTaskQueue(database.sessions)
        maximum_age = timedelta(
            seconds=max(15, settings.poll_interval.total_seconds() * 4)
        )
        if not queue.heartbeat_is_fresh(settings.worker_id, maximum_age):
            raise SystemExit("worker heartbeat is stale")
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
