"""任务详情、人工操作和 Finding 裁决接口回归测试。"""

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from sqlalchemy import event, select

from apps.api.main import create_app
from domain.enums import (
    ExecutionStatus,
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
from domain.models import ReviewRequest
from domain.security import ErrorCode, SafeApplicationError, SafeError
from persistence.dashboard import SqlAlchemyDashboardRepository
from persistence.database import Database
from persistence.models import (
    Base,
    FindingEvaluationRecord,
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
from persistence.review_management import SqlAlchemyReviewManagementRepository
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from services.dashboard import DashboardService
from services.review_management import (
    FindingDecision,
    ReviewAction,
    ReviewManagementService,
)
from services.reviews import ReviewService
from tests.support import (
    TEST_GITHUB_ACCESS_POLICY,
    TEST_PASSWORD,
    TEST_USERNAME,
    make_auth_service,
)


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
        github_access_policy=TEST_GITHUB_ACCESS_POLICY,
    )


def test_evaluation_gate_reads_only_recent_rows_in_one_statement(
    database: Database,
) -> None:
    """评测门禁按类别有界读取，避免历史表增长后扫描全仓库。"""

    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    with database.sessions() as session:
        session.add_all(
            [
                FindingEvaluationRecord(
                    finding_id=f"evaluation-{index:03d}",
                    repository_id=42,
                    category=FindingCategory.SECURITY.value,
                    severity=Severity.HIGH.value,
                    verdict=("valid" if index < 100 else "false_positive"),
                    adjudicated_at=now - timedelta(minutes=index),
                    adjudicated_by=TEST_USERNAME,
                    updated_at=now - timedelta(minutes=index),
                )
                for index in range(120)
            ]
        )
        session.commit()

        select_statements: list[str] = []

        def count_selects(
            _conn,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                select_statements.append(statement)

        event.listen(database.engine, "before_cursor_execute", count_selects)
        try:
            gates = SqlAlchemyReviewManagementRepository._load_evaluation_gates(
                session,
                42,
            )
        finally:
            event.remove(database.engine, "before_cursor_execute", count_selects)

    security = next(
        gate for gate in gates if gate.category == FindingCategory.SECURITY.value
    )
    assert security.sample_count == 100
    assert security.valid_count == 100
    assert security.false_positive_count == 0
    assert len(select_statements) == 1
    assert "UNION ALL" in select_statements[0].upper()


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
            initial_token = body["change_token"]
            assert len(initial_token) == 24
            unchanged = await client.get(
                f"/api/v1/reviews/{run_id}/change-token"
            )
            assert unchanged.status_code == 200
            assert unchanged.json() == {"change_token": initial_token}

            cancelled = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": "detail-cancel-001"},
                json={"action": "cancel"},
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["execution_status"] == "cancelled"

            changed = await client.get(
                f"/api/v1/reviews/{run_id}/change-token"
            )
            assert changed.status_code == 200
            assert changed.json()["change_token"] != initial_token

            after = await client.get(f"/api/v1/reviews/{run_id}")
            assert after.status_code == 200
            assert after.json()["phase"] == "cancelled"
            assert after.json()["events"][-1]["event_type"] == "review.manual.cancel"
            assert (
                after.json()["change_token"]
                == changed.json()["change_token"]
            )

    asyncio.run(exercise())


def test_superseded_review_only_offers_new_review(database: Database) -> None:
    """已被新提交替代的旧任务不能重新排队，只能创建新审查。"""

    submission = ReviewService(
        SqlAlchemyReviewRepository(database.sessions)
    ).submit(
        ReviewRequest(
            installation_id=10,
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=129,
            head_sha="b" * 40,
        ),
        "superseded-action-source",
    )
    with database.sessions() as session:
        run = session.get(ReviewRunRecord, submission.review_run_id)
        task = session.get(ReviewTaskRecord, submission.review_task_id)
        assert run is not None and task is not None
        run.execution_status = ExecutionStatus.SUPERSEDED.value
        run.workflow_status = ExecutionStatus.SUPERSEDED.value
        run.coverage_status = "stale"
        task.execution_status = ExecutionStatus.SUPERSEDED.value
        task.workflow_status = ExecutionStatus.SUPERSEDED.value
        session.commit()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application_for(database))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await login(client)
            response = await client.get(
                f"/api/v1/reviews/{submission.review_run_id}"
            )
            assert response.status_code == 200
            body = response.json()
            assert body["available_actions"] == ["new_review"]
            created = await client.post(
                f"/api/v1/reviews/{submission.review_run_id}/actions",
                headers={"Idempotency-Key": "superseded-new-review-001"},
                json={
                    "action": "new_review",
                    "retry_scope": "new_review",
                    "head_sha": "c" * 40,
                    "state_version": body["change_token"],
                },
            )
            assert created.status_code == 200
            assert created.json()["review_run_id"] != submission.review_run_id
            assert created.json()["execution_status"] == "queued"

    asyncio.run(exercise())


def test_action_idempotency_is_rechecked_after_target_lock(database: Database) -> None:
    """并发请求在锁等待后发现事件时，不应撞唯一键并返回 503。"""

    submission = ReviewService(
        SqlAlchemyReviewRepository(database.sessions)
    ).submit(
        ReviewRequest(
            installation_id=10,
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=127,
            head_sha="a" * 40,
        ),
        "action-lock-race-source",
    )
    request_id = "action-lock-race-001"
    action = ReviewAction.EXPEDITE
    action_digest = sha256(
        f"{submission.review_run_id}:{action.value}:{request_id}".encode()
    ).hexdigest()
    event_key = (
        f"review.action:{submission.review_run_id}:{action.value}:{action_digest}"
    )
    injected = False

    def inject_after_first_lookup(
        connection,
        _cursor,
        statement,
        parameters,
        _context,
        _executemany,
    ) -> None:
        nonlocal injected
        normalized_statement = statement.lstrip().lower()
        if (
            injected
            or not normalized_statement.startswith("select")
            or "outbox_events" not in normalized_statement
            or "event_key" not in normalized_statement
            or event_key not in str(parameters)
        ):
            return
        connection.execute(
            OutboxEventRecord.__table__.insert().values(
                id="event-action-lock-race",
                event_key=event_key,
                aggregate_type="review_run",
                aggregate_id=submission.review_run_id,
                event_type="review.manual.expedite",
                payload={"action": action.value, "target_stage": None},
                occurred_at=datetime.now(UTC),
                publish_attempts=0,
            )
        )
        injected = True

    event.listen(database.engine, "after_cursor_execute", inject_after_first_lookup)
    try:
        result = SqlAlchemyReviewManagementRepository(
            database.sessions
        ).apply_action(
            submission.review_run_id,
            action,
            actor="race-test",
            request_id=request_id,
        )
    finally:
        event.remove(database.engine, "after_cursor_execute", inject_after_first_lookup)

    assert injected is True
    assert result == (
        submission.review_run_id,
        submission.review_task_id,
        ExecutionStatus.QUEUED,
    )


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
        github_access_policy=TEST_GITHUB_ACCESS_POLICY,
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
        github_access_policy=TEST_GITHUB_ACCESS_POLICY,
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
        github_access_policy=TEST_GITHUB_ACCESS_POLICY,
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


def test_manual_publish_does_not_overwrite_a_replaced_attempt(
    database: Database,
) -> None:
    """外部发布返回后若令牌已被替换，旧尝试只能报告冲突。"""

    def publisher(details) -> None:
        with database.sessions() as session:
            run = session.get(ReviewRunRecord, details.review_run_id)
            task = session.scalar(
                select(ReviewTaskRecord).where(
                    ReviewTaskRecord.review_run_id == details.review_run_id
                )
            )
            assert run is not None
            assert task is not None
            # 模拟发布恢复流程已经启动了下一次尝试；真实流程会在短事务
            # 中写入新的令牌，旧回调随后必须被拒绝。
            run.publish_attempt_token = "replacement-attempt"
            session.commit()

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
        github_access_policy=TEST_GITHUB_ACCESS_POLICY,
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
                headers={"Idempotency-Key": "publish-token-source"},
                json={
                    "installation_id": 10,
                    "repository_id": 42,
                    "repository": "lboverfys/NiuMa",
                    "pull_request_number": 133,
                    "head_sha": "3" * 40,
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

            response = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": f"ui:publish-token:{run_id}"},
                json={"action": "publish"},
            )
            assert response.status_code == 409
            assert "冲突" in response.json()["detail"]

            detail = await client.get(f"/api/v1/reviews/{run_id}")
            assert detail.status_code == 200
            assert detail.json()["workflow_status"] == "publishing"
            assert not any(
                event["event_type"] == "review.manual.publish_completed"
                for event in detail.json()["events"]
            )

    asyncio.run(exercise())


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

            # 幂等键允许 200 字符；只在尾部不同的长键也必须创建不同的重跑，
            # 不能被旧实现截断成同一个数据库键。
            long_prefix = "x" * 190
            long_key_a = long_prefix + "a"
            long_key_b = long_prefix + "b"
            long_rerun_a = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": long_key_a},
                json={"action": "rerun"},
            )
            long_rerun_b = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": long_key_b},
                json={"action": "rerun"},
            )
            assert long_rerun_a.status_code == 200
            assert long_rerun_b.status_code == 200
            assert (
                long_rerun_a.json()["review_run_id"]
                != long_rerun_b.json()["review_run_id"]
            )

    asyncio.run(exercise())


