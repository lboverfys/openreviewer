from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import event, func, select

from apps.worker.main import WorkerRuntime, WorkerSettings
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
    PullRequestState,
    ReviewFileDecision,
    Severity,
)
from domain.github import (
    CiSnapshot,
    GitHubReviewContext,
    PullRequestFile,
    PullRequestSnapshot,
)
from domain.model_budget import ModelBudgetPolicy
from domain.model_review import (
    ModelFindingCandidate,
    ModelFindingLocation,
    ModelReviewInput,
    ModelReviewOutput,
    ModelReviewResult,
    ModelTokenUsage,
    materialize_findings,
)
from domain.models import ReviewRequest
from domain.review_planning import RepositoryRule, RepositoryRulesSnapshot
from domain.security import ErrorCode, SafeApplicationError, SafeError
from persistence.database import Database
from persistence.models import (
    Base,
    FindingLifecycleRecord,
    ModelCallRecord,
    ModelHttpCallRecord,
    OutboxEventRecord,
    PullRequestVersionRecord,
    ReviewFilePlanRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewPlanRuleRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
    ReviewUnitRecord,
)
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.review_management import SqlAlchemyReviewManagementRepository
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from services.ai_settings import ActiveAiRuntime
from services.model_budget import ModelBudgetRequest
from services.model_review import ModelServiceSettings
from services.review_management import ReviewAction
from services.review_planning import DeterministicReviewPlanner, ReviewPlanningSettings
from services.reviews import ReviewService
from services.task_queue import (
    ModelBudgetExceededError,
    ReviewPlanConflictError,
    ReviewPlanInputError,
    ReviewTarget,
)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class StaticContextLoader:
    def __init__(self, context: GitHubReviewContext) -> None:
        self.context = context

    def load(self, _target: ReviewTarget) -> GitHubReviewContext:
        return self.context


class StaticRuleLoader:
    def __init__(self) -> None:
        self.calls: list[tuple[ReviewTarget, tuple[PullRequestFile, ...]]] = []

    def load(
        self,
        target: ReviewTarget,
        files: tuple[PullRequestFile, ...],
    ) -> RepositoryRulesSnapshot:
        self.calls.append((target, files))
        return _rules(target)


class StaticModelReviewer:
    def __init__(self) -> None:
        self.calls: list[ModelReviewInput] = []

    def review(self, review_input: ModelReviewInput) -> ModelReviewResult:
        self.calls.append(review_input)
        assert review_input.units
        unit = review_input.units[0]
        output = ModelReviewOutput(
            findings=(
                ModelFindingCandidate(
                    unit_key=unit.unit_key,
                    severity=Severity.HIGH,
                    category=FindingCategory.AUTHORIZATION,
                    location=ModelFindingLocation(
                        file=unit.file,
                        start_line=1,
                        end_line=1,
                        side=LocationSide.RIGHT,
                        symbol="handler",
                    ),
                    title="授权检查缺失",
                    evidence="新增处理路径没有调用授权检查。",
                    impact="普通用户可能执行受限操作。",
                    suggestion="在进入处理路径前调用统一授权检查。",
                    required_test="增加未授权请求被拒绝的测试。",
                    confidence=0.95,
                    rule_reference="AGENTS.md",
                ),
            )
        )
        return ModelReviewResult(
            provider=ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.RESPONSES,
            model="test-model",
            status=ModelCallStatus.SUCCEEDED,
            prompt_version="structured-review-v1",
            request_fingerprint="9" * 64,
            provider_response_id="resp_test_1",
            provider_request_id="req_test_1",
            response_status=200,
            duration_ms=250,
            usage=ModelTokenUsage(
                input_tokens=100,
                output_tokens=25,
                cache_read_input_tokens=10,
                reasoning_output_tokens=5,
            ),
            estimated_cost_microusd=375,
            output=output,
        )

    def close(self) -> None:
        return None


class FailingModelReviewer:
    def __init__(self) -> None:
        self.calls: list[ModelReviewInput] = []

    def review(self, review_input: ModelReviewInput) -> ModelReviewResult:
        self.calls.append(review_input)
        raise SafeApplicationError(
            SafeError(
                code=ErrorCode.MODEL_SERVER_ERROR,
                safe_message="模型中转站请求超时",
                retryable=True,
                details={
                    "status_code": 524,
                    "duration_ms": 100_250,
                    "provider_request_id": "relay-request-524",
                    "provider": "openai",
                    "api_protocol": "responses",
                    "model": "test-model",
                },
            )
        )

    def close(self) -> None:
        return None


class StaticAiRuntimeProvider:
    def __init__(self, runtime: ActiveAiRuntime | None) -> None:
        self.runtime = runtime
        self.calls = 0

    def current(self) -> ActiveAiRuntime | None:
        self.calls += 1
        return self.runtime

    def close(self) -> None:
        return None


@pytest.fixture
def database(tmp_path: Path):
    path = (tmp_path / "review-plan.sqlite3").as_posix()
    configured = Database.connect(f"sqlite:///{path}")
    Base.metadata.create_all(configured.engine)
    try:
        yield configured
    finally:
        configured.dispose()


