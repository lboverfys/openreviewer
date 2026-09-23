"""模型完成、文件覆盖与人工门禁的一致性回归。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, func, select

from persistence.models import (
    OutboxEventRecord,
    ReviewFilePlanRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
)
from persistence.review_management import SqlAlchemyReviewManagementRepository
from services.github_publisher import GitHubReviewPublisher
from services.rbac import ResourceScope
from services.review_management import (
    ReviewAction,
    ReviewActionConflictError,
    ReviewManagementService,
    ReviewNotFoundError,
)
from tests.integration.test_review_management_api import application_for, login
from tests.integration.test_review_plan_persistence import (
    MutableClock,
    _store_complete_review,
)
from tests.integration.test_review_plan_persistence import database as database


def _completed_review(database, *, excluded=(), head_sha="a" * 40, decision="unsupported"):
    clock = MutableClock(datetime.now(UTC))
    run_id = _store_complete_review(database, clock, key=f"coverage-{head_sha}",
        head_sha=head_sha, include_finding=True)
    with database.sessions() as session, session.begin():
        plan = session.scalar(select(ReviewPlanRecord).where(ReviewPlanRecord.review_run_id == run_id))
        # 模拟升级前保存的不可变计划，包含当时未支持的 .gitignore。
        for ordinal, path in enumerate(excluded, start=plan.file_count + 1):
            session.add(ReviewFilePlanRecord(id=str(uuid4()), review_plan_id=plan.id,
                ordinal=ordinal, file=path, decision=decision))
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


@pytest.mark.parametrize("decision", ["binary", "generated"])
def test_excluded_assets_require_explicit_approval_and_remain_partial_when_published(database, decision):
    run_id = _completed_review(database, excluded=("image.webp",), decision=decision)
    application = application_for(database)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://testserver") as client:
            await login(client)
            before = (await client.get(f"/api/v1/reviews/{run_id}?view=overview")).json()
            assert before["phase"] == "awaiting_coverage_confirmation"
            assert before["coverage_requires_acknowledgement"] is True
            assert "approve" in before["available_actions"]
            blocked = await client.post(f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key":"plain-approve"}, json={"action":"approve"})
            assert blocked.status_code == 409
            assert "人工接管" in blocked.json()["detail"]
            payload = {"action":"approve", "acknowledge_exclusions":True,
                "state_version":before["change_token"], "head_sha":before["head_sha"]}
            for bad in ({**payload,"state_version":"stale"},{**payload,"head_sha":"b" * 40},
                        {"action":"approve","acknowledge_exclusions":True}):
                denied = await client.post(f"/api/v1/reviews/{run_id}/actions",
                    headers={"Idempotency-Key":"bad-state"}, json=bad)
                assert denied.status_code == 409
            for _ in range(2):
                approved = await client.post(f"/api/v1/reviews/{run_id}/actions",
                    headers={"Idempotency-Key":"confirmed-approve"}, json=payload)
                assert approved.status_code == 200, approved.text
            conflict = await client.post(f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key":"confirmed-approve"}, json={**payload,"acknowledge_exclusions":False})
            assert conflict.status_code == 409
            after = (await client.get(f"/api/v1/reviews/{run_id}?view=overview")).json()
            assert after["phase"] == "awaiting_publish"
            assert after["coverage_status"] == "partial"
            assert after["coverage_exclusions_acknowledged"] is True
            assert after["coverage_block_reason"] is None
            assert "publish" in after["available_actions"]
            assert after["valid_finding_count"] == 1

    asyncio.run(exercise())
    with database.sessions() as session:
        audit = session.scalars(select(OutboxEventRecord).where(
            OutboxEventRecord.aggregate_id == run_id,
            OutboxEventRecord.event_type == "review.coverage.exclusions_acknowledged")).all()
        assert len(audit) == 1
        assert audit[0].payload["excluded_file_count"] == 1
        assert audit[0].payload["review_plan_id"]
        assert audit[0].payload["actor"]
    publisher = Mock()
    repository = SqlAlchemyReviewManagementRepository(database.sessions, publisher=publisher)
    manager = ReviewManagementService(repository)
    assert "未经 AI 文本审查" in GitHubReviewPublisher.render_comment(manager.details(run_id).stored)
    manager.apply_action(run_id, ReviewAction.PUBLISH, actor="tester", request_id="publish-confirmed")
    publisher.assert_called_once()
    assert publisher.call_args.args[0].coverage_status == "partial"


@pytest.mark.parametrize("decision", ["unsupported", "patch_missing", "patch_too_large", "rules_incomplete", "omitted_by_budget"])
def test_acknowledgement_cannot_bypass_missing_reviewable_content(database, decision):
    run_id = _completed_review(database, excluded=("source.unknown",), decision=decision)
    manager = ReviewManagementService(SqlAlchemyReviewManagementRepository(database.sessions))
    before = manager.details(run_id)
    assert before.stored.coverage_requires_acknowledgement is False
    with pytest.raises(ReviewActionConflictError):
        manager.apply_action(run_id, ReviewAction.APPROVE, actor="tester", request_id="no-bypass",
            acknowledge_exclusions=True, state_version=before.stored.change_token, head_sha=before.stored.head_sha)
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(OutboxEventRecord).where(
            OutboxEventRecord.event_type == "review.coverage.exclusions_acknowledged")) == 0


@pytest.mark.parametrize("invalid", ["stale", "rules", "model", "findings"])
def test_asset_confirmation_cannot_bypass_other_approval_requirements(database, invalid):
    run_id = _completed_review(database, excluded=("image.webp",), decision="binary")
    with database.sessions() as session, session.begin():
        plan = session.scalar(select(ReviewPlanRecord).where(ReviewPlanRecord.review_run_id == run_id))
        if invalid == "stale":
            session.get(ReviewRunRecord, run_id).coverage_status = "stale"
        elif invalid == "rules":
            plan.rules_complete = False
        elif invalid == "model":
            plan.model_review_completed_at = None
        else:
            finding = session.scalar(select(ReviewFindingRecord).where(ReviewFindingRecord.review_run_id == run_id))
            finding.adjudication_status = "unreviewed"
    manager = ReviewManagementService(SqlAlchemyReviewManagementRepository(database.sessions))
    before = manager.details(run_id)
    with pytest.raises(ReviewActionConflictError):
        manager.apply_action(run_id, ReviewAction.APPROVE, actor="tester", request_id="no-bypass",
            acknowledge_exclusions=True, state_version=before.stored.change_token, head_sha=before.stored.head_sha)


def test_confirmation_expires_when_model_results_are_rebuilt(database):
    run_id = _completed_review(database, excluded=("image.webp",), decision="binary")
    publisher = Mock()
    manager = ReviewManagementService(SqlAlchemyReviewManagementRepository(database.sessions, publisher=publisher))
    before = manager.details(run_id)
    manager.apply_action(run_id, ReviewAction.APPROVE, actor="tester", request_id="first-confirmation",
        acknowledge_exclusions=True, state_version=before.stored.change_token, head_sha=before.stored.head_sha)
    with database.sessions() as session, session.begin():
        plan = session.scalar(select(ReviewPlanRecord).where(ReviewPlanRecord.review_run_id == run_id))
        plan.model_review_completed_at += timedelta(seconds=1)
    assert manager.details(run_id).stored.coverage_exclusions_acknowledged is False
    with pytest.raises(ReviewActionConflictError):
        manager.apply_action(run_id, ReviewAction.PUBLISH, actor="tester", request_id="stale-confirmation")
    publisher.assert_not_called()


def test_excluded_file_pages_are_complete_bounded_and_authorized(database):
    run_id = _completed_review(database, excluded=tuple(f"images/{i:02d}.webp" for i in range(12)), decision="binary")
    manager = ReviewManagementService(SqlAlchemyReviewManagementRepository(database.sessions))
    plan_id = manager.details(run_id).stored.review_plan_id
    page = manager.excluded_file_page(run_id, plan_id=plan_id)
    assert len(page.items) == 10 and page.next_cursor
    last = manager.excluded_file_page(run_id, plan_id=plan_id, cursor=page.next_cursor)
    assert len(last.items) == 2 and last.next_cursor is None
    assert len({item.file for item in (*page.items, *last.items)}) == 12
    with pytest.raises(ReviewNotFoundError):
        manager.excluded_file_page(run_id, plan_id="wrong-plan")
    with pytest.raises(ReviewNotFoundError):
        manager.excluded_file_page(run_id, plan_id=plan_id, scope=ResourceScope())