def test_action_retry_scope_and_new_review_head_are_strictly_validated(
    database: Database,
) -> None:
    """不同动作不能混用重试范围，新建最新提交必须绑定 SHA。"""

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
                headers={"Idempotency-Key": "strict-action-source"},
                json={
                    "installation_id": 10,
                    "repository_id": 42,
                    "repository": "lboverfys/NiuMa",
                    "pull_request_number": 129,
                    "head_sha": "a" * 40,
                },
            )
            assert created.status_code == 202
            run_id = created.json()["review_run_id"]
            invalid_requests = (
                {"action": "retry", "retry_scope": "stage"},
                {"action": "rerun", "retry_scope": "stage"},
                {"action": "cancel", "retry_scope": "failed_node"},
                {
                    "action": "retry_failed_node",
                    "retry_scope": "failed_node",
                    "batch_number": 1,
                },
                {"action": "new_review"},
                {"action": "rerun", "retry_scope": "new_review"},
            )
            for index, payload in enumerate(invalid_requests, start=1):
                response = await client.post(
                    f"/api/v1/reviews/{run_id}/actions",
                    headers={"Idempotency-Key": f"strict-action-invalid-{index}"},
                    json=payload,
                )
                assert response.status_code == 409, response.text

            valid = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": "strict-action-valid"},
                json={
                    "action": "new_review",
                    "retry_scope": "new_review",
                    "head_sha": "b" * 40,
                },
            )
            assert valid.status_code == 200, valid.text
            assert valid.json()["review_run_id"] != run_id
            assert valid.json()["execution_status"] == "queued"

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
                ModelReviewBatchRecord(
                    id="batch-retry-001",
                    review_plan_id="plan-retry-001",
                    agent="security",
                    batch_number=1,
                    batch_count=1,
                    unit_keys=["d" * 64],
                    estimated_input_tokens=128,
                    status="failed",
                    attempt_count=3,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                    error_code="model_server_error",
                    error_message="模型服务暂时不可用",
                    error_details={"batch_retry_managed": True},
                ),
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

            with database.sessions() as session:
                assert session.scalars(
                    select(ModelReviewBatchRecord).where(
                        ModelReviewBatchRecord.review_plan_id == "plan-retry-001"
                    )
                ).all() == []

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
                execution_status="completed",
                workflow_status="awaiting_approval",
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
                execution_status="completed",
                workflow_status="awaiting_approval",
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
                verification_status=VerificationStatus.VERIFIED.value,
                rule_reference=None,
                reviewed_at=None,
                reviewed_by=None,
                created_at=now,
            )
        )
        session.commit()

    race_request_id = "finding-lock-race-001"
    race_decision = FindingDecision.VALID
    race_digest = sha256(
        f"{run_id}:{finding_id}:{race_decision.value}:{race_request_id}".encode()
    ).hexdigest()
    race_event_key = f"review.finding.decision:{finding_id}:{race_digest}"
    race_injected = False

    def inject_finding_event_after_first_lookup(
        connection,
        _cursor,
        statement,
        parameters,
        _context,
        _executemany,
    ) -> None:
        nonlocal race_injected
        normalized_statement = statement.lstrip().lower()
        if (
            race_injected
            or not normalized_statement.startswith("select")
            or "outbox_events" not in normalized_statement
            or "event_key" not in normalized_statement
            or race_event_key not in str(parameters)
        ):
            return
        connection.execute(
            OutboxEventRecord.__table__.insert().values(
                id="event-finding-lock-race",
                event_key=race_event_key,
                aggregate_type="review_run",
                aggregate_id=run_id,
                event_type="review.finding.decided",
                payload={
                    "finding_id": finding_id,
                    "decision": race_decision.value,
                    "actor": "race-test",
                },
                occurred_at=datetime.now(UTC),
                publish_attempts=0,
            )
        )
        race_injected = True

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await login(client)
            blocked_approval = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": "finding-approval-blocked-001"},
                json={"action": "approve"},
            )
            assert blocked_approval.status_code == 409

            raced = await client.post(
                f"/api/v1/reviews/{run_id}/findings/{finding_id}",
                headers={"Idempotency-Key": race_request_id},
                json={"decision": race_decision.value},
            )
            assert raced.status_code == 200
            assert raced.json()["findings"][0]["adjudication_status"] == "unreviewed"

            response = await client.post(
                f"/api/v1/reviews/{run_id}/findings/{finding_id}",
                headers={"Idempotency-Key": "finding-decision-001"},
                json={"decision": "valid"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["location_verified_finding_count"] == 1
            assert body["location_unverified_finding_count"] == 0
            assert body["valid_finding_count"] == 1
            assert body["unreviewed_finding_count"] == 0
            assert body["model_reasoning_tokens"] == 0
            assert body["findings"][0]["verification_status"] == "verified"
            assert (
                body["findings"][0]["location_verification_status"]
                == "verified"
            )
            assert (
                body["findings"][0]["evidence_verification_status"]
                == "unverified"
            )
            assert body["findings"][0]["adjudication_status"] == "valid"
            assert body["findings"][0]["head_sha"] == "c" * 40
            assert len(body["evaluation_gates"]) == len(FindingCategory)
            security_gate = next(
                gate
                for gate in body["evaluation_gates"]
                if gate["category"] == "security"
            )
            assert security_gate == {
                "category": "security",
                "sample_count": 1,
                "valid_count": 1,
                "false_positive_count": 0,
                "duplicate_count": 0,
                "out_of_scope_count": 0,
                "known_issue_count": 0,
                "rejected_count": 0,
                "high_severity_sample_count": 1,
                "high_severity_false_positive_count": 0,
                "high_severity_rejected_count": 0,
                "precision": 1.0,
                "high_severity_false_positive_rate": 0.0,
                "admitted": False,
                "reason": "insufficient_samples",
            }
            revised = await client.post(
                f"/api/v1/reviews/{run_id}/findings/{finding_id}",
                headers={"Idempotency-Key": "finding-decision-002"},
                json={"decision": "false_positive"},
            )
            assert revised.status_code == 200
            revised_body = revised.json()
            assert revised_body["location_verified_finding_count"] == 1
            assert revised_body["false_positive_finding_count"] == 1
            assert (
                revised_body["findings"][0]["adjudication_status"]
                == "false_positive"
            )
            revised_security_gate = next(
                gate
                for gate in revised_body["evaluation_gates"]
                if gate["category"] == "security"
            )
            assert revised_security_gate["sample_count"] == 1
            assert revised_security_gate["valid_count"] == 0
            assert revised_security_gate["false_positive_count"] == 1

            duplicate = await client.post(
                f"/api/v1/reviews/{run_id}/findings/{finding_id}",
                headers={"Idempotency-Key": "finding-decision-003"},
                json={"decision": "duplicate"},
            )
            assert duplicate.status_code == 200
            duplicate_gate = next(
                gate
                for gate in duplicate.json()["evaluation_gates"]
                if gate["category"] == "security"
            )
            assert duplicate_gate["sample_count"] == 1
            assert duplicate_gate["valid_count"] == 0
            assert duplicate_gate["false_positive_count"] == 0
            assert duplicate_gate["duplicate_count"] == 1
            assert duplicate_gate["rejected_count"] == 1
            assert duplicate_gate["high_severity_rejected_count"] == 1

            approved = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": "finding-approval-allowed-001"},
                json={"action": "approve"},
            )
            assert approved.status_code == 200
            assert approved.json()["workflow_status"] == "awaiting_publish"

    event.listen(
        database.engine,
        "after_cursor_execute",
        inject_finding_event_after_first_lookup,
    )
    try:
        asyncio.run(exercise())
    finally:
        event.remove(
            database.engine,
            "after_cursor_execute",
            inject_finding_event_after_first_lookup,
        )
    assert race_injected is True

    with database.sessions() as session:
        evaluation = session.get(FindingEvaluationRecord, finding_id)
        assert evaluation is not None
        assert evaluation.repository_id == 42
        assert evaluation.category == FindingCategory.SECURITY.value
        assert evaluation.severity == Severity.HIGH.value
        assert evaluation.verdict == "duplicate"
        assert evaluation.adjudicated_by == TEST_USERNAME