def _files() -> tuple[PullRequestFile, ...]:
    return (
        PullRequestFile(
            path="src/app.py",
            status=ChangedFileStatus.MODIFIED,
            blob_sha="d" * 40,
            additions=1,
            deletions=1,
            changes=2,
            patch_state=PatchState.AVAILABLE,
            patch="@@ -1 +1 @@\n-old\n+new\n",
        ),
        PullRequestFile(
            path="assets/logo.png",
            status=ChangedFileStatus.MODIFIED,
            blob_sha="e" * 40,
            additions=0,
            deletions=0,
            changes=0,
            patch_state=PatchState.BINARY,
        ),
        PullRequestFile(
            path="src/large.ts",
            status=ChangedFileStatus.MODIFIED,
            blob_sha="1" * 40,
            additions=2000,
            deletions=1000,
            changes=3000,
            patch_state=PatchState.TOO_LARGE,
        ),
        PullRequestFile(
            path="src/missing.py",
            status=ChangedFileStatus.MODIFIED,
            blob_sha="2" * 40,
            additions=1,
            deletions=0,
            changes=1,
            patch_state=PatchState.MISSING,
        ),
    )


def _context(head_sha: str, now: datetime) -> GitHubReviewContext:
    files = _files()
    return GitHubReviewContext(
        pull_request=PullRequestSnapshot(
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=48,
            author_login="contributor",
            html_url="https://github.com/lboverfys/NiuMa/pull/48",
            head_repository="contributor/NiuMa",
            head_ref="feature/review-plan",
            base_repository="lboverfys/NiuMa",
            base_ref="main",
            base_sha="b" * 40,
            head_sha=head_sha,
            state=PullRequestState.OPEN,
            draft=False,
            title="Persist a deterministic review plan",
            changed_files=len(files),
            updated_at=now,
        ),
        files=files,
        files_complete=True,
        diff_complete=True,
        ci=CiSnapshot(
            head_sha=head_sha,
            state=CiState.SUCCESS,
            checks=(),
            complete=True,
            checked_at=now,
        ),
    )


def _complete_context(head_sha: str, now: datetime) -> GitHubReviewContext:
    """只包含一个可审查文本文件，用于验证完整覆盖下的结果消解。"""

    file = _files()[0]
    return GitHubReviewContext(
        pull_request=PullRequestSnapshot(
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=48,
            author_login="contributor",
            html_url="https://github.com/lboverfys/NiuMa/pull/48",
            head_repository="contributor/NiuMa",
            head_ref="feature/review-plan",
            base_repository="lboverfys/NiuMa",
            base_ref="main",
            base_sha="b" * 40,
            head_sha=head_sha,
            state=PullRequestState.OPEN,
            draft=False,
            title="Track findings across commits",
            changed_files=1,
            updated_at=now,
        ),
        files=(file,),
        files_complete=True,
        diff_complete=True,
        ci=CiSnapshot(
            head_sha=head_sha,
            state=CiState.SUCCESS,
            checks=(),
            complete=True,
            checked_at=now,
        ),
    )


def _rules(target: ReviewTarget) -> RepositoryRulesSnapshot:
    content = "# Review rules\nCheck authorization boundaries.\n"
    encoded = content.encode("utf-8")
    return RepositoryRulesSnapshot(
        repository_id=target.repository_id,
        repository=target.repository,
        head_sha=target.head_sha,
        rules=(
            RepositoryRule(
                path="AGENTS.md",
                scope=None,
                blob_sha="f" * 40,
                content=content,
                content_sha256=sha256(encoded).hexdigest(),
                byte_size=len(encoded),
            ),
        ),
        incomplete_files=(),
        issues=(),
        candidate_count=1,
        requested_candidate_count=1,
    )


def _submit(
    database: Database,
    clock: MutableClock,
    key: str,
    head_sha: str,
) -> tuple[str, str]:
    result = ReviewService(
        SqlAlchemyReviewRepository(database.sessions, clock=clock)
    ).submit(
        ReviewRequest(
            installation_id=10,
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=48,
            head_sha=head_sha,
        ),
        key,
    )
    return result.review_task_id, result.review_run_id


def _prepare_planning_lease(
    database: Database,
    clock: MutableClock,
    key: str = "review-plan",
    head_sha: str = "a" * 40,
    complete_context: bool = False,
):
    task_id, run_id = _submit(database, clock, key, head_sha)
    queue = SqlAlchemyReviewTaskQueue(database.sessions, clock=clock)
    context_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert context_lease is not None
    assert queue.store_github_context(
        context_lease,
        (
            _complete_context(head_sha, clock.value)
            if complete_context
            else _context(head_sha, clock.value)
        ),
        ci_poll_interval=timedelta(seconds=30),
        ci_wait_timeout=timedelta(hours=1),
    ) is ExecutionStatus.READY_FOR_REVIEW
    planning_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert planning_lease is not None
    assert planning_lease.claimed_from_status is ExecutionStatus.READY_FOR_REVIEW
    return queue, planning_lease, task_id, run_id


def _store_complete_review(
    database: Database,
    clock: MutableClock,
    *,
    key: str,
    head_sha: str,
    include_finding: bool,
) -> str:
    queue, plan_lease, _task_id, run_id = _prepare_planning_lease(
        database,
        clock,
        key=key,
        head_sha=head_sha,
        complete_context=True,
    )
    planning_input = queue.load_planning_input(plan_lease)
    rules = _rules(planning_input.target)
    plan = DeterministicReviewPlanner().plan(
        planning_input.target,
        planning_input.files,
        rules,
    )
    queue.store_review_plan(plan_lease, rules, plan)
    model_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert model_lease is not None
    model_input = queue.load_model_review_input(model_lease)
    result = StaticModelReviewer().review(model_input)
    if not include_finding:
        result = result.model_copy(
            update={"output": ModelReviewOutput(findings=())}
        )
    queue.store_model_review(
        model_lease,
        model_input,
        result,
        materialize_findings(model_input, result.output),
    )
    return run_id


