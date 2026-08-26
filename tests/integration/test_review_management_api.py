"""任务详情、人工操作和 Finding 裁决接口回归测试。"""

import asyncio
from datetime import UTC, datetime
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
    Severity,
    VerificationStatus,
)
from domain.model_review import ModelTokenUsage
from domain.models import FindingLocation, ReviewFinding, ReviewRequest
from persistence.database import Database
from persistence.models import (
    Base,
    ModelCallRecord,
    OutboxEventRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.repositories import SqlAlchemyReviewRepository
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
            assert body["findings"][0]["verification_status"] == "verified"

    asyncio.run(exercise())
