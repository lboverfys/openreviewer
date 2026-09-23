"""模型完成、文件覆盖与人工门禁的一致性回归。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, select

from persistence.models import (
    ReviewFilePlanRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
)
from persistence.review_management import SqlAlchemyReviewManagementRepository
from services.review_management import (
    ReviewAction,
    ReviewActionConflictError,
    ReviewManagementService,
)
from tests.integration.test_review_management_api import application_for, login
from tests.integration.test_review_plan_persistence import (
    MutableClock,
    _store_complete_review,
)
from tests.integration.test_review_plan_persistence import database as database


def _completed_review(database, *, excluded=(), head_sha="a" * 40):
    clock = MutableClock(datetime.now(UTC))
    run_id = _store_complete_review(database, clock, key=f"coverage-{head_sha}",
        head_sha=head_sha, include_finding=True)
    with database.sessions() as session, session.begin():
        plan = session.scalar(select(ReviewPlanRecord).where(ReviewPlanRecord.review_run_id == run_id))
        # 模拟升级前保存的不可变计划，包含当时未支持的 .gitignore。
        for ordinal, path in enumerate(excluded, start=plan.file_count + 1):
            session.add(ReviewFilePlanRecord(id=str(uuid4()), review_plan_id=plan.id,
                ordinal=ordinal, file=path, decision="unsupported"))
        plan.file_count += len(excluded)
        if excluded:
            session.get(ReviewRunRecord, run_id).coverage_status = "partial"
        finding = session.scalar(select(ReviewFindingRecord).where(ReviewFindingRecord.review_run_id == run_id))
        finding.adjudication_status = "valid"
    return run_id


def test_completed_legacy_plan_reports_the_real_block_and_preserves_results(database):
    run_id = _completed_review(database, excluded=(".gitignore",))
    application = application_for(database)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://testserver") as client:
            await login(client)
            for view in ("full", "overview"):
                response = await client.get(f"/api/v1/reviews/{run_id}?view={view}")
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["phase"] == "coverage_incomplete"
                assert body["model_review_completed_at"] is not None
                assert body["failed_agents"] == body["failed_batches"] == []
                assert body["valid_finding_count"] == 1
                assert body["unreviewed_finding_count"] == 0
                assert body["available_actions"] == ["new_review", "reject", "pause"]
                assert body["excluded_file_examples"] == [{"file":".gitignore","decision":"unsupported","unit_key":None}]
                assert "1 个变更文件未进入审查" in body["coverage_block_reason"]
                assert "重试已成功批次不会补入" in body["coverage_block_reason"]
            blocked = await client.post(f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key":"coverage-approve"}, json={"action":"approve"})
            assert blocked.status_code == 409
            assert blocked.json()["detail"] == body["coverage_block_reason"]
            new = await client.post(f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key":"coverage-new"}, json={"action":"new_review"})
            assert new.status_code == 200, new.text
            assert new.json()["review_run_id"] != run_id
            old = (await client.get(f"/api/v1/reviews/{run_id}")).json()
            assert old["review_plan_id"] == body["review_plan_id"]
            assert old["valid_finding_count"] == 1
            assert old["plan_file_decisions"] == {"planned":1,"unsupported":1}

    asyncio.run(exercise())


@pytest.mark.parametrize("coverage,rules_complete,reason", [
    ("partial", False, "审查规则快照不完整"),
    ("partial", True, "覆盖记录不完整"),
    ("stale", True, "提交已过期"),
])
def test_approval_and_publish_share_the_detail_block_reason(database, coverage, rules_complete, reason):
    run_id = _completed_review(database)
    publisher = Mock()
    repository = SqlAlchemyReviewManagementRepository(database.sessions, publisher=publisher)
    manager = ReviewManagementService(repository)
    with database.sessions() as session, session.begin():
        session.get(ReviewRunRecord, run_id).coverage_status = coverage
        plan = session.scalar(select(ReviewPlanRecord).where(ReviewPlanRecord.review_run_id == run_id))
        plan.rules_complete = rules_complete
    details = manager.details(run_id)
    assert reason in details.stored.coverage_block_reason
    assert ReviewAction.APPROVE not in details.available_actions
    with pytest.raises(ReviewActionConflictError) as blocked:
        manager.apply_action(run_id, ReviewAction.APPROVE, actor="tester", request_id="blocked")
    assert str(blocked.value) == details.stored.coverage_block_reason
    with database.sessions() as session, session.begin():
        session.get(ReviewRunRecord, run_id).workflow_status = "awaiting_publish"
    publish_details = manager.details(run_id)
    assert publish_details.phase == "coverage_incomplete"
    assert ReviewAction.PUBLISH not in publish_details.available_actions
    with pytest.raises(ReviewActionConflictError) as blocked:
        manager.apply_action(run_id, ReviewAction.PUBLISH, actor="tester", request_id="blocked-publish")
    assert str(blocked.value) == details.stored.coverage_block_reason
    assert publisher.mock_calls == []


def test_complete_coverage_can_still_be_approved_and_failed_nodes_can_still_be_retried(database):
    run_id = _completed_review(database)
    manager = ReviewManagementService(SqlAlchemyReviewManagementRepository(database.sessions))
    details = manager.details(run_id)
    assert details.phase == "awaiting_approval"
    assert details.stored.coverage_block_reason is None
    assert ReviewAction.APPROVE in details.available_actions
    partial = replace(details.stored, coverage_status="partial", model_review_completed_at=None,
        failed_agents=("security",))
    assert "Agent 或批次未完成" in partial.coverage_block_reason
    actions = manager._available_actions(partial, 0)
    assert ReviewAction.RETRY_FAILED_NODE in actions
    assert ReviewAction.APPROVE not in actions
    manager.apply_action(run_id, ReviewAction.APPROVE, actor="tester", request_id="approve-complete")
    assert manager.details(run_id).phase == "awaiting_publish"


def test_excluded_file_examples_are_bounded_and_scoped_to_one_plan(database):
    run_id = _completed_review(database, excluded=tuple(f"a/{i:02d}.unknown" for i in range(12)))
    other_run_id = _completed_review(database, excluded=("other.unknown",), head_sha="b" * 40)
    statements = []

    def capture(_connection, _cursor, statement, parameters, _context, _executemany):
        if "SELECT review_file_plans.file, review_file_plans.decision" in statement:
            statements.append((statement, parameters))

    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        repository = SqlAlchemyReviewManagementRepository(database.sessions)
        details = repository.get(run_id)
        assert len(details.excluded_file_examples) == 10
        assert [item.file for item in details.excluded_file_examples] == [f"a/{i:02d}.unknown" for i in range(10)]
        assert details.plan_file_decisions["unsupported"] == 12
        assert len(statements) == 1
        assert "LIMIT" in statements[0][0]
        assert 10 in statements[0][1]
        assert repository.get(other_run_id).excluded_file_examples[0].file == "other.unknown"
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)
