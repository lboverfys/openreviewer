"""CI 隔离数据库中的费用、调度和人工处理契约；不调用外部模型。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import event, func, select

from apps.api.main import create_app
from apps.api.platform_services import PlatformServices
from domain.models import ReviewRequest
from domain.platform import (
    ModelChannelUnavailableError,
    MonthlyBudgetExceededError,
    PlatformConflictError,
    WorkItemCreate,
    WorkItemUpdate,
)
from domain.repository_policy import RepositoryPolicy
from domain.security import SafeError
from persistence.models import (
    FindingWorkItemRecord,
    ModelUsageRequestRecord,
    OutboxEventRecord,
    RepositoryUsageMonthRecord,
    ReviewFindingRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.platform_queries import PlatformQueries
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.review_profiles import ReviewProfileRepository
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from persistence.usage import SqlAlchemyUsageLedger
from persistence.usage_queries import UsageQueries
from persistence.work_items import WorkItemRepository
from services.auth import AuthService, UserCredential
from services.model_budget import ModelBudgetRequest
from services.rbac import AccessRole, ResourceScope
from services.reviews import ReviewService
from services.task_queue import TaskLeaseLostError
from services.team import RepositoryWrite, TeamService
from tests.integration.test_finding_pagination import _seed_review
from tests.integration.test_github_context_persistence import _submit
from tests.integration.test_management_api import database as database
from tests.support import (
    TEST_GITHUB_ACCESS_POLICY,
    TEST_HASHER,
    TEST_PASSWORD,
    TEST_USERNAME,
    make_auth_service,
)

NOW = datetime(2026, 9, 13, 1, tzinfo=UTC)
ALL = ResourceScope.unrestricted_scope()


def setup_ledger(database, budget=None):
    TeamService(database.sessions, TEST_USERNAME).save_repository(
        RepositoryWrite(
            repository="lboverfys/NiuMa",
            expected_revision=0,
            policy=RepositoryPolicy(monthly_budget_microusd=budget),
        ),
        TEST_USERNAME,
    )
    _submit(database, "ledger-test", "a" * 40)
    clock = [NOW]
    queue = SqlAlchemyReviewTaskQueue(database.sessions, clock=lambda: clock[0])
    lease = queue.claim_next("usage-worker", timedelta(hours=3))
    assert lease is not None
    return (
        queue,
        lease,
        clock,
        SqlAlchemyUsageLedger(database.sessions, clock=lambda: clock[0]),
    )


def request(cost=60, **kwargs):
    return ModelBudgetRequest(
        provider="openai",
        api_protocol="chat_completions",
        model="fixture-model",
        request_bytes=100,
        input_token_upper_bound=100,
        output_token_upper_bound=50,
        cost_upper_bound_microusd=cost,
        **kwargs,
    )


def settle(ledger, reservation, cost=20, status=200, uncertain=False):
    ledger.settle(
        reservation,
        input_tokens=10,
        output_tokens=5,
        estimated_cost_microusd=cost,
        response_status=status,
        duration_ms=10,
        uncertain=uncertain,
    )


def test_monthly_reservation_settlement_and_retry_are_atomic(database):
    _, lease, _, ledger = setup_ledger(database, budget=100)
    first = ledger.reserve(lease, "security", request())
    with pytest.raises(MonthlyBudgetExceededError):
        ledger.reserve(lease, "logic", request())
    settle(ledger, first)
    settle(ledger, first)
    second = ledger.reserve(lease, "logic", request())
    settle(ledger, second, cost=None, status=None, uncertain=True)
    with pytest.raises(MonthlyBudgetExceededError):
        ledger.reserve(lease, "logic", request())
    with database.sessions() as session:
        month = session.scalar(select(RepositoryUsageMonthRecord))
        assert month.request_count == 2 and month.estimated_cost_microusd == 20
        assert month.reserved_cost_microusd == 60 and month.unknown_count == 1
        assert month.uncertain_count == 1
        assert (
            session.scalar(select(func.count()).select_from(ModelUsageRequestRecord))
            == 2
        )


def test_unknown_price_and_expired_lease_never_send_or_reserve(database):
    queue, lease, clock, ledger = setup_ledger(database, budget=100)
    with pytest.raises(MonthlyBudgetExceededError):
        ledger.reserve(lease, "security", request(None))
    queue.pause_for_monthly_budget(
        lease, SafeError.from_exception(MonthlyBudgetExceededError())
    )
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, lease.task_id)
        assert task.workflow_status == "paused" and task.lease_owner is None
    with pytest.raises(TaskLeaseLostError):
        ledger.reserve(lease, "security", request())
    clock[0] += timedelta(days=1)
    with pytest.raises(TaskLeaseLostError):
        ledger.reserve(lease, "security", request())


def test_budget_warning_is_deduplicated_after_repeated_reservations(database):
    _, lease, _, ledger = setup_ledger(database, budget=100)
    settle(ledger, ledger.reserve(lease, "logic", request(90)), cost=0)
    settle(ledger, ledger.reserve(lease, "logic", request(90)), cost=0)
    with database.sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEventRecord)
                .where(OutboxEventRecord.event_type == "platform.budget.warning")
            )
            == 1
        )


def test_late_settlement_stays_in_original_month_and_lists_are_scoped(database):
    _, lease, clock, ledger = setup_ledger(database)
    old = ledger.reserve(lease, "security", request(None))
    clock[0] = NOW.replace(month=10)
    settle(ledger, old, cost=None)
    queries = UsageQueries(database.sessions)
    page = queries.months(ALL, "2026-09")
    assert page.items[0].unknown_count == 1 and page.items[0].budget_microusd is None
    assert not queries.months(ALL, "2026-10").items
    denied = ResourceScope(repositories=frozenset({"other/repo"}))
    assert not queries.months(denied, "2026-09").items
    assert not queries.requests(denied, page.items[0].id).items
    assert queries.breakdown(ALL, page.items[0].id)[0].unknown_count == 1


def test_provider_concurrency_circuit_and_single_recovery_probe(database):
    _, lease, clock, ledger = setup_ledger(database)
    bounded = request(connection_key="c" * 64)
    permits = [ledger.reserve(lease, "logic", bounded) for _ in range(3)]
    with pytest.raises(ModelChannelUnavailableError):
        ledger.reserve(lease, "logic", bounded)
    for permit in permits:
        settle(ledger, permit, cost=None, status=503, uncertain=True)
    for _ in range(2):
        permit = ledger.reserve(lease, "logic", bounded)
        settle(ledger, permit, cost=None, status=429, uncertain=True)
    with pytest.raises(ModelChannelUnavailableError):
        ledger.reserve(lease, "logic", bounded)
    clock[0] += timedelta(seconds=61)
    probe = ledger.reserve(lease, "logic", bounded)
    with pytest.raises(ModelChannelUnavailableError):
        ledger.reserve(lease, "logic", bounded)
    settle(ledger, probe)
    assert ledger.reserve(lease, "logic", bounded).id != probe.id


def test_repository_cap_skips_busy_repository_and_keeps_queue_available(database):
    team = TeamService(database.sessions, TEST_USERNAME)
    team.save_repository(
        RepositoryWrite(
            repository="lboverfys/NiuMa",
            expected_revision=0,
            policy=RepositoryPolicy(max_concurrent_reviews=1),
        ),
        TEST_USERNAME,
    )
    _submit(database, "repo-one", "a" * 40)
    _submit(database, "repo-two", "b" * 40)
    service = ReviewService(SqlAlchemyReviewRepository(database.sessions))
    other = service.submit(
        ReviewRequest(
            installation_id=10,
            repository_id=43,
            repository="lboverfys/Other",
            pull_request_number=1,
            head_sha="c" * 40,
        ),
        "other-repo",
    )
    queue = SqlAlchemyReviewTaskQueue(database.sessions)
    first = queue.claim_next("first", timedelta(minutes=5))
    second = queue.claim_next("second", timedelta(minutes=5))
    assert first is not None and second is not None
    assert second.review_run_id == other.review_run_id
    assert queue.claim_next("third", timedelta(minutes=5)) is None


def test_work_items_keep_human_state_and_require_revision(database):
    run_id = _seed_review(database, finding_count=1)
    with database.sessions() as session:
        finding_id = session.scalar(
            select(ReviewFindingRecord.id).where(
                ReviewFindingRecord.review_run_id == run_id
            )
        )
    store = WorkItemRepository(database.sessions, TEST_USERNAME)
    item = store.create(
        WorkItemCreate(finding_id=finding_id, assignee=TEST_USERNAME),
        TEST_USERNAME,
        ALL,
    )
    again = store.create(WorkItemCreate(finding_id=finding_id), TEST_USERNAME, ALL)
    assert again.id == item.id and again.assignee == TEST_USERNAME.casefold()
    with pytest.raises(ValueError):
        WorkItemUpdate(expected_revision=1, status="resolved")
    saved = store.update(
        item.id,
        WorkItemUpdate(
            expected_revision=1,
            status="resolved",
            note="已核对权限校验及修复提交",
            fix_pull_request_number=89,
            assignee=TEST_USERNAME,
        ),
        TEST_USERNAME,
        ALL,
    )
    with pytest.raises(PlatformConflictError):
        store.update(
            item.id,
            WorkItemUpdate(expected_revision=1, status="open"),
            TEST_USERNAME,
            ALL,
        )
    assert saved.status == "resolved" and saved.revision == 2
    assert (
        len(
            PlatformQueries(database.sessions)
            .audits(ResourceScope(repositories=frozenset({"lboverfys/NiuMa"})))
            .items
        )
        == 2
    )
    assert not PlatformQueries(database.sessions).audits(ResourceScope.deny_all()).items
    assert not store.list(ResourceScope.deny_all()).items
    assert store.approvals(ALL, TEST_USERNAME).items[0].due_at is None
    with database.sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(FindingWorkItemRecord)) == 1
        )


def test_usage_pages_and_work_pages_do_not_grow_queries_with_rows(database):
    _, lease, _, ledger = setup_ledger(database)
    for _ in range(15):
        settle(ledger, ledger.reserve(lease, "logic", request()))
    page = UsageQueries(database.sessions).months(ALL, "2026-09")
    statements = []

    def capture(_conn, _cursor, sql, *_args):
        if sql.lstrip().upper().startswith("SELECT"):
            statements.append(sql)

    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        first = UsageQueries(database.sessions).requests(ALL, page.items[0].id)
        second = UsageQueries(database.sessions).requests(
            ALL, page.items[0].id, cursor=first.next_cursor
        )
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)
    assert len(first.items) == 10 and len(second.items) == 5
    assert len(statements) == 2


def test_old_approval_owner_is_respected_without_inventing_deadline(database):
    run_id = _seed_review(database, finding_count=0)
    with database.sessions() as session:
        run = session.get(ReviewRunRecord, run_id)
        run.repository_policy = {
            "repository": run.repository,
            "revision": 1,
            "approver": "AnotherReviewer",
        }
        session.commit()
    store = WorkItemRepository(database.sessions, TEST_USERNAME)
    assert not store.approvals(ALL, TEST_USERNAME).items
    assigned = store.approvals(ALL, "anotherreviewer").items
    assert len(assigned) == 1 and assigned[0].assignee == "anotherreviewer"
    assert assigned[0].due_at is None


def test_platform_api_auth_scope_and_origin(database):
    services = PlatformServices(
        UsageQueries(database.sessions),
        WorkItemRepository(database.sessions, TEST_USERNAME),
        PlatformQueries(database.sessions),
        ReviewProfileRepository(database.sessions),
    )
    settings = make_auth_service().settings
    authentication = AuthService(
        replace(
            settings,
            additional_users=(
                UserCredential(
                    username="readonly",
                    password_hash=settings.password_hash,
                    role=AccessRole.VIEWER,
                    resource_scope=ResourceScope(
                        repositories=frozenset({"lboverfys/NiuMa"})
                    ),
                ),
            ),
        ),
        password_hasher=TEST_HASHER,
    )
    application = create_app(
        auth_service=authentication,
        platform_services=services,
        github_access_policy=TEST_GITHUB_ACCESS_POLICY,
    )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://testserver"
        ) as client:
            assert (await client.get("/api/v1/platform/usage")).status_code == 401
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
                )
            ).status_code == 200
            assert (await client.get("/api/v1/platform/usage?month=2026-09")).json()[
                "items"
            ] == []
            assert (
                await client.get("/api/v1/platform/usage?month=2026-99")
            ).status_code == 422
            assert (
                await client.post(
                    "/api/v1/platform/work-items",
                    headers={"Origin": "https://other.example"},
                    json={"finding_id": "missing"},
                )
            ).status_code == 403
            assert (
                await client.get("/api/v1/platform/work-items?limit=101")
            ).status_code == 422
            assert (await client.get("/api/v1/platform/diagnostics")).status_code == 200
            evidence = await client.get("/api/v1/platform/evidence")
            assert evidence.status_code == 200
            assert evidence.json()["evaluation_status"] == "awaiting_human_review"
            assert (await client.get("/api/v1/platform/evidence?dataset_id=missing")).status_code == 404
            assert (await client.post("/api/v1/platform/reviews/missing/static-report",
                headers={"Origin": "https://other.example"},
                json={"head_sha": "a" * 40, "head_sarif": "{}"})).status_code == 403
            await client.post("/api/v1/auth/logout")
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "readonly", "password": TEST_PASSWORD},
                )
            ).status_code == 200
            assert (await client.get("/api/v1/platform/usage")).status_code == 403
            assert (await client.get("/api/v1/platform/profiles")).status_code == 403
            assert (await client.get("/api/v1/platform/profiles/missing/quality")).status_code == 403
            assert (await client.post("/api/v1/platform/reviews/missing/static-report",
                json={"head_sha": "a" * 40, "head_sarif": "{}"})).status_code == 403
            assert (await client.get("/api/v1/platform/work-items")).status_code == 200
            assert (
                await client.post(
                    "/api/v1/platform/work-items", json={"finding_id": "missing"}
                )
            ).status_code == 403

    asyncio.run(exercise())
