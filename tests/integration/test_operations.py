from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, func, select, text

from domain.enums import ExecutionStatus, WorkerStatus
from domain.models import ReviewRequest
from persistence.database import Database
from persistence.models import (
    AdminSessionRecord,
    Base,
    FindingEvaluationRecord,
    OutboxEventRecord,
    ReviewQuotaBucketRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
    WorkerHeartbeatRecord,
)
from persistence.operations import SqlAlchemyOperationsRepository
from persistence.repositories import SqlAlchemyReviewRepository
from services.operations import (
    EXPECTED_DATABASE_REVISION,
    OperationsService,
    OperationsSettings,
    RetentionCutoffs,
)
from services.reviews import ReviewService


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@pytest.fixture
def database(tmp_path: Path):
    path = (tmp_path / "operations.sqlite3").as_posix()
    configured = Database.connect(f"sqlite:///{path}")

    @event.listens_for(configured.engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(configured.engine)
    with configured.engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": EXPECTED_DATABASE_REVISION},
        )
    try:
        yield configured
    finally:
        configured.dispose()


def add_outbox_event(
    database: Database,
    event_id: str,
    occurred_at: datetime,
    *,
    published_at: datetime | None = None,
) -> None:
    with database.sessions() as session:
        session.add(
            OutboxEventRecord(
                id=event_id,
                event_key=f"event:{event_id}",
                aggregate_type="review_run",
                aggregate_id=f"run-{event_id}",
                event_type="review.test",
                payload={"index": event_id},
                occurred_at=occurred_at,
                published_at=published_at,
                publish_attempts=0,
                next_publish_attempt_at=occurred_at,
            )
        )
        session.commit()


def test_readiness_and_metrics_use_database_migration_and_fresh_worker(
    database: Database,
) -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    with database.sessions() as session:
        session.add(
            WorkerHeartbeatRecord(
                worker_id="worker-ready",
                status=WorkerStatus.IDLE.value,
                started_at=now,
                last_seen_at=now,
            )
        )
        session.commit()
    add_outbox_event(database, "pending", now - timedelta(seconds=12))
    repository = SqlAlchemyOperationsRepository(
        database.sessions,
        clock=MutableClock(now),
    )

    snapshot = repository.readiness(
        EXPECTED_DATABASE_REVISION,
        timedelta(seconds=45),
    )
    metrics = OperationsService(repository).metrics()

    assert snapshot.ready is True
    assert "openreviewer_workers_fresh 1" in metrics
    assert "openreviewer_outbox_pending 1" in metrics
    assert "openreviewer_outbox_oldest_age_seconds 12.000" in metrics


def test_outbox_is_claimed_in_batches_and_marked_published(database: Database) -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    clock = MutableClock(now)
    for index in range(3):
        add_outbox_event(
            database,
            f"event-{index}",
            now - timedelta(seconds=3 - index),
        )
    published: list[str] = []
    settings = OperationsSettings(outbox_batch_size=2)
    service = OperationsService(
        SqlAlchemyOperationsRepository(database.sessions, clock=clock),
        settings,
        clock=clock,
        publisher=lambda item: published.append(item.id),
    )

    assert service.publish_pending("worker-1") == 2
    assert service.publish_pending("worker-1") == 1
    assert service.publish_pending("worker-1") == 0

    assert published == ["event-0", "event-1", "event-2"]
    with database.sessions() as session:
        rows = tuple(
            session.scalars(
                select(OutboxEventRecord).order_by(OutboxEventRecord.id)
            )
        )
        assert all(
            row.published_at is not None and as_utc(row.published_at) == now
            for row in rows
        )
        assert all(row.publish_attempts == 1 for row in rows)
        assert all(row.publish_lease_owner is None for row in rows)


