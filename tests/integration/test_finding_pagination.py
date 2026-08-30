"""Finding 详情分页、全量统计和发布上限回归测试。"""

import asyncio
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import event

from apps.api.main import create_app
from domain.enums import (
    FindingCategory,
    ModelApiProtocol,
    ModelCallStatus,
    ModelProvider,
    Severity,
    VerificationStatus,
)
from persistence.dashboard import SqlAlchemyDashboardRepository
from persistence.database import Database
from persistence.models import (
    Base,
    ModelCallRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.review_management import SqlAlchemyReviewManagementRepository
from services.dashboard import DashboardService
from services.review_management import ReviewManagementService
from services.reviews import ReviewService
from tests.support import (
    TEST_GITHUB_ACCESS_POLICY,
    TEST_PASSWORD,
    TEST_USERNAME,
    make_auth_service,
)

ADJUDICATIONS = (
    "unreviewed",
    "valid",
    "false_positive",
    "duplicate",
    "out_of_scope",
    "known_issue",
)
LIFECYCLES = ("new", "still_present", "reintroduced")
VERIFICATIONS = (
    VerificationStatus.UNVERIFIED.value,
    VerificationStatus.VERIFIED.value,
    VerificationStatus.REJECTED.value,
)


@pytest.fixture
def database(tmp_path: Path):
    database = Database.connect(
        f"sqlite:///{(tmp_path / 'finding-pages.sqlite3').as_posix()}"
    )
    Base.metadata.create_all(database.engine)
    try:
        yield database
    finally:
        database.dispose()


def _seed_review(
    database: Database,
    *,
    finding_count: int,
    workflow_status: str = "awaiting_approval",
    all_valid: bool = False,
    finding_id_prefix: str = "",
) -> str:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    suffix = f"{finding_count}-{workflow_status}"
    run_id = f"run-page-{suffix}"
    plan_id = f"plan-page-{suffix}"
    call_id = f"call-page-{suffix}"
    head_sha = "a" * 40
    with database.sessions() as session:
        session.add(
            ReviewRunRecord(
                id=run_id,
                review_version_key=f"42:88:{head_sha}",
                installation_id=10,
                repository_id=42,
                repository="lboverfys/NiuMa",
                pull_request_number=88,
                head_sha=head_sha,
                execution_status="completed",
                workflow_status=workflow_status,
                review_conclusion="findings_present",
                coverage_status="complete",
                idempotency_key=f"seed-{suffix}",
                request_fingerprint="b" * 64,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ReviewTaskRecord(
                id=f"task-page-{suffix}",
                review_run_id=run_id,
                execution_status="completed",
                workflow_status=workflow_status,
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
                pull_request_version_id=f"version-page-{suffix}",
                review_version_key=f"42:88:{head_sha}",
                head_sha=head_sha,
                plan_fingerprint="c" * 64,
                planner_version="test",
                rules_complete=True,
                incomplete_files=[],
                rule_issues=[],
                candidate_count=finding_count,
                requested_candidate_count=finding_count,
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
                request_fingerprint="d" * 64,
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
                finding_count=finding_count,
                created_at=now,
            )
        )
        session.add_all(
            ReviewFindingRecord(
                id=f"{finding_id_prefix}finding-{index:06d}",
                review_run_id=run_id,
                review_plan_id=plan_id,
                model_call_id=call_id,
                source_unit_key="e" * 64,
                fingerprint=f"{index:064x}",
                head_sha=head_sha,
                severity=Severity.HIGH.value,
                category=FindingCategory.SECURITY.value,
                location_file=None,
                location_blob_sha=None,
                location_start_line=None,
                location_end_line=None,
                location_side=None,
                location_in_diff=False,
                location_symbol=None,
                title=f"测试问题 {index}",
                evidence="证据",
                impact="影响",
                suggestion="建议",
                required_test=None,
                confidence=0.9,
                verification_status=VERIFICATIONS[index % len(VERIFICATIONS)],
                adjudication_status=(
                    "valid"
                    if all_valid
                    else ADJUDICATIONS[index % len(ADJUDICATIONS)]
                ),
                lifecycle_status=LIFECYCLES[index % len(LIFECYCLES)],
                occurrence_count=1,
                previous_review_run_id=None,
                rule_reference=None,
                reviewed_at=None,
                reviewed_by=None,
                created_at=now,
            )
            for index in range(finding_count)
        )
        session.commit()
    return run_id


def _application(database: Database, *, publisher=None):
    return create_app(
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


async def _login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200


def test_detail_pages_findings_and_uses_full_run_counts(database: Database) -> None:
    run_id = _seed_review(database, finding_count=125)
    application = _application(database)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await _login(client)
            cursor = None
            finding_ids: list[str] = []
            for expected_page_size in (40, 40, 40, 5):
                response = await client.get(
                    f"/api/v1/reviews/{run_id}",
                    params={
                        "finding_limit": 40,
                        **({"finding_cursor": cursor} if cursor else {}),
                    },
                )
                assert response.status_code == 200
                body = response.json()
                assert len(body["findings"]) == expected_page_size
                assert body["finding_total_count"] == 125
                assert body["valid_finding_count"] == Counter(
                    ADJUDICATIONS[index % len(ADJUDICATIONS)]
                    for index in range(125)
                )["valid"]
                assert body["location_verified_finding_count"] == Counter(
                    VERIFICATIONS[index % len(VERIFICATIONS)]
                    for index in range(125)
                )[VerificationStatus.VERIFIED.value]
                finding_ids.extend(item["id"] for item in body["findings"])
                cursor = body["finding_next_cursor"]

            assert cursor is None
            assert finding_ids == [f"finding-{index:06d}" for index in range(125)]
            assert len(finding_ids) == len(set(finding_ids))

            invalid = await client.get(
                f"/api/v1/reviews/{run_id}",
                params={"finding_cursor": "***"},
            )
            assert invalid.status_code == 422
            assert invalid.json()["detail"] == "finding cursor is invalid"

    asyncio.run(exercise())


def test_detail_query_count_is_constant_for_large_finding_page(database: Database) -> None:
    run_id = _seed_review(database, finding_count=125)
    select_count = 0

    def count_selects(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(database.engine, "before_cursor_execute", count_selects)
    try:
        details = SqlAlchemyReviewManagementRepository(database.sessions).get(
            run_id,
            finding_limit=100,
        )
    finally:
        event.remove(database.engine, "before_cursor_execute", count_selects)

    assert len(details.findings) == 100
    assert details.finding_has_more is True
    assert details.finding_counts.total == 125
    assert select_count <= 7


def test_dashboard_query_count_is_constant_for_page_size(database: Database) -> None:
    """Dashboard 读取多条运行时不应按行查询 Finding。"""

    for finding_count in (1, 2, 3):
        _seed_review(
            database,
            finding_count=finding_count,
            finding_id_prefix=f"run-{finding_count}-",
        )

    repository = SqlAlchemyDashboardRepository(database.sessions)
    select_counts: list[int] = []
    current_count = 0

    def count_selects(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal current_count
        if statement.lstrip().upper().startswith("SELECT"):
            current_count += 1

    event.listen(database.engine, "before_cursor_execute", count_selects)
    try:
        first_page = repository.load(limit=1)
        select_counts.append(current_count)
        current_count = 0
        full_page = repository.load(limit=3)
        select_counts.append(current_count)
    finally:
        event.remove(database.engine, "before_cursor_execute", count_selects)

    assert len(first_page.recent_reviews) == 1
    assert len(full_page.recent_reviews) == 3
    assert [item.finding_count for item in full_page.recent_reviews] == [3, 2, 1]
    # 统计、当前页、批量 Finding 指标、Worker 心跳各一条；页大小不改变查询数。
    assert select_counts == [4, 4]


@pytest.mark.parametrize(
    ("finding_count", "expected_status", "expected_calls"),
    ((200, 200, 1), (201, 409, 0)),
)
def test_publish_has_an_explicit_two_hundred_valid_finding_limit(
    database: Database,
    finding_count: int,
    expected_status: int,
    expected_calls: int,
) -> None:
    run_id = _seed_review(
        database,
        finding_count=finding_count,
        workflow_status="awaiting_publish",
        all_valid=True,
    )
    published_sizes: list[int] = []

    def publisher(details) -> None:
        published_sizes.append(len(details.findings))

    application = _application(database, publisher=publisher)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await _login(client)
            response = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": f"publish-{finding_count}"},
                json={"action": "publish"},
            )
            assert response.status_code == expected_status
            if finding_count == 201:
                assert "超过 200 条" in response.json()["detail"]

    asyncio.run(exercise())
    assert len(published_sizes) == expected_calls
    if expected_calls:
        assert published_sizes == [200]
    with database.sessions() as session:
        run = session.get(ReviewRunRecord, run_id)
        assert run is not None
        assert run.workflow_status == (
            "completed" if finding_count == 200 else "awaiting_publish"
        )