def _prepare_budget_lease(
    database: Database,
    clock: MutableClock,
    *,
    key: str,
    policy: ModelBudgetPolicy,
):
    queue, planning_lease, task_id, run_id = _prepare_planning_lease(
        database,
        clock,
        key=key,
    )
    planning_input = queue.load_planning_input(planning_lease)
    rules = _rules(planning_input.target)
    plan = DeterministicReviewPlanner(
        ReviewPlanningSettings(model_budget=policy)
    ).plan(planning_input.target, planning_input.files, rules)
    stored = queue.store_review_plan(planning_lease, rules, plan)
    model_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert model_lease is not None
    assert model_lease.review_plan_id == stored.plan_id
    assert stored.plan_id is not None
    return queue, model_lease, task_id, run_id, stored.plan_id


def test_sqlite_plan_is_loaded_once_and_saved_atomically(
    database: Database,
) -> None:
    clock = MutableClock(datetime(2026, 8, 25, 10, 0, tzinfo=UTC))
    queue, lease, task_id, run_id = _prepare_planning_lease(database, clock)

    select_statements: list[str] = []

    def count_selects(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            select_statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", count_selects)
    try:
        planning_input = queue.load_planning_input(lease)
    finally:
        event.remove(database.engine, "before_cursor_execute", count_selects)
    assert len(select_statements) == 1
    assert [item.path for item in planning_input.files] == [
        "assets/logo.png",
        "src/app.py",
        "src/large.ts",
        "src/missing.py",
    ]

    rules = _rules(planning_input.target)
    plan = DeterministicReviewPlanner().plan(
        planning_input.target,
        planning_input.files,
        rules,
    )
    stored = queue.store_review_plan(lease, rules, plan)
    repeated = queue.store_review_plan(lease, rules, plan)

    assert stored.created is True
    assert repeated.created is False
    assert repeated.plan_id == stored.plan_id
    assert stored.execution_status is ExecutionStatus.READY_FOR_REVIEW
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, run_id)
        persisted_plan = session.get(ReviewPlanRecord, stored.plan_id)
        assert task is not None
        assert run is not None
        assert persisted_plan is not None
        assert task.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        assert run.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        assert task.claimed_from_status is None
        assert task.lease_owner is None
        assert task.attempt_count == 2
        assert persisted_plan.plan_fingerprint == plan.plan_fingerprint
        assert persisted_plan.rules_complete is True
        assert persisted_plan.rule_count == 1
        assert persisted_plan.unit_count == 1
        assert persisted_plan.file_count == 4
        assert session.scalar(
            select(func.count()).select_from(ReviewPlanRuleRecord)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(ReviewUnitRecord)
        ) == 1
        decisions = dict(
            session.execute(
                select(ReviewFilePlanRecord.file, ReviewFilePlanRecord.decision)
            ).all()
        )
        assert decisions == {
            "assets/logo.png": ReviewFileDecision.BINARY.value,
            "src/app.py": ReviewFileDecision.PLANNED.value,
            "src/large.ts": ReviewFileDecision.PATCH_TOO_LARGE.value,
            "src/missing.py": ReviewFileDecision.PATCH_MISSING.value,
        }
        assert session.scalar(
            select(func.count())
            .select_from(OutboxEventRecord)
            .where(OutboxEventRecord.event_type == "review.plan.prepared")
        ) == 1

    conflicting = plan.model_copy(update={"plan_fingerprint": "0" * 64})
    with pytest.raises(ReviewPlanConflictError):
        queue.store_review_plan(lease, rules, conflicting)


def test_model_review_is_loaded_and_saved_atomically(database: Database) -> None:
    clock = MutableClock(datetime(2026, 8, 25, 10, 30, tzinfo=UTC))
    queue, plan_lease, task_id, run_id = _prepare_planning_lease(
        database,
        clock,
        key="model-review-persistence",
    )
    planning_input = queue.load_planning_input(plan_lease)
    rules = _rules(planning_input.target)
    plan = DeterministicReviewPlanner().plan(
        planning_input.target,
        planning_input.files,
        rules,
    )
    stored_plan = queue.store_review_plan(plan_lease, rules, plan)

    model_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert model_lease is not None
    assert model_lease.review_plan_id == stored_plan.plan_id
    assert model_lease.model_attempt_count == 1
    assert model_lease.attempt_count == 2
    model_input = queue.load_model_review_input(model_lease)
    reviewer = StaticModelReviewer()
    result = reviewer.review(model_input)
    findings = materialize_findings(model_input, result.output)
    stored = queue.store_model_review(
        model_lease,
        model_input,
        result,
        findings,
    )
    repeated = queue.store_model_review(
        model_lease,
        model_input,
        result,
        findings,
    )

    assert stored.created is True
    assert repeated.created is False
    assert repeated.model_call_id == stored.model_call_id
    assert stored.finding_count == 1
    assert stored.execution_status is ExecutionStatus.COMPLETED
    assert queue.claim_next("worker-1", timedelta(seconds=30)) is None
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, run_id)
        persisted_plan = session.get(ReviewPlanRecord, stored_plan.plan_id)
        call = session.get(ModelCallRecord, stored.model_call_id)
        finding = session.scalar(select(ReviewFindingRecord))
        assert task is not None
        assert run is not None
        assert persisted_plan is not None
        assert call is not None
        assert finding is not None
        assert task.execution_status == ExecutionStatus.COMPLETED.value
        assert run.execution_status == ExecutionStatus.COMPLETED.value
        assert run.review_conclusion == "findings_present"
        assert run.coverage_status == "partial"
        assert task.model_attempt_count == 1
        assert task.lease_owner is None
        completed_at = persisted_plan.model_review_completed_at
        assert completed_at is not None
        assert completed_at.replace(tzinfo=UTC) == clock.value
        assert call.input_tokens == 100
        assert call.cache_read_input_tokens == 10
        assert call.estimated_cost_microusd == 375
        assert call.finding_count == 1
        assert finding.head_sha == "a" * 40
        assert finding.verification_status == "verified"
        assert finding.location_in_diff is True
        assert session.scalar(
            select(func.count())
            .select_from(OutboxEventRecord)
            .where(OutboxEventRecord.event_type == "review.model.completed")
        ) == 1


