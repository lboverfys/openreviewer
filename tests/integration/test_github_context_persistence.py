from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from apps.worker.main import WorkerRuntime, WorkerSettings
from domain.enums import (
    ChangedFileStatus,
    CiCheckKind,
    CiState,
    CoverageStatus,
    ExecutionStatus,
    PatchState,
    PullRequestState,
)
from domain.github import (
    CiCheckSnapshot,
    CiSnapshot,
    GitHubReviewContext,
    PullRequestFile,
    PullRequestSnapshot,
)
from domain.models import ReviewRequest
from domain.security import ErrorCode
from persistence.dashboard import SqlAlchemyDashboardRepository
from persistence.database import Database
from persistence.models import (
    Base,
    PullRequestCiCheckRecord,
    PullRequestFileRecord,
    PullRequestVersionRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.review_management import SqlAlchemyReviewManagementRepository
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from services.dashboard import DashboardService
from services.review_management import ReviewManagementService
from services.reviews import ReviewService
from services.task_queue import ReviewTarget, TaskLeaseLostError


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class StaticContextLoader:
    """记录 Worker 传入目标并返回固定上下文的测试替身。"""

    def __init__(self, context: GitHubReviewContext) -> None:
        self.context = context
        self.targets: list[ReviewTarget] = []

    def load(self, target: ReviewTarget) -> GitHubReviewContext:
        self.targets.append(target)
        return self.context


@pytest.fixture
def database(tmp_path: Path):
    path = (tmp_path / "github-context.sqlite3").as_posix()
    configured = Database.connect(f"sqlite:///{path}")
    Base.metadata.create_all(configured.engine)
    try:
        yield configured
    finally:
        configured.dispose()


def _submit(database: Database, key: str, head_sha: str) -> tuple[str, str]:
    result = ReviewService(SqlAlchemyReviewRepository(database.sessions)).submit(
        ReviewRequest(
            installation_id=10,
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=48,
            head_sha=head_sha,
        ),
        key,
    )
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, result.review_task_id)
        assert task is not None
        task.available_at = datetime(2000, 1, 1, tzinfo=UTC)
        session.commit()
    return result.review_task_id, result.review_run_id


def _context(
    head_sha: str,
    ci_state: CiState,
    checked_at: datetime,
    *,
    include_files: bool,
) -> GitHubReviewContext:
    files = (
        (
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
        )
        if include_files
        else None
    )
    check_status = (
        "completed"
        if ci_state in {CiState.NOT_CONFIGURED, CiState.SUCCESS, CiState.FAILURE}
        else "in_progress"
    )
    conclusion = (
        "success"
        if ci_state is CiState.SUCCESS
        else "failure" if ci_state is CiState.FAILURE else None
    )
    checks = (
        ()
        if ci_state is CiState.NOT_CONFIGURED
        else (
            CiCheckSnapshot(
                kind=CiCheckKind.CHECK_RUN,
                external_key="101",
                name="Backend tests",
                status=check_status,
                conclusion=conclusion,
                app_id=123,
            ),
        )
    )
    return GitHubReviewContext(
        pull_request=PullRequestSnapshot(
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=48,
            author_login="contributor",
            html_url="https://github.com/lboverfys/NiuMa/pull/48",
            head_repository="contributor/NiuMa",
            head_ref="feature/review-context",
            base_repository="lboverfys/NiuMa",
            base_ref="main",
            base_sha="b" * 40,
            head_sha=head_sha,
            state=PullRequestState.OPEN,
            draft=False,
            title="准备 GitHub 审查上下文",
            changed_files=1,
            updated_at=checked_at,
        ),
        files=files,
        files_complete=include_files,
        diff_complete=include_files,
        ci=CiSnapshot(
            head_sha=head_sha,
            state=ci_state,
            checks=checks,
            complete=True,
            checked_at=checked_at,
        ),
    )


