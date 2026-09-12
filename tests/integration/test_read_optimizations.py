"""验证查询复用、长任务进度和分页的资源隔离。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, insert, select

from persistence.dashboard import SqlAlchemyDashboardRepository
from persistence.models import (
    ModelReviewBatchRecord,
    OutboxEventRecord,
    ReviewPlanRecord,
)
from persistence.review_management import SqlAlchemyReviewManagementRepository
from services.dashboard import DashboardService
from services.rbac import ResourceScope
from services.review_management import ReviewNotFoundError
from tests.integration.test_finding_pagination import _seed_review
from tests.integration.test_list_pagination import seed_runs
from tests.integration.test_management_api import database as database


def test_next_page_reuses_count_but_first_page_and_other_scopes_refresh(database, monkeypatch):
    seed_runs(database)
    now = [100.0]
    monkeypatch.setattr("persistence.dashboard.time.monotonic", lambda: now[0])
    service = DashboardService(SqlAlchemyDashboardRepository(database.sessions))
    statements = []
    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)
    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        first = service.snapshot(10, include_overview=False)
        assert len(statements) == 3
        statements.clear()
        second = service.snapshot(10, first.next_cursor, include_overview=False)
        assert second.total_reviews == first.total_reviews == 23
        assert len(statements) == 2
        statements.clear()
        service.snapshot(10, include_overview=False)
        assert len(statements) == 3
        now[0] += 6
        statements.clear()
        service.snapshot(10, first.next_cursor, include_overview=False)
        assert len(statements) == 3
        denied = service.snapshot(10, first.next_cursor, include_overview=False,
            scope=ResourceScope(repositories=frozenset({"other/repo"})))
        assert denied.total_reviews == 0 and not denied.recent_reviews
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)


def test_overview_uses_all_batches_without_loading_history_or_findings(database, monkeypatch):
    run_id = _seed_review(database, finding_count=25)
    now = datetime(2026, 9, 12, tzinfo=UTC)
    with database.sessions() as session, session.begin():
        plan_id = session.scalar(select(ReviewPlanRecord.id).where(ReviewPlanRecord.review_run_id == run_id))
        session.execute(insert(ModelReviewBatchRecord), [{
            "id": f"batch-{number}", "review_plan_id": plan_id, "agent": "security",
            "batch_number": number, "batch_count": 150, "unit_keys": [], "status": "succeeded",
            "duration_ms": 20, "result": {"usage": {"input_tokens": 100, "output_tokens": 10, "reasoning_output_tokens": 2}},
        } for number in range(1, 151)])
        session.execute(insert(OutboxEventRecord), [{
            "id": f"event-{number}", "event_key": f"event-{number}", "aggregate_type": "review_run",
            "aggregate_id": run_id, "event_type": "review.model.batch_completed",
            "payload": {"agent": "security", "batch_number": number},
            "occurred_at": now + timedelta(seconds=number),
        } for number in range(1, 701)])
    repository = SqlAlchemyReviewManagementRepository(database.sessions)
    def forbidden(*args, **kwargs):
        raise AssertionError("概览不应读取问题页、完整日志或评测样本")
    monkeypatch.setattr(repository, "_load_findings", forbidden)
    monkeypatch.setattr(repository, "_load_events", forbidden)
    monkeypatch.setattr(repository, "_load_evaluation_gates", forbidden)
    overview = repository.get(run_id, view="overview")
    assert overview.findings == () and overview.evaluation_gates == ()
    assert len(overview.events) == 1
    assert overview.change_token == repository.change_token(run_id)
    progress = overview.batch_progress["security"]
    assert (progress.total, progress.completed, progress.input_tokens) == (150, 150, 15000)
    first = repository.batch_page(run_id, "security")
    second = repository.batch_page(run_id, "security", after=int(first.next_cursor))
    assert [item.batch_number for item in first.items] == list(range(1, 11))
    assert [item.batch_number for item in second.items] == list(range(11, 21))
    with pytest.raises(ReviewNotFoundError):
        repository.batch_page(run_id, "security", scope=ResourceScope(repositories=frozenset({"other/repo"})))
