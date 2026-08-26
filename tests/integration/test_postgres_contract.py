import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from domain.enums import (
    ChangedFileStatus,
    CiState,
    ExecutionStatus,
    FindingCategory,
    LocationSide,
    ModelApiProtocol,
    ModelCallStatus,
    ModelProvider,
    PatchState,
    PullRequestAction,
    PullRequestState,
    Severity,
)
from domain.github import (
    CiSnapshot,
    GitHubReviewContext,
    PullRequestFile,
    PullRequestSnapshot,
)
from domain.models import PullRequestWebhook, ReviewRequest
from domain.model_review import (
    ModelFindingCandidate,
    ModelFindingLocation,
    ModelReviewOutput,
    ModelReviewResult,
    ModelTokenUsage,
    materialize_findings,
)
from domain.review_planning import RepositoryRule, RepositoryRulesSnapshot
from persistence.database import Database
from persistence.models import (
    Base,
    GitHubWebhookDeliveryRecord,
    ModelCallRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.review_management import SqlAlchemyReviewManagementRepository
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from persistence.webhooks import SqlAlchemyGitHubWebhookRepository
from services.reviews import ReviewService
from services.review_management import ReviewAction
from services.review_planning import DeterministicReviewPlanner


@pytest.fixture(scope="module")
def postgres_database():
    raw_url = os.environ.get("OPENREVIEWER_TEST_POSTGRES_URL", "").strip()
    if not raw_url:
        pytest.skip("未配置隔离的 PostgreSQL 契约测试数据库")
    url = make_url(raw_url)
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "openreviewer_test":
        raise RuntimeError(
            "PostgreSQL 契约测试只允许使用本机 openreviewer_test 数据库"
        )

    database = Database.connect(url)
    try:
        Base.metadata.drop_all(database.engine)
        with database.engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        configuration = Config("alembic.ini")
        configuration.set_main_option(
            "sqlalchemy.url", url.render_as_string(hide_password=False)
        )
        command.upgrade(configuration, "head")
        command.check(configuration)
        yield database
    finally:
        Base.metadata.drop_all(database.engine)
        with database.engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        database.dispose()


def test_postgres_migrations_and_skip_locked_claim(postgres_database: Database) -> None:
    submission = ReviewService(
        SqlAlchemyReviewRepository(postgres_database.sessions)
    ).submit(
        ReviewRequest(
            installation_id=10,
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=128,
            head_sha="a" * 40,
        ),
        "postgres-lock-contract",
    )
    queue = SqlAlchemyReviewTaskQueue(postgres_database.sessions)

    with postgres_database.sessions() as blocker:
        transaction = blocker.begin()
        locked_task = blocker.scalar(
            select(ReviewTaskRecord)
            .where(ReviewTaskRecord.id == submission.review_task_id)
            .with_for_update()
        )
        assert locked_task is not None
        assert queue.claim_next("worker-2", timedelta(seconds=30)) is None
        transaction.rollback()

    lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert lease is not None
    assert lease.task_id == submission.review_task_id
    with postgres_database.sessions() as session:
        task = session.get(ReviewTaskRecord, submission.review_task_id)
        assert task.execution_status == ExecutionStatus.RUNNING.value


def test_postgres_manual_retry_handles_run_without_plan(
    postgres_database: Database,
) -> None:
    """人工重试不能因可选 Review Plan 的行锁语义而失败。"""

    submission = ReviewService(
        SqlAlchemyReviewRepository(postgres_database.sessions)
    ).submit(
        ReviewRequest(
            installation_id=11,
            repository_id=44,
            repository="lboverfys/NiuMa",
            pull_request_number=131,
            head_sha="f" * 40,
        ),
        "postgres-manual-retry-source",
    )
    with postgres_database.sessions() as session:
        run = session.get(ReviewRunRecord, submission.review_run_id)
        task = session.get(ReviewTaskRecord, submission.review_task_id)
        assert run is not None
        assert task is not None
        run.execution_status = ExecutionStatus.FAILED.value
        task.execution_status = ExecutionStatus.FAILED.value
        task.last_error_code = "model_output_truncated"
        task.last_error_retryable = True
        session.commit()

    result = SqlAlchemyReviewManagementRepository(
        postgres_database.sessions
    ).apply_action(
        submission.review_run_id,
        ReviewAction.RETRY,
        actor="postgres-contract",
        request_id="postgres-manual-retry-action",
    )
    assert result == (
        submission.review_run_id,
        submission.review_task_id,
        ExecutionStatus.QUEUED,
    )


def test_postgres_webhook_creation_respects_foreign_keys(
    postgres_database: Database,
) -> None:
    repository = SqlAlchemyGitHubWebhookRepository(postgres_database.sessions)
    event = PullRequestWebhook(
        action=PullRequestAction.OPENED,
        delivery_id="postgres-delivery-001",
        installation_id=20,
        repository_id=43,
        repository="lboverfys/NiuMa",
        pull_request_number=129,
        head_sha="b" * 40,
    )

    created = repository.create_or_get(event, "c" * 64)
    repeated = repository.create_or_get(event, "c" * 64)

    assert created.created is True
    assert repeated.created is False
    assert repeated.review_run_id == created.review_run_id
    assert repeated.review_task_id == created.review_task_id
    with postgres_database.sessions() as session:
        persisted = session.execute(
            select(
                GitHubWebhookDeliveryRecord.delivery_id,
                ReviewRunRecord.id,
                ReviewTaskRecord.id,
            )
            .join(
                ReviewRunRecord,
                ReviewRunRecord.id == GitHubWebhookDeliveryRecord.review_run_id,
            )
            .join(
                ReviewTaskRecord,
                ReviewTaskRecord.id == GitHubWebhookDeliveryRecord.review_task_id,
            )
            .where(
                GitHubWebhookDeliveryRecord.delivery_id
                == "postgres-delivery-001"
            )
        ).one()
    assert persisted == (
        "postgres-delivery-001",
        created.review_run_id,
        created.review_task_id,
    )


def test_postgres_review_plan_is_claimed_and_saved_atomically(
    postgres_database: Database,
) -> None:
    """验证 PostgreSQL 行锁、JSON 快照、批量子表和 Outbox 同事务落库。"""

    now = datetime.now(UTC)
    submission = ReviewService(
        SqlAlchemyReviewRepository(postgres_database.sessions)
    ).submit(
        ReviewRequest(
            installation_id=30,
            repository_id=77,
            repository="lboverfys/ReviewPlanContract",
            pull_request_number=501,
            head_sha="d" * 40,
        ),
        "postgres-review-plan-contract",
    )
    with postgres_database.sessions() as session:
        task = session.get(ReviewTaskRecord, submission.review_task_id)
        assert task is not None
        task.priority = 30_000
        session.commit()

    queue = SqlAlchemyReviewTaskQueue(postgres_database.sessions)
    context_lease = queue.claim_next("worker-plan-pg", timedelta(seconds=30))
    assert context_lease is not None
    assert context_lease.task_id == submission.review_task_id
    changed_file = PullRequestFile(
        path="src/contract.py",
        status=ChangedFileStatus.MODIFIED,
        blob_sha="e" * 40,
        additions=1,
        deletions=1,
        changes=2,
        patch_state=PatchState.AVAILABLE,
        patch="@@ -1 +1 @@\n-old\n+new\n",
    )
    assert queue.store_github_context(
        context_lease,
        GitHubReviewContext(
            pull_request=PullRequestSnapshot(
                repository_id=77,
                repository="lboverfys/ReviewPlanContract",
                pull_request_number=501,
                base_sha="b" * 40,
                head_sha="d" * 40,
                state=PullRequestState.OPEN,
                draft=False,
                title="PostgreSQL plan contract",
                changed_files=1,
                updated_at=now,
            ),
            files=(changed_file,),
            files_complete=True,
            diff_complete=True,
            ci=CiSnapshot(
                head_sha="d" * 40,
                state=CiState.SUCCESS,
                checks=(),
                complete=True,
                checked_at=now,
            ),
        ),
        ci_poll_interval=timedelta(seconds=30),
        ci_wait_timeout=timedelta(hours=1),
    ) is ExecutionStatus.READY_FOR_REVIEW

    plan_lease = queue.claim_next("worker-plan-pg", timedelta(seconds=30))
    assert plan_lease is not None
    assert plan_lease.claimed_from_status is ExecutionStatus.READY_FOR_REVIEW
    planning_input = queue.load_planning_input(plan_lease)
    rule_content = "# PostgreSQL contract\n"
    encoded_rule = rule_content.encode("utf-8")
    rules = RepositoryRulesSnapshot(
        repository_id=77,
        repository="lboverfys/ReviewPlanContract",
        head_sha="d" * 40,
        rules=(
            RepositoryRule(
                path="AGENTS.md",
                scope=None,
                blob_sha="f" * 40,
                content=rule_content,
                content_sha256=sha256(encoded_rule).hexdigest(),
                byte_size=len(encoded_rule),
            ),
        ),
        incomplete_files=(),
        issues=(),
        candidate_count=1,
        requested_candidate_count=1,
    )
    plan = DeterministicReviewPlanner().plan(
        planning_input.target,
        planning_input.files,
        rules,
    )
    stored = queue.store_review_plan(plan_lease, rules, plan)

    assert stored.created is True
    assert stored.execution_status is ExecutionStatus.READY_FOR_REVIEW
    model_lease = queue.claim_next("worker-plan-pg", timedelta(seconds=30))
    assert model_lease is not None
    assert model_lease.review_plan_id == stored.plan_id
    model_input = queue.load_model_review_input(model_lease)
    model_output = ModelReviewOutput(
        findings=(
            ModelFindingCandidate(
                unit_key=model_input.units[0].unit_key,
                severity=Severity.MEDIUM,
                category=FindingCategory.RELIABILITY,
                location=ModelFindingLocation(
                    file="src/contract.py",
                    start_line=1,
                    end_line=1,
                    side=LocationSide.RIGHT,
                    symbol=None,
                ),
                title="错误路径缺少恢复处理",
                evidence="新增路径直接返回且没有恢复状态。",
                impact="瞬时失败可能让任务永久停留。",
                suggestion="保存恢复状态并让 Worker 可重试。",
                required_test=None,
                confidence=0.9,
                rule_reference="AGENTS.md",
            ),
        )
    )
    model_result = ModelReviewResult(
        provider=ModelProvider.ANTHROPIC,
        api_protocol=ModelApiProtocol.MESSAGES,
        model="postgres-contract-model",
        status=ModelCallStatus.SUCCEEDED,
        prompt_version="structured-review-v1",
        request_fingerprint="1" * 64,
        provider_response_id="msg_postgres_contract",
        provider_request_id="req_postgres_contract",
        response_status=200,
        duration_ms=100,
        usage=ModelTokenUsage(input_tokens=50, output_tokens=10),
        estimated_cost_microusd=25,
        output=model_output,
    )
    findings = materialize_findings(model_input, model_output)
    stored_model = queue.store_model_review(
        model_lease,
        model_input,
        model_result,
        findings,
    )

    assert stored_model.created is True
    assert stored_model.finding_count == 1
    assert stored_model.execution_status is ExecutionStatus.COMPLETED
    with postgres_database.sessions() as session:
        persisted = session.get(ReviewPlanRecord, stored.plan_id)
        task = session.get(ReviewTaskRecord, submission.review_task_id)
        assert persisted is not None
        assert task is not None
        assert persisted.plan_fingerprint == plan.plan_fingerprint
        assert persisted.model_review_completed_at is not None
        assert task.execution_status == ExecutionStatus.COMPLETED.value
        assert task.claimed_from_status is None
        assert session.scalar(select(ModelCallRecord.id)) == stored_model.model_call_id
        assert session.scalar(select(ReviewFindingRecord.verification_status)) == (
            "unverified"
        )