def test_pending_ci_is_polled_without_consuming_failure_attempts(
    database: Database,
) -> None:
    """验证首轮保存文件、后续只刷新 CI，并在终态进入可审查边界。"""

    head_sha = "a" * 40
    task_id, run_id = _submit(database, "ci-poll", head_sha)
    clock = MutableClock(datetime(2026, 8, 24, 12, 0, tzinfo=UTC))
    queue = SqlAlchemyReviewTaskQueue(database.sessions, clock=clock)
    first = queue.claim_next("worker-1", timedelta(seconds=30))
    assert first is not None
    assert queue.load_target(first).context_fetched_at is None

    assert queue.store_github_context(
        first,
        _context(head_sha, CiState.PENDING, clock.value, include_files=True),
        ci_poll_interval=timedelta(seconds=30),
        ci_wait_timeout=timedelta(hours=1),
    ) is ExecutionStatus.WAITING_FOR_CI

    clock.value += timedelta(seconds=30)
    second = queue.claim_next("worker-1", timedelta(seconds=30))
    assert second is not None
    assert second.claimed_from_status is ExecutionStatus.WAITING_FOR_CI
    assert second.attempt_count == 1
    assert second.ci_poll_count == 1
    with pytest.raises(TaskLeaseLostError):
        queue.renew_lease(first, timedelta(seconds=30))
    assert queue.load_target(second).context_fetched_at is not None
    assert queue.store_github_context(
        second,
        _context(head_sha, CiState.SUCCESS, clock.value, include_files=False),
        ci_poll_interval=timedelta(seconds=30),
        ci_wait_timeout=timedelta(hours=1),
    ) is ExecutionStatus.READY_FOR_REVIEW

    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, run_id)
        version = session.scalar(
            select(PullRequestVersionRecord).where(
                PullRequestVersionRecord.review_version_key
                == run.review_version_key
            )
        )
        assert task.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        assert run.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        assert task.attempt_count == 1
        assert task.ci_poll_count == 1
        assert version.ci_state == CiState.SUCCESS.value
        assert version.author_login == "contributor"
        assert version.html_url == "https://github.com/lboverfys/NiuMa/pull/48"
        assert version.head_repository == "contributor/NiuMa"
        assert version.head_ref == "feature/review-context"
        assert version.base_repository == "lboverfys/NiuMa"
        assert version.base_ref == "main"
        assert version.files_complete is True
        assert version.diff_complete is True
        assert session.scalar(
            select(func.count()).select_from(PullRequestFileRecord)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(PullRequestCiCheckRecord)
        ) == 1

    dashboard_item = DashboardService(
        SqlAlchemyDashboardRepository(database.sessions),
        clock=clock,
    ).snapshot().recent_reviews[0]
    assert dashboard_item.pr_title == "准备 GitHub 审查上下文"
    assert dashboard_item.pr_author_login == "contributor"
    assert dashboard_item.pr_html_url == (
        "https://github.com/lboverfys/NiuMa/pull/48"
    )
    assert dashboard_item.head_repository == "contributor/NiuMa"
    assert dashboard_item.head_ref == "feature/review-context"
    assert dashboard_item.base_repository == "lboverfys/NiuMa"
    assert dashboard_item.base_ref == "main"

    stored_details = ReviewManagementService(
        SqlAlchemyReviewManagementRepository(database.sessions)
    ).details(run_id).stored
    assert stored_details.pr_author_login == "contributor"
    assert stored_details.pr_html_url == (
        "https://github.com/lboverfys/NiuMa/pull/48"
    )
    assert stored_details.head_repository == "contributor/NiuMa"
    assert stored_details.head_ref == "feature/review-context"
    assert stored_details.base_repository == "lboverfys/NiuMa"
    assert stored_details.base_ref == "main"


def test_unconfigured_ci_moves_directly_to_ready_for_review(
    database: Database,
) -> None:
    """完整但为空的 CI 快照不应无意义地等待到超时。"""

    head_sha = "a" * 40
    task_id, run_id = _submit(database, "ci-not-configured", head_sha)
    clock = MutableClock(datetime(2026, 8, 24, 12, 0, tzinfo=UTC))
    queue = SqlAlchemyReviewTaskQueue(database.sessions, clock=clock)
    lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert lease is not None

    assert queue.store_github_context(
        lease,
        _context(head_sha, CiState.NOT_CONFIGURED, clock.value, include_files=True),
        ci_poll_interval=timedelta(seconds=30),
        ci_wait_timeout=timedelta(hours=1),
    ) is ExecutionStatus.READY_FOR_REVIEW

    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, run_id)
        version = session.scalar(
            select(PullRequestVersionRecord).where(
                PullRequestVersionRecord.review_version_key
                == run.review_version_key
            )
        )
        assert task.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        assert run.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        assert task.ci_wait_started_at is None
        assert task.ci_deadline_at is None
        assert version.ci_state == CiState.NOT_CONFIGURED.value


def test_worker_runtime_wires_github_loader_to_ready_state(
    database: Database,
) -> None:
    """验证生产 Worker 路径确实调用上下文读取器，而非停留在旧占位边界。"""

    head_sha = "a" * 40
    task_id, run_id = _submit(database, "worker-github-context", head_sha)
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    loader = StaticContextLoader(
        _context(head_sha, CiState.SUCCESS, now, include_files=True)
    )
    runtime = WorkerRuntime(
        SqlAlchemyReviewTaskQueue(database.sessions, clock=MutableClock(now)),
        WorkerSettings(
            worker_id="worker-1",
            poll_interval=timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        ),
        context_loader=loader,
    )

    assert runtime.run_once() is True
    assert len(loader.targets) == 1
    assert loader.targets[0].head_sha == head_sha
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, run_id)
        assert task.execution_status == ExecutionStatus.READY_FOR_REVIEW.value
        assert run.execution_status == ExecutionStatus.READY_FOR_REVIEW.value