def test_complete_reviews_track_present_fixed_and_reintroduced_findings(
    database: Database,
) -> None:
    clock = MutableClock(datetime(2026, 8, 28, 9, 0, tzinfo=UTC))
    first_run_id = _store_complete_review(
        database,
        clock,
        key="lifecycle-first",
        head_sha="1" * 40,
        include_finding=True,
    )
    clock.value += timedelta(minutes=1)
    second_run_id = _store_complete_review(
        database,
        clock,
        key="lifecycle-second",
        head_sha="2" * 40,
        include_finding=True,
    )
    clock.value += timedelta(minutes=1)
    fixed_run_id = _store_complete_review(
        database,
        clock,
        key="lifecycle-fixed",
        head_sha="3" * 40,
        include_finding=False,
    )

    management = SqlAlchemyReviewManagementRepository(
        database.sessions,
        clock=clock,
    )
    fixed_details = management.get(fixed_run_id)
    assert fixed_details.fixed_finding_count == 1
    assert fixed_details.findings == ()

    clock.value += timedelta(minutes=1)
    reintroduced_run_id = _store_complete_review(
        database,
        clock,
        key="lifecycle-reintroduced",
        head_sha="4" * 40,
        include_finding=True,
    )

    with database.sessions() as session:
        findings = list(
            session.scalars(
                select(ReviewFindingRecord).order_by(
                    ReviewFindingRecord.created_at.asc()
                )
            )
        )
        assert [item.lifecycle_status for item in findings] == [
            "new",
            "still_present",
            "reintroduced",
        ]
        assert [item.occurrence_count for item in findings] == [1, 2, 3]
        assert findings[0].previous_review_run_id is None
        assert findings[1].previous_review_run_id == first_run_id
        assert findings[2].previous_review_run_id == second_run_id
        lifecycle = session.scalar(select(FindingLifecycleRecord))
        assert lifecycle is not None
        assert lifecycle.state == "present"
        assert lifecycle.last_seen_review_run_id == reintroduced_run_id
        assert lifecycle.previous_seen_review_run_id == second_run_id
        assert lifecycle.fixed_by_review_run_id is None
        assert lifecycle.occurrence_count == 3

    details = management.get(reintroduced_run_id)
    assert details.fixed_finding_count == 0
    assert details.findings[0].lifecycle_status == "reintroduced"
    assert details.findings[0].occurrence_count == 3


def test_partial_review_does_not_mark_absent_finding_as_fixed(
    database: Database,
) -> None:
    clock = MutableClock(datetime(2026, 8, 28, 10, 0, tzinfo=UTC))
    _store_complete_review(
        database,
        clock,
        key="lifecycle-complete-before-partial",
        head_sha="5" * 40,
        include_finding=True,
    )
    clock.value += timedelta(minutes=1)
    queue, plan_lease, _task_id, partial_run_id = _prepare_planning_lease(
        database,
        clock,
        key="lifecycle-partial",
        head_sha="6" * 40,
    )
    planning_input = queue.load_planning_input(plan_lease)
    rules = _rules(planning_input.target)
    plan = DeterministicReviewPlanner().plan(
        planning_input.target,
        planning_input.files,
        rules,
    )
    queue.store_review_plan(plan_lease, rules, plan)
    model_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert model_lease is not None
    model_input = queue.load_model_review_input(model_lease)
    result = StaticModelReviewer().review(model_input).model_copy(
        update={"output": ModelReviewOutput(findings=())}
    )
    queue.store_model_review(model_lease, model_input, result, ())

    with database.sessions() as session:
        lifecycle = session.scalar(select(FindingLifecycleRecord))
        assert lifecycle is not None
        assert lifecycle.state == "present"
        assert lifecycle.fixed_by_review_run_id is None
    assert (
        SqlAlchemyReviewManagementRepository(database.sessions)
        .get(partial_run_id)
        .fixed_finding_count
        == 0
    )


def test_model_aggregating_state_is_atomic_and_idempotent(database: Database) -> None:
    clock = MutableClock(datetime(2026, 8, 27, 10, 45, tzinfo=UTC))
    queue, plan_lease, task_id, run_id = _prepare_planning_lease(
        database,
        clock,
        key="model-aggregating-state",
    )
    planning_input = queue.load_planning_input(plan_lease)
    rules = _rules(planning_input.target)
    plan = DeterministicReviewPlanner().plan(
        planning_input.target,
        planning_input.files,
        rules,
    )
    queue.store_review_plan(plan_lease, rules, plan)
    model_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert model_lease is not None

    queue.mark_model_aggregating(model_lease)
    queue.mark_model_aggregating(model_lease)

    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, run_id)
        assert task is not None
        assert run is not None
        assert task.execution_status == ExecutionStatus.RUNNING.value
        assert run.execution_status == ExecutionStatus.RUNNING.value
        assert task.workflow_status == ExecutionStatus.AGGREGATING.value
        assert run.workflow_status == ExecutionStatus.AGGREGATING.value
        events = list(
            session.scalars(
                select(OutboxEventRecord).where(
                    OutboxEventRecord.event_type
                    == "review.model.aggregating_started"
                )
            )
        )
        assert len(events) == 1
        assert events[0].payload["review_plan_id"] == model_lease.review_plan_id
        assert events[0].payload["agent_count"] == 3


