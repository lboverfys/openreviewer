"""节点预览、分页、权限及保留期在隔离数据库中验证。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, insert, select, update

from domain.pagination import decode_cursor
from persistence.dashboard import SqlAlchemyDashboardRepository
from persistence.models import ReviewTaskRecord, WorkerHeartbeatRecord
from persistence.operations import SqlAlchemyOperationsRepository
from persistence.workers import WorkerRepository, load_worker_overview
from services.dashboard import DashboardService
from services.rbac import ResourceScope
from tests.integration.test_finding_pagination import _seed_review
from tests.integration.test_management_api import database as database

NOW = datetime(2026, 9, 13, 7, tzinfo=UTC)
ALL = ResourceScope.unrestricted_scope()


def seed_workers(database):
    history = [dict(worker_id=f"old-{number:04}", status="stopping", current_task_id=None,
        started_at=NOW - timedelta(days=2, seconds=number),
        last_seen_at=NOW - timedelta(days=1, seconds=number)) for number in range(1000)]
    active = [dict(worker_id=f"live-{number}", status="busy" if number % 2 else "idle", current_task_id=None,
        started_at=NOW - timedelta(minutes=1, seconds=number), last_seen_at=NOW - timedelta(seconds=1)) for number in range(8)]
    with database.sessions() as session, session.begin():
        session.execute(insert(WorkerHeartbeatRecord), history + active + [dict(
            worker_id="closing", status="stopping", current_task_id=None,
            started_at=NOW - timedelta(seconds=1), last_seen_at=NOW,
        )])


def test_overview_uses_one_query_for_exact_counts_and_three_live_rows(database):
    seed_workers(database)
    statements = []
    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)
    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        with database.sessions() as session:
            overview = load_worker_overview(session, NOW - timedelta(seconds=15), ALL)
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert overview.online_count == 8 and overview.busy_count == 4
    assert [worker.worker_id for worker in overview.workers] == ["live-0", "live-1", "live-2"]


def test_hidden_worker_state_and_expiration_change_sse_token(database):
    seed_workers(database)
    clock = [NOW]
    service = DashboardService(SqlAlchemyDashboardRepository(database.sessions), clock=lambda: clock[0])
    snapshot = service.snapshot()
    assert len(snapshot.workers) == 3 and snapshot.worker_online_count == 8
    assert all(worker.online for worker in snapshot.workers)
    first = service.change_token()
    with database.sessions() as session, session.begin():
        session.execute(update(WorkerHeartbeatRecord).where(WorkerHeartbeatRecord.worker_id == "live-7").values(status="idle"))
    assert service.change_token() != first
    clock[0] += timedelta(seconds=16)
    offline = service.snapshot()
    assert offline.worker_online_count == 0 and not offline.workers
    assert offline.worker.configured and not offline.worker.online


def test_history_pages_are_bounded_and_heartbeat_updates_do_not_move_rows(database):
    seed_workers(database)
    repository = WorkerRepository(database.sessions, clock=lambda: NOW)
    first = repository.page(ALL, state="all", limit=10)
    assert len(first.items) == 10 and first.next_cursor
    with database.sessions() as session, session.begin():
        session.execute(update(WorkerHeartbeatRecord).where(WorkerHeartbeatRecord.worker_id.like("live-%")).values(last_seen_at=NOW))
    second = repository.page(ALL, state="all", limit=10, cursor=first.next_cursor)
    assert len(second.items) == 10
    assert not {item.worker_id for item in first.items} & {item.worker_id for item in second.items}
    assert all(not item.online for item in repository.page(ALL, state="offline").items)
    assert len(repository.page(ALL, state="online").items) == 8
    with pytest.raises(ValueError):
        repository.page(ALL, limit=101)


def test_long_worker_identifiers_use_bounded_extended_cursor(database):
    seed_workers(database)
    identifier = "节" * 200
    with database.sessions() as session, session.begin():
        session.add(WorkerHeartbeatRecord(worker_id=identifier, status="idle", started_at=NOW, last_seen_at=NOW))
    repository = WorkerRepository(database.sessions, clock=lambda: NOW)
    first = repository.page(ALL, state="all", limit=1)
    assert first.items[0].worker_id == identifier
    assert first.next_cursor and 512 < len(first.next_cursor) <= 1536
    with pytest.raises(ValueError):
        decode_cursor(first.next_cursor)
    second = repository.page(ALL, state="all", limit=1, cursor=first.next_cursor)
    assert second.items[0].worker_id != identifier


def test_worker_task_links_keep_repository_scope(database):
    run_id = _seed_review(database, finding_count=0)
    with database.sessions() as session, session.begin():
        task_id = session.scalar(select(ReviewTaskRecord.id).where(ReviewTaskRecord.review_run_id == run_id))
        session.add(WorkerHeartbeatRecord(worker_id="private-task", status="busy", current_task_id=task_id, started_at=NOW, last_seen_at=NOW))
    repository = WorkerRepository(database.sessions, clock=lambda: NOW)
    visible = repository.page(ALL).items[0]
    assert visible.current_review_run_id == run_id
    hidden = repository.page(ResourceScope(repositories=frozenset({"other/repo"}))).items[0]
    assert hidden.online and hidden.current_task_id is None and hidden.current_review_run_id is None


def test_heartbeat_cleanup_keeps_a_restarted_worker_and_respects_batch_limit(database):
    with database.sessions() as session, session.begin():
        session.execute(insert(WorkerHeartbeatRecord), [dict(worker_id=f"expired-{number}", status="stopping",
            started_at=NOW - timedelta(days=10), last_seen_at=NOW - timedelta(days=8)) for number in range(3)])
        session.add(WorkerHeartbeatRecord(worker_id="restarted", status="starting",
            started_at=NOW - timedelta(days=10), last_seen_at=NOW))
    with database.sessions() as session, session.begin():
        assert SqlAlchemyOperationsRepository._delete_worker_heartbeats(session, NOW - timedelta(days=7), 2) == 2
        assert session.scalar(select(WorkerHeartbeatRecord.worker_id).where(WorkerHeartbeatRecord.worker_id == "restarted")) == "restarted"
    with database.sessions() as session, session.begin():
        assert SqlAlchemyOperationsRepository._delete_worker_heartbeats(session, NOW - timedelta(days=7), 2) == 1