def test_new_head_supersedes_all_previous_active_runs_in_bulk(
    database: Database,
) -> None:
    """验证新提交用固定次数批量更新旧运行，旧 SHA 不再有活动任务。"""

    old_sha = "a" * 40
    new_sha = "c" * 40
    old_task_id, old_run_id = _submit(database, "old-head", old_sha)
    clock = MutableClock(datetime(2026, 8, 24, 12, 0, tzinfo=UTC))
    queue = SqlAlchemyReviewTaskQueue(database.sessions, clock=clock)
    old_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert old_lease is not None
    queue.store_github_context(
        old_lease,
        _context(old_sha, CiState.PENDING, clock.value, include_files=True),
        ci_poll_interval=timedelta(seconds=30),
        ci_wait_timeout=timedelta(hours=1),
    )

    new_task_id, new_run_id = _submit(database, "new-head", new_sha)
    new_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert new_lease is not None
    assert new_lease.task_id == new_task_id
    queue.store_github_context(
        new_lease,
        _context(new_sha, CiState.SUCCESS, clock.value, include_files=True),
        ci_poll_interval=timedelta(seconds=30),
        ci_wait_timeout=timedelta(hours=1),
    )

    with database.sessions() as session:
        old_task = session.get(ReviewTaskRecord, old_task_id)
        old_run = session.get(ReviewRunRecord, old_run_id)
        new_run = session.get(ReviewRunRecord, new_run_id)
        assert old_task is not None
        assert old_run is not None
        assert new_run is not None
        assert old_task.execution_status == ExecutionStatus.SUPERSEDED.value
        assert old_task.workflow_status == ExecutionStatus.SUPERSEDED.value
        assert old_run.execution_status == ExecutionStatus.SUPERSEDED.value
        assert old_run.workflow_status == ExecutionStatus.SUPERSEDED.value
        assert old_task.workflow_paused_from is None
        assert old_run.workflow_paused_from is None
        assert old_run.coverage_status == CoverageStatus.STALE.value
        assert new_run.execution_status == ExecutionStatus.READY_FOR_REVIEW.value


def test_ci_wait_timeout_is_not_reported_as_a_success(
    database: Database,
) -> None:
    """验证长期没有 CI 终态时进入明确超时，而不是无限等待或伪装通过。"""

    head_sha = "a" * 40
    task_id, run_id = _submit(database, "ci-timeout", head_sha)
    clock = MutableClock(datetime(2026, 8, 24, 12, 0, tzinfo=UTC))
    queue = SqlAlchemyReviewTaskQueue(database.sessions, clock=clock)
    first = queue.claim_next("worker-1", timedelta(seconds=30))
    assert first is not None
    queue.store_github_context(
        first,
        _context(head_sha, CiState.PENDING, clock.value, include_files=True),
        ci_poll_interval=timedelta(seconds=30),
        ci_wait_timeout=timedelta(seconds=60),
    )

    clock.value += timedelta(seconds=60)
    second = queue.claim_next("worker-1", timedelta(seconds=30))
    assert second is not None
    assert queue.store_github_context(
        second,
        _context(head_sha, CiState.PENDING, clock.value, include_files=False),
        ci_poll_interval=timedelta(seconds=30),
        ci_wait_timeout=timedelta(seconds=60),
    ) is ExecutionStatus.TIMED_OUT

    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, run_id)
        assert task.execution_status == ExecutionStatus.TIMED_OUT.value
        assert run.execution_status == ExecutionStatus.TIMED_OUT.value
        assert task.last_error_code == ErrorCode.CI_WAIT_TIMEOUT.value
        assert task.last_error_retryable is False


def test_current_github_head_mismatch_supersedes_before_context_is_saved(
    database: Database,
) -> None:
    """验证发布前版本保护的第一道门：旧任务只记录失效，不接受新 SHA 的数据。"""

    task_head_sha = "a" * 40
    current_head_sha = "c" * 40
    task_id, run_id = _submit(database, "stale-head", task_head_sha)
    clock = MutableClock(datetime(2026, 8, 24, 12, 0, tzinfo=UTC))
    queue = SqlAlchemyReviewTaskQueue(database.sessions, clock=clock)
    lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert lease is not None
    context = GitHubReviewContext(
        pull_request=PullRequestSnapshot(
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=48,
            author_login="contributor",
            html_url="https://github.com/lboverfys/NiuMa/pull/48",
            head_repository="contributor/NiuMa",
            head_ref="feature/review-context",
            base_repository="lboverfys/NiuMa",
            base_ref="main",
            base_sha="b" * 40,
            head_sha=current_head_sha,
            state=PullRequestState.OPEN,
            draft=False,
            title="PR 已经产生新提交",
            changed_files=1,
            updated_at=clock.value,
        ),
        files=None,
        files_complete=False,
        diff_complete=False,
        ci=None,
    )

    assert queue.store_github_context(
        lease,
        context,
        ci_poll_interval=timedelta(seconds=30),
        ci_wait_timeout=timedelta(hours=1),
    ) is ExecutionStatus.SUPERSEDED

    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, run_id)
        assert task is not None
        assert run is not None
        assert task.execution_status == ExecutionStatus.SUPERSEDED.value
        assert task.workflow_status == ExecutionStatus.SUPERSEDED.value
        assert run.execution_status == ExecutionStatus.SUPERSEDED.value
        assert run.workflow_status == ExecutionStatus.SUPERSEDED.value
        assert run.coverage_status == CoverageStatus.STALE.value
        assert session.scalar(
            select(func.count()).select_from(PullRequestVersionRecord)
        ) == 0