def test_new_sha_discards_model_result_before_call_and_finding_insert(
    database: Database,
) -> None:
    clock = MutableClock(datetime(2026, 8, 25, 11, 30, tzinfo=UTC))
    queue, plan_lease, old_task_id, old_run_id = _prepare_planning_lease(
        database,
        clock,
        key="old-model-head",
    )
    planning_input = queue.load_planning_input(plan_lease)
    rules = _rules(planning_input.target)
    plan = DeterministicReviewPlanner().plan(
        planning_input.target,
        planning_input.files,
        rules,
    )
    queue.store_review_plan(plan_lease, rules, plan)
    model_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert model_lease is not None
    model_input = queue.load_model_review_input(model_lease)
    result = StaticModelReviewer().review(model_input)
    findings = materialize_findings(model_input, result.output)

    clock.value += timedelta(seconds=1)
    _submit(database, clock, "new-model-head", "c" * 40)
    stored = queue.store_model_review(
        model_lease,
        model_input,
        result,
        findings,
    )

    assert stored.execution_status is ExecutionStatus.SUPERSEDED
    assert stored.model_call_id is None
    with database.sessions() as session:
        old_task = session.get(ReviewTaskRecord, old_task_id)
        old_run = session.get(ReviewRunRecord, old_run_id)
        assert old_task is not None
        assert old_run is not None
        assert old_task.execution_status == ExecutionStatus.SUPERSEDED.value
        assert old_run.execution_status == ExecutionStatus.SUPERSEDED.value
        assert session.scalar(select(func.count()).select_from(ModelCallRecord)) == 0
        assert session.scalar(
            select(func.count()).select_from(ReviewFindingRecord)
        ) == 0


def test_model_stage_uses_an_independent_retry_counter(database: Database) -> None:
    clock = MutableClock(datetime(2026, 8, 25, 12, 0, tzinfo=UTC))
    queue, plan_lease, task_id, _run_id = _prepare_planning_lease(
        database,
        clock,
        key="model-stage-retry-counter",
    )
    planning_input = queue.load_planning_input(plan_lease)
    rules = _rules(planning_input.target)
    plan = DeterministicReviewPlanner().plan(
        planning_input.target,
        planning_input.files,
        rules,
    )
    queue.store_review_plan(plan_lease, rules, plan)

    first_model_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert first_model_lease is not None
    queue.retry_or_fail(
        first_model_lease,
        SafeError(
            code=ErrorCode.MODEL_RATE_LIMITED,
            safe_message="模型暂时限流",
            retryable=True,
        ),
    )
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        assert task is not None
        assert task.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        assert task.attempt_count == 2
        assert task.model_attempt_count == 1

    clock.value += timedelta(seconds=5)
    second_model_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert second_model_lease is not None
    assert second_model_lease.attempt_count == 2
    assert second_model_lease.model_attempt_count == 2
    assert second_model_lease.review_plan_id is not None


def test_model_budget_settlement_releases_unused_reservation(
    database: Database,
) -> None:
    clock = MutableClock(datetime(2026, 8, 28, 8, 0, tzinfo=UTC))
    queue, lease, _task_id, _run_id, plan_id = _prepare_budget_lease(
        database,
        clock,
        key="budget-settlement",
        policy=ModelBudgetPolicy(
            max_http_calls=2,
            max_input_tokens=1_000,
            max_output_tokens=256,
            max_estimated_cost_microusd=1_000,
            max_duration_seconds=30,
        ),
    )
    reservation = queue.reserve_model_budget(
        lease,
        ModelBudgetRequest(
            provider="openai",
            api_protocol="responses",
            model="test-model",
            request_bytes=200,
            input_token_upper_bound=400,
            output_token_upper_bound=100,
            cost_upper_bound_microusd=500,
        ),
    )

    queue.settle_model_budget(
        reservation,
        input_tokens=150,
        output_tokens=30,
        estimated_cost_microusd=125,
        response_status=200,
        duration_ms=250,
    )

    with database.sessions() as session:
        plan = session.get(ReviewPlanRecord, plan_id)
        call = session.get(ModelHttpCallRecord, reservation.id)
        assert plan is not None
        assert call is not None
        assert plan.model_http_calls == 1
        assert plan.model_input_tokens == 150
        assert plan.model_output_tokens == 30
        assert plan.model_estimated_cost_microusd == 125
        assert call.status == "settled"
        assert call.actual_input_tokens == 150
        assert call.actual_output_tokens == 30
        assert call.actual_cost_microusd == 125