def test_failed_outbox_publish_is_retried_after_bounded_delay(
    database: Database,
) -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    clock = MutableClock(now)
    add_outbox_event(database, "retry", now)
    repository = SqlAlchemyOperationsRepository(database.sessions, clock=clock)
    settings = OperationsSettings(outbox_retry_delay=timedelta(seconds=10))

    def fail_publish(_event) -> None:
        raise RuntimeError("token=must-not-be-persisted")

    failed_service = OperationsService(
        repository,
        settings,
        clock=clock,
        publisher=fail_publish,
    )
    assert failed_service.publish_pending("worker-1") == 0
    assert failed_service.publish_pending("worker-1") == 0

    with database.sessions() as session:
        row = session.get(OutboxEventRecord, "retry")
        assert row is not None
        assert row.publish_attempts == 1
        assert row.publish_lease_owner is None
        assert as_utc(row.next_publish_attempt_at) == now + timedelta(seconds=10)
        assert "RuntimeError" in row.last_publish_error
        assert "must-not-be-persisted" not in row.last_publish_error

    clock.value += timedelta(seconds=10)
    published: list[str] = []
    recovered_service = OperationsService(
        repository,
        settings,
        clock=clock,
        publisher=lambda item: published.append(item.id),
    )
    assert recovered_service.publish_pending("worker-2") == 1
    assert published == ["retry"]


def test_cleanup_deletes_each_data_class_in_bounded_batches(
    database: Database,
) -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    old = now - timedelta(days=400)
    for index in range(3):
        add_outbox_event(database, f"old-{index}", old, published_at=old)
    with database.sessions() as session:
        for index in range(3):
            session.add(
                AdminSessionRecord(
                    session_hash=f"{index:064d}",
                    username="administrator",
                    issued_at=old - timedelta(hours=1),
                    expires_at=old,
                )
            )
            session.add(
                WorkerHeartbeatRecord(
                    worker_id=f"old-worker-{index}",
                    status=WorkerStatus.STOPPING.value,
                    started_at=old - timedelta(hours=1),
                    last_seen_at=old,
                )
            )
        session.commit()
    repository = SqlAlchemyOperationsRepository(
        database.sessions,
        clock=MutableClock(now),
    )
    cutoffs = RetentionCutoffs(
        published_outbox=now - timedelta(days=14),
        admin_sessions=now - timedelta(days=7),
        worker_heartbeats=now - timedelta(days=7),
        webhooks=now - timedelta(days=90),
        reviews=now - timedelta(days=180),
    )

    first = repository.cleanup(cutoffs, batch_size=2)
    second = repository.cleanup(cutoffs, batch_size=2)

    assert first.published_outbox_events == 2
    assert first.admin_sessions == 2
    assert first.worker_heartbeats == 2
    assert second.published_outbox_events == 1
    assert second.admin_sessions == 1
    assert second.worker_heartbeats == 1


def test_cleanup_deletes_quota_buckets_in_bounded_batches(
    database: Database,
) -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    old = now - timedelta(days=10)
    fresh = now - timedelta(hours=1)
    with database.sessions() as session:
        for index in range(3):
            session.add(
                ReviewQuotaBucketRecord(
                    id=f"old-quota-{index}",
                    scope="user",
                    scope_key=f"user-{index}",
                    window="hour",
                    window_start=old,
                    request_count=1,
                    updated_at=old,
                )
            )
        session.add(
            ReviewQuotaBucketRecord(
                id="fresh-quota",
                scope="global",
                scope_key="global",
                window="day",
                window_start=fresh,
                request_count=1,
                updated_at=fresh,
            )
        )
        session.commit()

    repository = SqlAlchemyOperationsRepository(
        database.sessions,
        clock=MutableClock(now),
    )
    cutoffs = RetentionCutoffs(
        published_outbox=now,
        admin_sessions=now,
        worker_heartbeats=now,
        webhooks=now,
        reviews=now,
        quota_buckets=now - timedelta(days=3),
    )

    first = repository.cleanup(cutoffs, batch_size=2)
    second = repository.cleanup(cutoffs, batch_size=2)

    assert first.quota_buckets == 2
    assert second.quota_buckets == 1
    assert first.total == 2
    assert second.total == 1
    with database.sessions() as session:
        remaining = tuple(
            session.scalars(
                select(ReviewQuotaBucketRecord).order_by(ReviewQuotaBucketRecord.id)
            )
        )
    assert [row.id for row in remaining] == ["fresh-quota"]


