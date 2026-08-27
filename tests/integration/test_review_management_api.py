"""任务详情、人工操作和 Finding 裁决接口回归测试。"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from apps.api.main import create_app
from domain.enums import (
    FindingCategory,
    LocationSide,
    ModelApiProtocol,
    ModelCallStatus,
    ModelProvider,
    PullRequestState,
    Severity,
    VerificationStatus,
)
from domain.github import PullRequestSnapshot
from domain.model_review import ModelTokenUsage
from domain.models import FindingLocation, ReviewFinding, ReviewRequest
from domain.security import ErrorCode, SafeApplicationError, SafeError
from persistence.database import Database
from persistence.models import (
    Base,
    GitHubInstallationRecord,
    ModelCallRecord,
    ModelReviewBatchRecord,
    OutboxEventRecord,
    PullRequestVersionRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from services.dashboard import DashboardService
from persistence.dashboard import SqlAlchemyDashboardRepository
from services.reviews import ReviewService
from services.review_management import ReviewManagementService
from persistence.review_management import SqlAlchemyReviewManagementRepository
from tests.support import TEST_PASSWORD, TEST_USERNAME, make_auth_service


@pytest.fixture
def database(tmp_path: Path):
    database = Database.connect(f"sqlite:///{(tmp_path / 'details.sqlite3').as_posix()}")
    Base.metadata.create_all(database.engine)
    try:
        yield database
    finally:
        database.dispose()


def application_for(database: Database):
    return create_app(
        ReviewService(SqlAlchemyReviewRepository(database.sessions)),
        auth_service=make_auth_service(),
        dashboard_service=DashboardService(
            SqlAlchemyDashboardRepository(database.sessions)
        ),
        review_management_service=ReviewManagementService(
            SqlAlchemyReviewManagementRepository(database.sessions)
        ),
    )


async def login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200


def test_detail_is_readable_and_cancel_is_audited(database: Database) -> None:
    application = application_for(database)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await login(client)
            created = await client.post(
                "/api/v1/reviews",
                headers={"Idempotency-Key": "detail-task-001"},
                json={
                    "installation_id": 10,
                    "repository_id": 42,
                    "repository": "lboverfys/NiuMa",
                    "pull_request_number": 128,
                    "head_sha": "a" * 40,
                },
            )
            assert created.status_code == 202
            run_id = created.json()["review_run_id"]

            detail = await client.get(f"/api/v1/reviews/{run_id}")
            assert detail.status_code == 200
            body = detail.json()
            assert body["current_stage"] == "context"
            assert body["phase"] == "queued"
            assert body["findings"] == []
            assert body["events"][0]["event_type"] == "review.requested"
            assert "cancel" in body["available_actions"]

            cancelled = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": "detail-cancel-001"},
                json={"action": "cancel"},
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["execution_status"] == "cancelled"

            after = await client.get(f"/api/v1/reviews/{run_id}")
            assert after.status_code == 200
            assert after.json()["phase"] == "cancelled"
            assert after.json()["events"][-1]["event_type"] == "review.manual.cancel"

    asyncio.run(exercise())


def test_historical_pull_request_identity_is_synced_once_without_rewriting_version(
    database: Database,
) -> None:
    """历史身份同步只补展示字段，不把 GitHub 当前 SHA 混入旧版本快照。"""

    calls = []
    current_head_sha = "f" * 40

    class IdentityLoader:
        def load_pull_request(self, target):
            calls.append(target)
            return PullRequestSnapshot(
                repository_id=42,
                repository="lboverfys/NiuMa",
                pull_request_number=128,
                author_login="pull-author",
                html_url="https://github.com/lboverfys/NiuMa/pull/128",
                head_repository="contributor/NiuMa",
                head_ref="feature/identity",
                base_repository="lboverfys/NiuMa",
                base_ref="main",
                base_sha="e" * 40,
                head_sha=current_head_sha,
                state=PullRequestState.OPEN,
                draft=False,
                title="GitHub 当前标题",
                changed_files=99,
                updated_at=datetime(2026, 8, 27, 6, 0, tzinfo=UTC),
            )

    application = create_app(
        ReviewService(SqlAlchemyReviewRepository(database.sessions)),
        auth_service=make_auth_service(),
        dashboard_service=DashboardService(
            SqlAlchemyDashboardRepository(database.sessions)
        ),
        review_management_service=ReviewManagementService(
            SqlAlchemyReviewManagementRepository(database.sessions)
        ),
        identity_loader=IdentityLoader(),
    )

    async def exercise() -> str:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await login(client)
            created = await client.post(
                "/api/v1/reviews",
                headers={"Idempotency-Key": "identity-sync-source"},
                json={
                    "installation_id": 10,
                    "repository_id": 42,
                    "repository": "lboverfys/NiuMa",
                    "pull_request_number": 128,
                    "head_sha": "a" * 40,
                },
            )
            run_id = created.json()["review_run_id"]
            response = await client.post(
                f"/api/v1/reviews/{run_id}/identity/sync",
                headers={"Idempotency-Key": f"ui:identity:{run_id}"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["pr_author_login"] == "pull-author"
            assert body["pr_html_url"].endswith("/pull/128")
            assert body["head_repository"] == "contributor/NiuMa"
            assert body["head_ref"] == "feature/identity"
            assert body["base_repository"] == "lboverfys/NiuMa"
            assert body["base_ref"] == "main"
            assert body["identity_fetched_at"] is not None

            repeated = await client.post(
                f"/api/v1/reviews/{run_id}/identity/sync",
                headers={"Idempotency-Key": f"ui:identity:{run_id}"},
            )
            assert repeated.status_code == 200
            assert repeated.json()["identity_fetched_at"] == body["identity_fetched_at"]
            return run_id

    run_id = asyncio.run(exercise())

    assert len(calls) == 1
    assert calls[0].head_sha == "a" * 40
    with database.sessions() as session:
        version = session.scalar(
            select(PullRequestVersionRecord).where(
                PullRequestVersionRecord.review_version_key
                == calls[0].review_version_key
            )
        )
        assert version is not None
        assert version.head_sha == "a" * 40
        assert version.base_sha is None
        assert version.title is None
        assert version.changed_files_count is None
        assert session.get(GitHubInstallationRecord, 10) is not None
        events = session.scalars(
            select(OutboxEventRecord).where(
                OutboxEventRecord.aggregate_id == run_id,
                OutboxEventRecord.event_type == "review.github.identity_synced",
            )
        ).all()
        assert len(events) == 1


def test_historical_identity_sync_reports_missing_configuration(
    database: Database,
) -> None:
    application = application_for(database)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await login(client)
            created = await client.post(
                "/api/v1/reviews",
                headers={"Idempotency-Key": "identity-sync-unavailable-source"},
                json={
                    "installation_id": 10,
                    "repository_id": 42,
                    "repository": "lboverfys/NiuMa",
                    "pull_request_number": 129,
                    "head_sha": "b" * 40,
                },
            )
            run_id = created.json()["review_run_id"]

            response = await client.post(
                f"/api/v1/reviews/{run_id}/identity/sync",
                headers={"Idempotency-Key": f"ui:identity:{run_id}"},
            )

            assert response.status_code == 503
            assert response.json()["detail"] == (
                "GitHub PR identity sync is not configured"
            )

    asyncio.run(exercise())


def test_historical_identity_sync_exposes_only_safe_github_error(
    database: Database,
) -> None:
    secret = "ghs_must-not-reach-the-browser"

    class FailingIdentityLoader:
        def load_pull_request(self, _target):
            raise SafeApplicationError(
                SafeError(
                    code=ErrorCode.GITHUB_PERMISSION_DENIED,
                    safe_message="GitHub App 无权读取该 Pull Request",
                    retryable=False,
                    details={"authorization": secret},
                )
            )

    application = create_app(
        ReviewService(SqlAlchemyReviewRepository(database.sessions)),
        auth_service=make_auth_service(),
        dashboard_service=DashboardService(
            SqlAlchemyDashboardRepository(database.sessions)
        ),
        review_management_service=ReviewManagementService(
            SqlAlchemyReviewManagementRepository(database.sessions)
        ),
        identity_loader=FailingIdentityLoader(),
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await login(client)
            created = await client.post(
                "/api/v1/reviews",
                headers={"Idempotency-Key": "identity-sync-forbidden-source"},
                json={
                    "installation_id": 10,
                    "repository_id": 42,
                    "repository": "lboverfys/NiuMa",
                    "pull_request_number": 130,
                    "head_sha": "c" * 40,
                },
            )
            run_id = created.json()["review_run_id"]

            response = await client.post(
                f"/api/v1/reviews/{run_id}/identity/sync",
                headers={"Idempotency-Key": f"ui:identity:{run_id}"},
            )

            assert response.status_code == 403
            assert response.json()["error"]["code"] == (
                ErrorCode.GITHUB_PERMISSION_DENIED.value
            )
            assert response.json()["error"]["message"] == (
                "GitHub App 无权读取该 Pull Request"
            )
            assert secret not in response.text

    asyncio.run(exercise())


def test_approve_action_returns_persisted_workflow_status(database: Database) -> None:
    """批准门不能被旧的 execution_status 兼容值覆盖。"""

    application = application_for(database)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await login(client)
            created = await client.post(
                "/api/v1/reviews",
                headers={"Idempotency-Key": "workflow-status-source"},
                json={
                    "installation_id": 10,
                    "repository_id": 42,
                    "repository": "lboverfys/NiuMa",
                    "pull_request_number": 131,
                    "head_sha": "1" * 40,
                },
            )
            assert created.status_code == 202
            run_id = created.json()["review_run_id"]
            with database.sessions() as session:
                run = session.get(ReviewRunRecord, run_id)
                task = session.scalar(
                    select(ReviewTaskRecord).where(
                        ReviewTaskRecord.review_run_id == run_id
                    )
                )
                assert run is not None
                assert task is not None
                # 模拟旧读模型仍保留 completed，但新版 DAG 已到批准节点。
                run.execution_status = "completed"
                task.execution_status = "completed"
                run.workflow_status = "awaiting_approval"
                task.workflow_status = "awaiting_approval"
                session.commit()

            approved = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": "workflow-approve-001"},
                json={"action": "approve"},
            )
            assert approved.status_code == 200
            assert approved.json()["execution_status"] == "completed"
            assert approved.json()["workflow_status"] == "awaiting_publish"

            repeated = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": "workflow-approve-001"},
                json={"action": "approve"},
            )
            assert repeated.status_code == 200
            assert repeated.json()["workflow_status"] == "awaiting_publish"

            detail = await client.get(f"/api/v1/reviews/{run_id}")
            workflow_events = [
                event
                for event in detail.json()["events"]
                if event["event_type"] in {
                    "review.workflow.approve",
                    "review.workflow.advance",
                }
            ]
            assert [event["payload"]["new_status"] for event in workflow_events] == [
                "approved",
                "awaiting_publish",
            ]

    asyncio.run(exercise())


def test_awaiting_publish_review_can_still_be_rejected(database: Database) -> None:
    """批准和发布是两次操作，尚未发布的结果仍允许人工驳回。"""

    application = application_for(database)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await login(client)
            created = await client.post(
                "/api/v1/reviews",
                headers={"Idempotency-Key": "reject-before-publish-source"},
                json={
                    "installation_id": 10,
                    "repository_id": 42,
                    "repository": "lboverfys/NiuMa",
                    "pull_request_number": 133,
                    "head_sha": "3" * 40,
                },
            )
            assert created.status_code == 202
            run_id = created.json()["review_run_id"]
            with database.sessions() as session:
                run = session.get(ReviewRunRecord, run_id)
                task = session.scalar(
                    select(ReviewTaskRecord).where(
                        ReviewTaskRecord.review_run_id == run_id
                    )
                )
                assert run is not None
                assert task is not None
                run.execution_status = "completed"
                task.execution_status = "completed"
                run.workflow_status = "awaiting_publish"
                task.workflow_status = "awaiting_publish"
                plan = ReviewPlanRecord(
                    id="plan-reject-retry-001",
                    review_run_id=run_id,
                    pull_request_version_id="version-reject-retry-001",
                    review_version_key=run.review_version_key,
                    head_sha=run.head_sha,
                    plan_fingerprint="a" * 64,
                    planner_version="test",
                    rules_complete=True,
                    incomplete_files=[],
                    rule_issues=[],
                    candidate_count=1,
                    requested_candidate_count=1,
                    rule_count=0,
                    unit_count=1,
                    file_count=1,
                    total_estimated_input_bytes=10,
                    model_review_completed_at=datetime.now(UTC),
                    created_at=datetime.now(UTC),
                )
                session.add(plan)
                for agent in ("security", "summary"):
                    session.add(
                        ModelReviewBatchRecord(
                            id=f"batch-reject-retry-{agent}",
                            review_plan_id=plan.id,
                            agent=agent,
                            batch_number=1,
                            batch_count=1,
                            unit_keys=["b" * 64],
                            estimated_input_tokens=10,
                            status="succeeded",
                            attempt_count=1,
                            available_at=datetime.now(UTC),
                            result={},
                            created_at=datetime.now(UTC),
                            updated_at=datetime.now(UTC),
                        )
                    )
                session.commit()

            before = await client.get(f"/api/v1/reviews/{run_id}")
            assert before.status_code == 200
            assert "reject" in before.json()["available_actions"]

            rejected = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": "reject-before-publish-001"},
                json={"action": "reject"},
            )
            assert rejected.status_code == 200
            assert rejected.json()["execution_status"] == "completed"
            assert rejected.json()["workflow_status"] == "rejected"

            detail = await client.get(f"/api/v1/reviews/{run_id}")
            assert detail.status_code == 200
            assert detail.json()["phase"] == "rejected"
            assert detail.json()["available_actions"] == ["retry_stage", "rerun"]
            assert detail.json()["events"][-1]["event_type"] == (
                "review.workflow.reject"
            )

            retried = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": "reject-retry-aggregate-001"},
                json={"action": "retry_stage", "target_stage": "aggregating"},
            )
            assert retried.status_code == 200
            assert retried.json()["execution_status"] == "ready_for_review"
            assert retried.json()["workflow_status"] == "aggregating"

            with database.sessions() as session:
                plan = session.get(ReviewPlanRecord, "plan-reject-retry-001")
                assert plan is not None
                assert plan.model_review_completed_at is None
                agents = session.scalars(
                    select(ModelReviewBatchRecord.agent).where(
                        ModelReviewBatchRecord.review_plan_id == plan.id
                    )
                ).all()
                assert agents == ["security"]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("target_stage", "expected_execution", "expected_agents", "plan_retained"),
    (
        ("ci", "queued", (), False),
        ("planning", "ready_for_review", (), False),
        ("agent_batches", "ready_for_review", (), True),
        ("aggregating", "ready_for_review", ("security",), True),
    ),
)
def test_rejected_review_restarts_from_the_selected_real_stage(
    database: Database,
    target_stage: str,
    expected_execution: str,
    expected_agents: tuple[str, ...],
    plan_retained: bool,
) -> None:
    application = application_for(database)
    now = datetime.now(UTC)
    submission = ReviewService(SqlAlchemyReviewRepository(database.sessions)).submit(
        ReviewRequest(
            installation_id=10,
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=140,
            head_sha="4" * 40,
        ),
        f"stage-retry-source-{target_stage}",
    )
    plan_id = f"plan-stage-retry-{target_stage}"
    with database.sessions() as session:
        run = session.get(ReviewRunRecord, submission.review_run_id)
        task = session.get(ReviewTaskRecord, submission.review_task_id)
        assert run is not None
        assert task is not None
        run.execution_status = "completed"
        task.execution_status = "completed"
        run.workflow_status = "rejected"
        task.workflow_status = "rejected"
        run.review_conclusion = "findings_present"
        session.add(
            ReviewPlanRecord(
                id=plan_id,
                review_run_id=run.id,
                pull_request_version_id=f"version-stage-retry-{target_stage}",
                review_version_key=run.review_version_key,
                head_sha=run.head_sha,
                plan_fingerprint="5" * 64,
                planner_version="test",
                rules_complete=True,
                incomplete_files=[],
                rule_issues=[],
                candidate_count=1,
                requested_candidate_count=1,
                rule_count=0,
                unit_count=1,
                file_count=1,
                total_estimated_input_bytes=10,
                model_review_completed_at=now,
                created_at=now,
            )
        )
        session.add(
            ModelCallRecord(
                id=f"call-stage-retry-{target_stage}",
                review_plan_id=plan_id,
                provider="openai",
                api_protocol="chat_completions",
                model="test-model",
                status="succeeded",
                prompt_version="test",
                request_fingerprint="6" * 64,
                provider_response_id=None,
                provider_request_id=None,
                response_status=200,
                duration_ms=1,
                input_tokens=1,
                output_tokens=1,
                cache_read_input_tokens=0,
                cache_write_input_tokens=0,
                reasoning_output_tokens=0,
                estimated_cost_microusd=None,
                finding_count=0,
                created_at=now,
            )
        )
        for agent in ("security", "summary"):
            session.add(
                ModelReviewBatchRecord(
                    id=f"batch-stage-retry-{target_stage}-{agent}",
                    review_plan_id=plan_id,
                    agent=agent,
                    batch_number=1,
                    batch_count=1,
                    unit_keys=["7" * 64],
                    estimated_input_tokens=10,
                    status="succeeded",
                    attempt_count=1,
                    available_at=now,
                    result={},
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await login(client)
            idempotency_key = f"stage-retry-{target_stage}-001"
            response = await client.post(
                f"/api/v1/reviews/{submission.review_run_id}/actions",
                headers={"Idempotency-Key": idempotency_key},
                json={"action": "retry_stage", "target_stage": target_stage},
            )
            assert response.status_code == 200
            assert response.json()["execution_status"] == expected_execution
            assert response.json()["workflow_status"] == target_stage

            repeated = await client.post(
                f"/api/v1/reviews/{submission.review_run_id}/actions",
                headers={"Idempotency-Key": idempotency_key},
                json={"action": "retry_stage", "target_stage": target_stage},
            )
            assert repeated.status_code == 200
            assert repeated.json() == response.json()

            conflict = await client.post(
                f"/api/v1/reviews/{submission.review_run_id}/actions",
                headers={"Idempotency-Key": idempotency_key},
                json={
                    "action": "retry_stage",
                    "target_stage": (
                        "planning" if target_stage != "planning" else "agent_batches"
                    ),
                },
            )
            assert conflict.status_code == 409

    asyncio.run(exercise())

    with database.sessions() as session:
        plan = session.get(ReviewPlanRecord, plan_id)
        assert (plan is not None) is plan_retained
        if plan is not None:
            assert plan.model_review_completed_at is None
        assert session.get(ModelCallRecord, f"call-stage-retry-{target_stage}") is None
        agents = tuple(
            session.scalars(
                select(ModelReviewBatchRecord.agent)
                .where(ModelReviewBatchRecord.review_plan_id == plan_id)
                .order_by(ModelReviewBatchRecord.agent)
            ).all()
        )
        assert agents == expected_agents
        run = session.get(ReviewRunRecord, submission.review_run_id)
        assert run is not None
        assert run.review_conclusion is None
        if not plan_retained:
            assert run.coverage_status == "unknown"

    lease = SqlAlchemyReviewTaskQueue(database.sessions).claim_next(
        f"stage-retry-worker-{target_stage}",
        timedelta(minutes=5),
    )
    assert lease is not None
    assert lease.claimed_from_status.value == expected_execution
    assert lease.review_plan_id == (plan_id if plan_retained else None)


def test_paused_approval_keeps_its_stage_and_only_resumes(database: Database) -> None:
    """人工门暂停后仍显示批准阶段，且不暴露无法执行的取消动作。"""

    application = application_for(database)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await login(client)
            created = await client.post(
                "/api/v1/reviews",
                headers={"Idempotency-Key": "pause-approval-source"},
                json={
                    "installation_id": 10,
                    "repository_id": 42,
                    "repository": "lboverfys/NiuMa",
                    "pull_request_number": 134,
                    "head_sha": "4" * 40,
                },
            )
            assert created.status_code == 202
            run_id = created.json()["review_run_id"]
            with database.sessions() as session:
                run = session.get(ReviewRunRecord, run_id)
                task = session.scalar(
                    select(ReviewTaskRecord).where(
                        ReviewTaskRecord.review_run_id == run_id
                    )
                )
                assert run is not None
                assert task is not None
                run.execution_status = "completed"
                task.execution_status = "completed"
                run.workflow_status = "awaiting_approval"
                task.workflow_status = "awaiting_approval"
                session.commit()

            paused = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": "pause-approval-001"},
                json={"action": "pause"},
            )
            assert paused.status_code == 200
            assert paused.json()["workflow_status"] == "paused"

            detail = await client.get(f"/api/v1/reviews/{run_id}")
            assert detail.status_code == 200
            assert detail.json()["current_stage"] == "approval"
            assert detail.json()["phase"] == "paused"
            assert detail.json()["available_actions"] == ["resume"]

            resumed = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": "resume-approval-001"},
                json={"action": "resume"},
            )
            assert resumed.status_code == 200
            assert resumed.json()["workflow_status"] == "awaiting_approval"

    asyncio.run(exercise())


def test_manual_publish_can_retry_after_external_failure_and_is_idempotent(
    database: Database,
) -> None:
    """GitHub 调用失败后回到待发布；同一幂等键可恢复且不重复成功调用。"""

    publish_calls: list[str] = []

    def publisher(details) -> None:
        publish_calls.append(details.review_run_id)
        if len(publish_calls) == 1:
            raise RuntimeError("temporary GitHub failure")

    application = create_app(
        ReviewService(SqlAlchemyReviewRepository(database.sessions)),
        auth_service=make_auth_service(),
        dashboard_service=DashboardService(
            SqlAlchemyDashboardRepository(database.sessions)
        ),
        review_management_service=ReviewManagementService(
            SqlAlchemyReviewManagementRepository(
                database.sessions,
                publisher=publisher,
            )
        ),
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await login(client)
            created = await client.post(
                "/api/v1/reviews",
                headers={"Idempotency-Key": "publish-retry-source"},
                json={
                    "installation_id": 10,
                    "repository_id": 42,
                    "repository": "lboverfys/NiuMa",
                    "pull_request_number": 132,
                    "head_sha": "2" * 40,
                },
            )
            run_id = created.json()["review_run_id"]
            with database.sessions() as session:
                run = session.get(ReviewRunRecord, run_id)
                task = session.scalar(
                    select(ReviewTaskRecord).where(
                        ReviewTaskRecord.review_run_id == run_id
                    )
                )
                assert run is not None
                assert task is not None
                run.execution_status = "completed"
                task.execution_status = "completed"
                run.workflow_status = "awaiting_publish"
                task.workflow_status = "awaiting_publish"
                session.commit()

            headers = {"Idempotency-Key": f"ui:publish:{run_id}"}
            first = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers=headers,
                json={"action": "publish"},
            )
            assert first.status_code == 503
            failed_detail = await client.get(f"/api/v1/reviews/{run_id}")
            assert failed_detail.json()["workflow_status"] == "awaiting_publish"

            second = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers=headers,
                json={"action": "publish"},
            )
            assert second.status_code == 200
            assert second.json()["workflow_status"] == "completed"

            repeated = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers=headers,
                json={"action": "publish"},
            )
            assert repeated.status_code == 200
            assert repeated.json()["workflow_status"] == "completed"

    asyncio.run(exercise())
    assert len(publish_calls) == 2
    assert len(set(publish_calls)) == 1


def test_rerun_creates_a_new_queued_run(database: Database) -> None:
    application = application_for(database)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await login(client)
            created = await client.post(
                "/api/v1/reviews",
                headers={"Idempotency-Key": "detail-rerun-source"},
                json={
                    "installation_id": 10,
                    "repository_id": 42,
                    "repository": "lboverfys/NiuMa",
                    "pull_request_number": 128,
                    "head_sha": "b" * 40,
                },
            )
            run_id = created.json()["review_run_id"]
            rerun = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": "detail-rerun-001"},
                json={"action": "rerun"},
            )
            assert rerun.status_code == 200
            assert rerun.json()["execution_status"] == "queued"
            assert rerun.json()["review_run_id"] != run_id

            repeated = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": "detail-rerun-001"},
                json={"action": "rerun"},
            )
            assert repeated.status_code == 200
            assert repeated.json()["review_run_id"] == rerun.json()["review_run_id"]

    asyncio.run(exercise())


def test_failed_planned_run_can_retry_and_rerun(database: Database) -> None:
    """失败且已有计划的任务可以重试，并可继续创建全新审查运行。"""

    application = application_for(database)
    now = datetime.now(UTC)

    with database.sessions() as session:
        # 先通过真实提交路径创建运行和任务，再模拟模型阶段失败并补上计划记录。
        submission = ReviewService(
            SqlAlchemyReviewRepository(database.sessions)
        ).submit(
            ReviewRequest(
                installation_id=10,
                repository_id=42,
                repository="lboverfys/NiuMa",
                pull_request_number=130,
                head_sha="f" * 40,
            ),
            "detail-retry-source",
        )
        run = session.get(ReviewRunRecord, submission.review_run_id)
        task = session.get(ReviewTaskRecord, submission.review_task_id)
        assert run is not None
        assert task is not None
        run.execution_status = "failed"
        task.execution_status = "failed"
        task.model_attempt_count = 1
        task.last_error = "model output truncated"
        task.last_error_code = "model_output_truncated"
        task.last_error_retryable = True
        session.add(
            ReviewPlanRecord(
                id="plan-retry-001",
                review_run_id=run.id,
                pull_request_version_id="version-retry-missing",
                review_version_key=run.review_version_key,
                head_sha=run.head_sha,
                plan_fingerprint="1" * 64,
                planner_version="test",
                rules_complete=True,
                incomplete_files=[],
                rule_issues=[],
                candidate_count=1,
                requested_candidate_count=1,
                rule_count=0,
                unit_count=1,
                file_count=1,
                total_estimated_input_bytes=10,
                model_review_completed_at=None,
                created_at=now,
            )
        )
        session.add_all(
            [
                OutboxEventRecord(
                    id="event-old-model-running",
                    event_key=(
                        f"review.task.running:{task.id}:model-review:1"
                    ),
                    aggregate_type="review_run",
                    aggregate_id=run.id,
                    event_type="review.task.running",
                    payload={},
                    occurred_at=now,
                    publish_attempts=0,
                ),
                OutboxEventRecord(
                    id="event-old-model-failed",
                    event_key=(
                        f"review.task.failed:{task.id}:failed:model-attempt-1"
                    ),
                    aggregate_type="review_run",
                    aggregate_id=run.id,
                    event_type="review.task.failed",
                    payload={},
                    occurred_at=now,
                    publish_attempts=0,
                ),
            ]
        )
        session.commit()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await login(client)
            retried = await client.post(
                f"/api/v1/reviews/{submission.review_run_id}/actions",
                headers={"Idempotency-Key": "detail-retry-001"},
                json={"action": "retry"},
            )
            assert retried.status_code == 200
            assert retried.json()["review_run_id"] == submission.review_run_id
            assert retried.json()["execution_status"] == "ready_for_review"

            queue = SqlAlchemyReviewTaskQueue(database.sessions)
            lease = queue.claim_next("retry-regression-worker", timedelta(minutes=5))
            assert lease is not None
            assert lease.review_run_id == submission.review_run_id
            queue.retry_or_fail(
                lease,
                SafeError(
                    code=ErrorCode.MODEL_OUTPUT_TRUNCATED,
                    safe_message="model output truncated",
                    retryable=False,
                ),
            )

            rerun = await client.post(
                f"/api/v1/reviews/{submission.review_run_id}/actions",
                headers={"Idempotency-Key": "detail-rerun-after-retry-001"},
                json={"action": "rerun"},
            )
            assert rerun.status_code == 200
            assert rerun.json()["execution_status"] == "queued"
            assert rerun.json()["review_run_id"] != submission.review_run_id

    asyncio.run(exercise())


def test_finding_decision_is_visible_in_detail(database: Database) -> None:
    application = application_for(database)
    run_id = "run-finding-001"
    plan_id = "plan-finding-001"
    call_id = "call-finding-001"
    finding_id = "finding-finding-001"
    now = datetime.now(UTC)
    with database.sessions() as session:
        from persistence.models import ReviewRunRecord

        session.add(
            ReviewRunRecord(
                id=run_id,
                review_version_key="42:128:" + "c" * 40,
                installation_id=10,
                repository_id=42,
                repository="lboverfys/NiuMa",
                pull_request_number=128,
                head_sha="c" * 40,
                execution_status="ready_for_review",
                review_conclusion=None,
                coverage_status="complete",
                idempotency_key="finding-source",
                request_fingerprint="d" * 64,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ReviewTaskRecord(
                id="task-finding-001",
                review_run_id=run_id,
                execution_status="ready_for_review",
                priority=100,
                attempt_count=1,
                model_attempt_count=1,
                max_attempts=3,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ReviewPlanRecord(
                id=plan_id,
                review_run_id=run_id,
                pull_request_version_id="version-missing",
                review_version_key="42:128:" + "c" * 40,
                head_sha="c" * 40,
                plan_fingerprint="e" * 64,
                planner_version="test",
                rules_complete=True,
                incomplete_files=[],
                rule_issues=[],
                candidate_count=1,
                requested_candidate_count=1,
                rule_count=0,
                unit_count=1,
                file_count=1,
                total_estimated_input_bytes=10,
                model_review_completed_at=now,
                created_at=now,
            )
        )
        session.add(
            ModelCallRecord(
                id=call_id,
                review_plan_id=plan_id,
                configuration_revision=1,
                provider=ModelProvider.OPENAI.value,
                api_protocol=ModelApiProtocol.RESPONSES.value,
                model="test-model",
                status=ModelCallStatus.SUCCEEDED.value,
                prompt_version="test",
                request_fingerprint="f" * 64,
                provider_response_id=None,
                provider_request_id=None,
                response_status=200,
                duration_ms=100,
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=0,
                cache_write_input_tokens=0,
                reasoning_output_tokens=0,
                estimated_cost_microusd=0,
                finding_count=1,
                created_at=now,
            )
        )
        session.add(
            ReviewFindingRecord(
                id=finding_id,
                review_run_id=run_id,
                review_plan_id=plan_id,
                model_call_id=call_id,
                source_unit_key="a" * 64,
                fingerprint="1" * 64,
                head_sha="c" * 40,
                severity=Severity.HIGH.value,
                category=FindingCategory.SECURITY.value,
                location_file="src/app.py",
                location_blob_sha="2" * 40,
                location_start_line=4,
                location_end_line=4,
                location_side=LocationSide.RIGHT.value,
                location_in_diff=True,
                location_symbol=None,
                title="测试问题",
                evidence="证据",
                impact="影响",
                suggestion="建议",
                required_test=None,
                confidence=0.9,
                verification_status=VerificationStatus.UNVERIFIED.value,
                rule_reference=None,
                reviewed_at=None,
                reviewed_by=None,
                created_at=now,
            )
        )
        session.commit()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await login(client)
            response = await client.post(
                f"/api/v1/reviews/{run_id}/findings/{finding_id}",
                headers={"Idempotency-Key": "finding-decision-001"},
                json={"decision": "verified"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["verified_finding_count"] == 1
            assert body["unverified_finding_count"] == 0
            assert body["model_reasoning_tokens"] == 0
            assert body["findings"][0]["verification_status"] == "verified"

    asyncio.run(exercise())