def test_model_budget_settlement_rejects_a_response_after_the_hard_deadline(
    database: Database,
) -> None:
    """最后一个慢响应越过计划截止线时，结果不能继续进入成功流程。"""

    clock = MutableClock(datetime(2026, 8, 28, 8, 5, tzinfo=UTC))
    queue, lease, _task_id, run_id, plan_id = _prepare_budget_lease(
        database,
        clock,
        key="budget-deadline",
        policy=ModelBudgetPolicy(
            max_http_calls=2,
            max_input_tokens=1_000,
            max_output_tokens=256,
            max_estimated_cost_microusd=1_000,
            max_duration_seconds=30,
        ),
    )
    reservation = queue.reserve_model_budget(
        lease,
        ModelBudgetRequest(
            provider="openai",
            api_protocol="responses",
            model="test-model",
            request_bytes=200,
            input_token_upper_bound=400,
            output_token_upper_bound=100,
            cost_upper_bound_microusd=500,
        ),
    )
    clock.value += timedelta(seconds=31)

    with pytest.raises(ModelBudgetExceededError) as captured:
        queue.settle_model_budget(
            reservation,
            input_tokens=150,
            output_tokens=30,
            estimated_cost_microusd=125,
            response_status=200,
            duration_ms=31_000,
        )

    assert captured.value.error.details["budget_reason"] == "duration"
    assert captured.value.error.details["settled_after_deadline"] is True
    with database.sessions() as session:
        plan = session.get(ReviewPlanRecord, plan_id)
        call = session.get(ModelHttpCallRecord, reservation.id)
        assert plan is not None
        assert call is not None
        assert plan.model_budget_exhausted_reason == "duration"
        assert call.status == "settled"
        assert session.scalar(
            select(func.count())
            .select_from(OutboxEventRecord)
            .where(
                OutboxEventRecord.aggregate_id == run_id,
                OutboxEventRecord.event_type == "review.model.budget_exhausted",
            )
        ) == 1


def test_uncertain_model_budget_settlement_is_conservative_and_idempotent(
    database: Database,
) -> None:
    clock = MutableClock(datetime(2026, 8, 28, 8, 10, tzinfo=UTC))
    queue, lease, _task_id, _run_id, plan_id = _prepare_budget_lease(
        database,
        clock,
        key="budget-uncertain",
        policy=ModelBudgetPolicy(
            max_http_calls=2,
            max_input_tokens=1_000,
            max_output_tokens=256,
            max_estimated_cost_microusd=1_000,
            max_duration_seconds=30,
        ),
    )
    reservation = queue.reserve_model_budget(
        lease,
        ModelBudgetRequest(
            provider="anthropic",
            api_protocol="messages",
            model="test-model",
            request_bytes=200,
            input_token_upper_bound=400,
            output_token_upper_bound=100,
            cost_upper_bound_microusd=500,
        ),
    )

    queue.settle_model_budget(
        reservation,
        input_tokens=None,
        output_tokens=None,
        estimated_cost_microusd=None,
        response_status=None,
        duration_ms=1_000,
        uncertain=True,
    )
    queue.settle_model_budget(
        reservation,
        input_tokens=1,
        output_tokens=1,
        estimated_cost_microusd=1,
        response_status=200,
        duration_ms=2_000,
    )

    with database.sessions() as session:
        plan = session.get(ReviewPlanRecord, plan_id)
        call = session.get(ModelHttpCallRecord, reservation.id)
        assert plan is not None
        assert call is not None
        assert plan.model_input_tokens == 400
        assert plan.model_output_tokens == 100
        assert plan.model_estimated_cost_microusd == 500
        assert call.status == "uncertain"
        assert call.actual_input_tokens is None
        assert call.actual_output_tokens is None
        assert call.actual_cost_microusd is None


def test_cost_cap_rejects_request_when_model_pricing_is_unknown(
    database: Database,
) -> None:
    clock = MutableClock(datetime(2026, 8, 28, 8, 20, tzinfo=UTC))
    queue, lease, _task_id, _run_id, plan_id = _prepare_budget_lease(
        database,
        clock,
        key="budget-pricing-unknown",
        policy=ModelBudgetPolicy(
            max_http_calls=2,
            max_input_tokens=1_000,
            max_output_tokens=256,
            max_estimated_cost_microusd=1_000,
            max_duration_seconds=30,
        ),
    )

    with pytest.raises(ModelBudgetExceededError) as captured:
        queue.reserve_model_budget(
            lease,
            ModelBudgetRequest(
                provider="openai",
                api_protocol="responses",
                model="unpriced-model",
                request_bytes=200,
                input_token_upper_bound=400,
                output_token_upper_bound=100,
                cost_upper_bound_microusd=None,
            ),
        )

    assert captured.value.error.details["budget_reason"] == "pricing_unknown"
    with database.sessions() as session:
        plan = session.get(ReviewPlanRecord, plan_id)
        assert plan is not None
        assert plan.model_budget_exhausted_reason == "pricing_unknown"
        assert plan.model_http_calls == 0
        assert session.scalar(
            select(func.count()).select_from(ModelHttpCallRecord)
        ) == 0