def test_cleanup_deletes_finding_evaluations_in_bounded_batches(
    database: Database,
) -> None:
    """评测样本按独立保留期分批删除，不受 Finding 外键影响。"""

    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    old = now - timedelta(days=800)
    fresh = now - timedelta(days=10)
    with database.sessions() as session:
        session.add_all(
            [
                FindingEvaluationRecord(
                    finding_id=f"old-evaluation-{index}",
                    repository_id=42,
                    category="security",
                    severity="high",
                    verdict="valid",
                    adjudicated_at=old,
                    adjudicated_by="reviewer",
                    updated_at=old,
                )
                for index in range(3)
            ]
            + [
                FindingEvaluationRecord(
                    finding_id="fresh-evaluation",
                    repository_id=42,
                    category="security",
                    severity="high",
                    verdict="valid",
                    adjudicated_at=fresh,
                    adjudicated_by="reviewer",
                    updated_at=fresh,
                )
            ]
        )
        session.commit()

    repository = SqlAlchemyOperationsRepository(
        database.sessions,
        clock=MutableClock(now),
    )
    cutoffs = RetentionCutoffs(
        published_outbox=now,
        admin_sessions=now,
        worker_heartbeats=now,
        webhooks=now,
        reviews=now,
        finding_evaluations=now - timedelta(days=730),
    )

    first = repository.cleanup(cutoffs, batch_size=2)
    second = repository.cleanup(cutoffs, batch_size=2)

    assert first.finding_evaluations == 2
    assert second.finding_evaluations == 1
    assert first.total == 2
    assert second.total == 1
    with database.sessions() as session:
        remaining = tuple(
            session.scalars(
                select(FindingEvaluationRecord).order_by(
                    FindingEvaluationRecord.finding_id
                )
            )
        )
    assert [row.finding_id for row in remaining] == ["fresh-evaluation"]


def test_cleanup_removes_only_terminal_reviews_without_pending_events(
    database: Database,
) -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    old = now - timedelta(days=400)
    submission = ReviewService(
        SqlAlchemyReviewRepository(database.sessions)
    ).submit(
        ReviewRequest(
            installation_id=10,
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=1,
            head_sha="a" * 40,
        ),
        "retention-review",
    )
    with database.sessions() as session:
        run = session.get(ReviewRunRecord, submission.review_run_id)
        task = session.get(ReviewTaskRecord, submission.review_task_id)
        assert run is not None and task is not None
        run.execution_status = ExecutionStatus.COMPLETED.value
        run.workflow_status = ExecutionStatus.COMPLETED.value
        run.created_at = old
        task.execution_status = ExecutionStatus.COMPLETED.value
        task.workflow_status = ExecutionStatus.COMPLETED.value
        for outbox in session.scalars(select(OutboxEventRecord)):
            outbox.published_at = old
        session.commit()

    result = SqlAlchemyOperationsRepository(
        database.sessions,
        clock=MutableClock(now),
    ).cleanup(
        RetentionCutoffs(
            published_outbox=now - timedelta(days=14),
            admin_sessions=now - timedelta(days=7),
            worker_heartbeats=now - timedelta(days=7),
            webhooks=now - timedelta(days=90),
            reviews=now - timedelta(days=180),
        ),
        batch_size=10,
    )

    assert result.review_runs == 1
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ReviewRunRecord)) == 0
        assert session.scalar(select(func.count()).select_from(ReviewTaskRecord)) == 0


def test_cleanup_keeps_model_result_waiting_for_manual_workflow(
    database: Database,
) -> None:
    """兼容字段 completed 不得绕过 awaiting_approval 的人工保留门。"""

    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    old = now - timedelta(days=400)
    submission = ReviewService(
        SqlAlchemyReviewRepository(database.sessions)
    ).submit(
        ReviewRequest(
            installation_id=10,
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=2,
            head_sha="b" * 40,
        ),
        "retention-awaiting-approval",
    )
    with database.sessions() as session:
        run = session.get(ReviewRunRecord, submission.review_run_id)
        task = session.get(ReviewTaskRecord, submission.review_task_id)
        assert run is not None and task is not None
        run.execution_status = ExecutionStatus.COMPLETED.value
        task.execution_status = ExecutionStatus.COMPLETED.value
        run.workflow_status = ExecutionStatus.AWAITING_APPROVAL.value
        task.workflow_status = ExecutionStatus.AWAITING_APPROVAL.value
        run.created_at = old
        session.commit()

    result = SqlAlchemyOperationsRepository(
        database.sessions,
        clock=MutableClock(now),
    ).cleanup(
        RetentionCutoffs(
            published_outbox=now,
            admin_sessions=now,
            worker_heartbeats=now,
            webhooks=now,
            reviews=now - timedelta(days=180),
        ),
        batch_size=10,
    )

    assert result.review_runs == 0
    with database.sessions() as session:
        assert session.get(ReviewRunRecord, submission.review_run_id) is not None
        assert session.get(ReviewTaskRecord, submission.review_task_id) is not None