def test_budget_pause_resume_grants_an_audited_additional_window(
    database: Database,
) -> None:
    clock = MutableClock(datetime(2026, 8, 28, 8, 30, tzinfo=UTC))
    queue, first_lease, task_id, run_id, plan_id = _prepare_budget_lease(
        database,
        clock,
        key="budget-resume-grant",
        policy=ModelBudgetPolicy(
            max_http_calls=1,
            max_input_tokens=1_000,
            max_output_tokens=256,
            max_estimated_cost_microusd=1_000,
            max_duration_seconds=30,
        ),
    )
    request = ModelBudgetRequest(
        provider="openai",
        api_protocol="responses",
        model="test-model",
        request_bytes=200,
        input_token_upper_bound=400,
        output_token_upper_bound=100,
        cost_upper_bound_microusd=250,
    )
    first = queue.reserve_model_budget(first_lease, request)
    queue.settle_model_budget(
        first,
        input_tokens=200,
        output_tokens=50,
        estimated_cost_microusd=100,
        response_status=200,
        duration_ms=200,
    )
    with pytest.raises(ModelBudgetExceededError) as captured:
        queue.reserve_model_budget(first_lease, request)
    queue.pause_for_model_budget(first_lease, captured.value.error)

    repository = SqlAlchemyReviewManagementRepository(
        database.sessions,
        clock=clock,
    )
    resumed_run_id, resumed_task_id, status = repository.apply_action(
        run_id,
        ReviewAction.RESUME,
        actor="test-administrator",
        request_id="budget-resume-001",
    )
    assert resumed_run_id == run_id
    assert resumed_task_id == task_id
    assert status is ExecutionStatus.READY_FOR_REVIEW

    second_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert second_lease is not None
    second = queue.reserve_model_budget(second_lease, request)
    assert second.sequence == 2

    with database.sessions() as session:
        plan = session.get(ReviewPlanRecord, plan_id)
        task = session.get(ReviewTaskRecord, task_id)
        resume_event = session.scalar(
            select(OutboxEventRecord).where(
                OutboxEventRecord.event_type == "review.workflow.resume"
            )
        )
        assert plan is not None
        assert task is not None
        assert resume_event is not None
        assert plan.model_budget_resume_count == 1
        assert plan.model_budget_exhausted_reason is None
        assert task.last_error_code is None
        assert resume_event.payload["model_budget_granted"] is True
        assert resume_event.payload["previous_budget_reason"] == "http_calls"
        assert resume_event.payload["max_model_http_calls"] == 2


def test_incomplete_file_snapshot_cannot_be_planned(database: Database) -> None:
    clock = MutableClock(datetime(2026, 8, 25, 11, 0, tzinfo=UTC))
    queue, lease, _task_id, run_id = _prepare_planning_lease(
        database,
        clock,
        key="incomplete-plan-input",
    )
    with database.sessions() as session:
        run = session.get(ReviewRunRecord, run_id)
        assert run is not None
        version = session.scalar(
            select(PullRequestVersionRecord).where(
                PullRequestVersionRecord.review_version_key
                == run.review_version_key
            )
        )
        assert version is not None
        version.files_complete = False
        session.commit()

    with pytest.raises(ReviewPlanInputError):
        queue.load_planning_input(lease)


def test_new_sha_supersedes_old_lease_before_plan_insert(database: Database) -> None:
    clock = MutableClock(datetime(2026, 8, 25, 12, 0, tzinfo=UTC))
    queue, old_lease, old_task_id, old_run_id = _prepare_planning_lease(
        database,
        clock,
        key="old-plan-head",
    )
    planning_input = queue.load_planning_input(old_lease)
    rules = _rules(planning_input.target)
    plan = DeterministicReviewPlanner().plan(
        planning_input.target,
        planning_input.files,
        rules,
    )

    clock.value += timedelta(seconds=1)
    new_task_id, _new_run_id = _submit(
        database,
        clock,
        "new-plan-head",
        "c" * 40,
    )
    stored = queue.store_review_plan(old_lease, rules, plan)

    assert stored.plan_id is None
    assert stored.execution_status is ExecutionStatus.SUPERSEDED
    with database.sessions() as session:
        old_task = session.get(ReviewTaskRecord, old_task_id)
        old_run = session.get(ReviewRunRecord, old_run_id)
        new_task = session.get(ReviewTaskRecord, new_task_id)
        assert old_task is not None
        assert old_run is not None
        assert new_task is not None
        assert old_task.execution_status == ExecutionStatus.SUPERSEDED.value
        assert old_task.workflow_status == ExecutionStatus.SUPERSEDED.value
        assert old_run.execution_status == ExecutionStatus.SUPERSEDED.value
        assert old_run.workflow_status == ExecutionStatus.SUPERSEDED.value
        assert new_task.execution_status == ExecutionStatus.QUEUED.value
        assert session.scalar(
            select(func.count()).select_from(ReviewPlanRecord)
        ) == 0


def test_plan_retry_and_expired_lease_return_to_ready_stage(
    database: Database,
) -> None:
    clock = MutableClock(datetime(2026, 8, 25, 12, 30, tzinfo=UTC))
    queue, first_lease, task_id, run_id = _prepare_planning_lease(
        database,
        clock,
        key="plan-stage-recovery",
    )
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        assert task is not None
        task.max_attempts = 5
        session.commit()

    queue.retry_or_fail(
        first_lease,
        SafeError(
            code=ErrorCode.GITHUB_TIMEOUT,
            safe_message="规则读取暂时超时",
            retryable=True,
        ),
    )
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, run_id)
        assert task is not None
        assert run is not None
        assert task.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        assert run.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        assert task.claimed_from_status is None

    clock.value += timedelta(seconds=10)
    second_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert second_lease is not None
    assert second_lease.claimed_from_status is ExecutionStatus.READY_FOR_REVIEW
    clock.value += timedelta(seconds=31)
    assert queue.recover_expired_leases() == 1
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, run_id)
        assert task is not None
        assert run is not None
        assert task.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        assert run.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        assert task.claimed_from_status is None


def test_worker_prepares_plan_runs_model_batches_and_completes(database: Database) -> None:
    clock = MutableClock(datetime(2026, 8, 25, 13, 0, tzinfo=UTC))
    task_id, run_id = _submit(database, clock, "worker-plan", "a" * 40)
    rule_loader = StaticRuleLoader()
    model_reviewer = StaticModelReviewer()
    runtime_provider = StaticAiRuntimeProvider(
        ActiveAiRuntime(
            revision=17,
            reviewer=model_reviewer,
            planner=DeterministicReviewPlanner(),
            model_settings=ModelServiceSettings(
                provider=ModelProvider.OPENAI,
                model="test-model",
                api_key="test-key",
                api_protocol=ModelApiProtocol.RESPONSES,
                context_window_tokens=1_000_000,
            ),
        )
    )
    runtime = WorkerRuntime(
        SqlAlchemyReviewTaskQueue(database.sessions, clock=clock),
        WorkerSettings(
            worker_id="worker-1",
            poll_interval=timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        ),
        context_loader=StaticContextLoader(_context("a" * 40, clock.value)),
        rule_loader=rule_loader,
        ai_runtime_provider=runtime_provider,
    )

    assert runtime.run_once() is True
    assert runtime.run_once() is True
    assert runtime.run_once() is True
    assert runtime.run_once() is False
    assert len(rule_loader.calls) == 1
    assert len(model_reviewer.calls) == 1
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, run_id)
        assert task is not None
        assert run is not None
        assert task.execution_status == ExecutionStatus.COMPLETED.value
        assert run.execution_status == ExecutionStatus.COMPLETED.value
        assert session.scalar(
            select(func.count()).select_from(ReviewPlanRecord)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(ModelCallRecord)
        ) == 1
        call = session.scalar(select(ModelCallRecord))
        assert call is not None
        assert call.configuration_revision == 17
        assert call.api_protocol == ModelApiProtocol.RESPONSES.value
        assert session.scalar(
            select(func.count()).select_from(ReviewFindingRecord)
        ) == 1
        progress_types = set(
            session.scalars(
                select(OutboxEventRecord.event_type).where(
                    OutboxEventRecord.event_type.like("review.model.%")
                )
            )
        )
        assert {
            "review.model.batches_planned",
            "review.model.batch_started",
            "review.model.request_started",
            "review.model.request_completed",
            "review.model.batch_completed",
            "review.model.completed",
        } <= progress_types
        completed_event = session.scalar(
            select(OutboxEventRecord).where(
                OutboxEventRecord.event_type == "review.model.batch_completed"
            )
        )
        assert completed_event is not None
        assert completed_event.payload["reasoning_tokens"] == 5


def test_worker_records_failed_batch_http_details_and_retry_time(
    database: Database,
) -> None:
    clock = MutableClock(datetime(2026, 8, 25, 14, 30, tzinfo=UTC))
    task_id, run_id = _submit(database, clock, "worker-model-524", "a" * 40)
    model_reviewer = FailingModelReviewer()
    runtime = WorkerRuntime(
        SqlAlchemyReviewTaskQueue(database.sessions, clock=clock),
        WorkerSettings(
            worker_id="worker-1",
            poll_interval=timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        ),
        context_loader=StaticContextLoader(_context("a" * 40, clock.value)),
        rule_loader=StaticRuleLoader(),
        ai_runtime_provider=StaticAiRuntimeProvider(
            ActiveAiRuntime(
                revision=18,
                reviewer=model_reviewer,
                planner=DeterministicReviewPlanner(),
                model_settings=ModelServiceSettings(
                    provider=ModelProvider.OPENAI,
                    model="test-model",
                    api_key="test-key",
                    api_protocol=ModelApiProtocol.RESPONSES,
                ),
            )
        ),
    )

    assert runtime.run_once() is True
    assert runtime.run_once() is True
    assert runtime.run_once() is True
    assert len(model_reviewer.calls) == 1
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, run_id)
        assert task is not None
        assert run is not None
        assert task.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        assert run.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        failed_event = session.scalar(
            select(OutboxEventRecord).where(
                OutboxEventRecord.event_type == "review.model.batch_failed"
            )
        )
        retry_event = session.scalar(
            select(OutboxEventRecord).where(
                OutboxEventRecord.event_type == "review.task.retry_scheduled"
            )
        )
        assert failed_event is not None
        assert failed_event.payload["batch_number"] == 1
        assert failed_event.payload["status_code"] == 524
        assert failed_event.payload["duration_ms"] == 100_250
        assert failed_event.payload["provider_request_id"] == "relay-request-524"
        assert failed_event.payload["error_retryable"] is True
        assert retry_event is not None
        assert retry_event.payload["retry_delay_seconds"] == 5
        assert datetime.fromisoformat(retry_event.payload["retry_at"]) == (
            clock.value + timedelta(seconds=5)
        )


def test_worker_does_not_claim_ready_task_without_active_ai_configuration(
    database: Database,
) -> None:
    clock = MutableClock(datetime(2026, 8, 25, 14, 0, tzinfo=UTC))
    task_id, _run_id = _submit(database, clock, "worker-no-ai", "b" * 40)
    rule_loader = StaticRuleLoader()
    runtime = WorkerRuntime(
        SqlAlchemyReviewTaskQueue(database.sessions, clock=clock),
        WorkerSettings(
            worker_id="worker-1",
            poll_interval=timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        ),
        context_loader=StaticContextLoader(_context("b" * 40, clock.value)),
        rule_loader=rule_loader,
        ai_runtime_provider=StaticAiRuntimeProvider(None),
    )

    assert runtime.run_once() is True
    assert runtime.run_once() is False
    assert rule_loader.calls == []
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        assert task is not None
        assert task.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        assert task.attempt_count == 1
        assert session.scalar(select(func.count()).select_from(ReviewPlanRecord)) == 0
