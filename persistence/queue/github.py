"""github 阶段的有界事务与数据访问。"""

from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import delete, insert, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from domain.enums import CiState, CoverageStatus, ExecutionStatus, PullRequestState
from domain.github import GitHubReviewContext
from domain.repository_policy import RepositoryPolicySnapshot
from domain.security import ErrorCode, SafeError
from persistence.models import (
    GitHubInstallationRecord,
    PullRequestCiCheckRecord,
    PullRequestFileRecord,
    PullRequestVersionRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.queue.common import (
    _add_event,
    _as_utc,
    _locked_owned_task_with_run,
    _set_owned_status,
    _set_workflow_status,
)
from persistence.queue.context import QueueStorage
from services.task_queue import (
    ReviewTarget,
    ReviewTaskLease,
    TaskLeaseLostError,
    TaskQueueError,
)


def load_target(self: QueueStorage, lease: ReviewTaskLease) -> ReviewTarget:
    """用一次有索引 JOIN 读取任务目标，不在外部请求期间持有事务。"""

    now = self._clock()
    context_fetched_at = (
        select(PullRequestVersionRecord.context_fetched_at)
        .where(
            PullRequestVersionRecord.review_version_key
            == ReviewRunRecord.review_version_key
        )
        .correlate(ReviewRunRecord)
        .scalar_subquery()
        .label("context_fetched_at")
    )
    statement = (
        select(
            ReviewRunRecord.installation_id,
            ReviewRunRecord.repository_id,
            ReviewRunRecord.repository,
            ReviewRunRecord.pull_request_number,
            ReviewRunRecord.head_sha,
            ReviewRunRecord.review_version_key,
            context_fetched_at,
        )
        .join(
            ReviewTaskRecord,
            ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
        )
        .where(
            ReviewTaskRecord.id == lease.task_id,
            ReviewTaskRecord.review_run_id == lease.review_run_id,
            ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
            ReviewTaskRecord.lease_owner == lease.worker_id,
            ReviewTaskRecord.attempt_count == lease.attempt_count,
            ReviewTaskRecord.model_attempt_count == lease.model_attempt_count,
            ReviewTaskRecord.ci_poll_count == lease.ci_poll_count,
            ReviewTaskRecord.claimed_from_status == lease.claimed_from_status.value,
            or_(
                ReviewTaskRecord.workflow_status.is_(None),
                ReviewTaskRecord.workflow_status != ExecutionStatus.PAUSED.value,
            ),
            or_(
                ReviewRunRecord.workflow_status.is_(None),
                ReviewRunRecord.workflow_status != ExecutionStatus.PAUSED.value,
            ),
            ReviewRunRecord.execution_status == ExecutionStatus.RUNNING.value,
            ReviewTaskRecord.lease_expires_at.is_not(None),
            ReviewTaskRecord.lease_expires_at > now,
        )
    )
    with self._sessions() as session:
        try:
            row = session.execute(statement).one_or_none()
            if row is None:
                raise TaskLeaseLostError()
            return ReviewTarget(
                installation_id=row.installation_id,
                repository_id=row.repository_id,
                repository=row.repository,
                pull_request_number=row.pull_request_number,
                head_sha=row.head_sha,
                review_version_key=row.review_version_key,
                context_fetched_at=(
                    _as_utc(row.context_fetched_at)
                    if row.context_fetched_at is not None
                    else None
                ),
            )
        except TaskLeaseLostError:
            raise
        except SQLAlchemyError as exc:
            raise TaskQueueError("the review target could not be loaded") from exc


def store_github_context(
    self: QueueStorage,
    lease: ReviewTaskLease,
    context: GitHubReviewContext,
    *,
    ci_poll_interval: timedelta,
    ci_wait_timeout: timedelta,
) -> ExecutionStatus:
    """保存有界快照并按当前 PR/CI 状态原子推进任务。"""

    if lease.claimed_from_status not in {
        ExecutionStatus.QUEUED,
        ExecutionStatus.WAITING_FOR_CI,
    }:
        raise TaskLeaseLostError("当前租约不属于 GitHub 上下文阶段")
    if ci_poll_interval.total_seconds() <= 0:
        raise ValueError("CI poll interval must be positive")
    if ci_wait_timeout <= ci_poll_interval:
        raise ValueError("CI wait timeout must exceed the poll interval")
    now = self._clock()
    with self._sessions() as session:
        try:
            task, run = _locked_owned_task_with_run(
                session,
                lease,
                now,
                include_repository_policy=True,
            )
            pull_request = context.pull_request
            if (
                pull_request.repository_id != run.repository_id
                or pull_request.repository != run.repository
                or pull_request.pull_request_number != run.pull_request_number
            ):
                raise TaskQueueError(
                    "GitHub PR identity does not match the review task"
                )
            if pull_request.head_sha != run.head_sha:
                _set_owned_status(
                    task,
                    run,
                    ExecutionStatus.SUPERSEDED,
                    now,
                )
                _set_workflow_status(
                    task,
                    run,
                    ExecutionStatus.SUPERSEDED,
                    now,
                )
                run.publish_attempt_token = None
                run.coverage_status = CoverageStatus.STALE.value
                _add_event(
                    self,
                    session,
                    task,
                    "review.superseded",
                    f"head-mismatch:{task.attempt_count}:ci-poll-{task.ci_poll_count}",
                    now,
                )
                session.commit()
                return ExecutionStatus.SUPERSEDED

            version = _get_or_create_version(self, session, run, now)
            _update_pull_request_snapshot(version, context, now)
            policy = (
                RepositoryPolicySnapshot.model_validate(run.repository_policy)
                if run.repository_policy is not None
                else None
            )
            if policy is not None and not policy.allows_branch(pull_request.base_ref):
                _set_owned_status(task, run, ExecutionStatus.CANCELLED, now)
                _set_workflow_status(task, run, ExecutionStatus.CANCELLED, now)
                _add_event(
                    self,
                    session,
                    task,
                    "review.policy_skipped",
                    f"policy:{policy.revision}",
                    now,
                    extra_payload={
                        "reason": "目标分支不在仓库审查范围内",
                        "base_ref": pull_request.base_ref,
                        "repository_policy_revision": policy.revision,
                    },
                )
                session.commit()
                return ExecutionStatus.CANCELLED
            if pull_request.state is PullRequestState.CLOSED or pull_request.draft:
                _set_owned_status(
                    task,
                    run,
                    ExecutionStatus.CANCELLED,
                    now,
                )
                _set_workflow_status(
                    task,
                    run,
                    ExecutionStatus.CANCELLED,
                    now,
                )
                run.publish_attempt_token = None
                _add_event(
                    self,
                    session,
                    task,
                    "review.cancelled",
                    f"not-reviewable:{task.attempt_count}:ci-poll-{task.ci_poll_count}",
                    now,
                )
                session.commit()
                return ExecutionStatus.CANCELLED

            if context.files is not None:
                _replace_files(session, version.id, context, now)
                version.files_complete = context.files_complete
                version.diff_complete = context.diff_complete
                version.context_fetched_at = now
            if context.ci is None or context.ci.head_sha != run.head_sha:
                raise TaskQueueError("GitHub CI snapshot is missing or stale")
            _replace_ci_checks(session, version.id, context, now)
            version.ci_state = context.ci.state.value
            version.ci_checks_complete = context.ci.complete
            version.ci_checked_at = context.ci.checked_at

            superseded_count = _supersede_previous_versions(
                session,
                run,
                now,
            )
            if superseded_count:
                _add_event(
                    self,
                    session,
                    task,
                    "review.previous_versions_superseded",
                    f"{task.attempt_count}:ci-poll-{task.ci_poll_count}",
                    now,
                    extra_payload={"superseded_count": superseded_count},
                )

            if context.ci.state in {CiState.PENDING, CiState.UNKNOWN}:
                wait_started_at = task.ci_wait_started_at or now
                deadline = task.ci_deadline_at or (wait_started_at + ci_wait_timeout)
                task.ci_wait_started_at = wait_started_at
                task.ci_deadline_at = deadline
                if _as_utc(deadline) <= _as_utc(now):
                    timeout_error = SafeError(
                        code=ErrorCode.CI_WAIT_TIMEOUT,
                        safe_message="等待 GitHub CI 完成已超时",
                        retryable=False,
                        details={"ci_state": context.ci.state.value},
                    )
                    task.last_error = timeout_error.safe_message
                    task.last_error_code = timeout_error.code.value
                    task.last_error_retryable = timeout_error.retryable
                    task.last_error_details = dict(timeout_error.details)
                    _set_owned_status(
                        task,
                        run,
                        ExecutionStatus.TIMED_OUT,
                        now,
                    )
                    _set_workflow_status(
                        task,
                        run,
                        ExecutionStatus.FAILED,
                        now,
                    )
                    _add_event(
                        self,
                        session,
                        task,
                        "review.ci_timed_out",
                        f"{task.attempt_count}:ci-poll-{task.ci_poll_count}",
                        now,
                        error=timeout_error,
                    )
                    next_status = ExecutionStatus.TIMED_OUT
                else:
                    _set_owned_status(
                        task,
                        run,
                        ExecutionStatus.WAITING_FOR_CI,
                        now,
                    )
                    _set_workflow_status(
                        task,
                        run,
                        ExecutionStatus.CI,
                        now,
                    )
                    task.available_at = now + ci_poll_interval
                    _add_event(
                        self,
                        session,
                        task,
                        "review.waiting_for_ci",
                        f"{task.attempt_count}:ci-poll-{task.ci_poll_count}",
                        now,
                        extra_payload={"ci_state": context.ci.state.value},
                    )
                    next_status = ExecutionStatus.WAITING_FOR_CI
            else:
                # 终态（包括明确的“未配置 CI”）不应残留上一轮等待期限，
                # 否则后续重试可能被旧 deadline 误判为超时。
                task.ci_wait_started_at = None
                task.ci_deadline_at = None
                _set_owned_status(
                    task,
                    run,
                    ExecutionStatus.READY_FOR_REVIEW,
                    now,
                )
                _set_workflow_status(
                    task,
                    run,
                    ExecutionStatus.PLANNING,
                    now,
                )
                _add_event(
                    self,
                    session,
                    task,
                    "review.ready_for_review",
                    f"{task.attempt_count}:ci-poll-{task.ci_poll_count}",
                    now,
                    extra_payload={"ci_state": context.ci.state.value},
                )
                next_status = ExecutionStatus.READY_FOR_REVIEW
            session.commit()
            return next_status
        except (TaskLeaseLostError, TaskQueueError):
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError("GitHub review context could not be stored") from exc


def _get_or_create_version(
    self: QueueStorage,
    session: Session,
    run: ReviewRunRecord,
    now: datetime,
) -> PullRequestVersionRecord:
    """锁定版本行；兼容历史手工任务缺少版本记录的情况。"""

    version = session.scalar(
        select(PullRequestVersionRecord)
        .where(PullRequestVersionRecord.review_version_key == run.review_version_key)
        .with_for_update()
    )
    if version is None:
        installation = session.get(GitHubInstallationRecord, run.installation_id)
        if installation is None:
            installation = GitHubInstallationRecord(
                id=run.installation_id,
                created_at=now,
                last_seen_at=now,
            )
            session.add(installation)
            session.flush()
        else:
            installation.last_seen_at = now
        version = PullRequestVersionRecord(
            id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"openreviewer:{run.review_version_key}",
                )
            ),
            review_version_key=run.review_version_key,
            installation_id=run.installation_id,
            repository_id=run.repository_id,
            repository=run.repository,
            pull_request_number=run.pull_request_number,
            head_sha=run.head_sha,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(version)
        session.flush()
    if (
        version.installation_id != run.installation_id
        or version.repository_id != run.repository_id
        or version.repository != run.repository
        or version.pull_request_number != run.pull_request_number
        or version.head_sha != run.head_sha
    ):
        raise TaskQueueError("stored PR version identity does not match the review run")
    return version


def _update_pull_request_snapshot(
    version: PullRequestVersionRecord,
    context: GitHubReviewContext,
    now: datetime,
) -> None:
    pull_request = context.pull_request
    version.base_sha = pull_request.base_sha
    version.author_login = pull_request.author_login
    version.html_url = pull_request.html_url
    version.head_repository = pull_request.head_repository
    version.head_ref = pull_request.head_ref
    version.base_repository = pull_request.base_repository
    version.base_ref = pull_request.base_ref
    version.identity_fetched_at = now
    version.pr_state = pull_request.state.value
    version.is_draft = pull_request.draft
    version.title = pull_request.title
    version.changed_files_count = pull_request.changed_files
    version.pr_updated_at = pull_request.updated_at
    version.last_seen_at = now


def _replace_files(
    session: Session,
    version_id: str,
    context: GitHubReviewContext,
    now: datetime,
) -> None:
    files = context.files
    if files is None:
        return
    session.execute(
        delete(PullRequestFileRecord).where(
            PullRequestFileRecord.pull_request_version_id == version_id
        )
    )
    if not files:
        return
    rows = [
        {
            "id": str(
                uuid5(
                    NAMESPACE_URL,
                    f"openreviewer:file:{version_id}:{item.path}",
                )
            ),
            "pull_request_version_id": version_id,
            "path": item.path,
            "previous_path": item.previous_path,
            "status": item.status.value,
            "blob_sha": item.blob_sha,
            "additions": item.additions,
            "deletions": item.deletions,
            "changes": item.changes,
            "patch_state": item.patch_state.value,
            "patch": item.patch,
            "observed_at": now,
        }
        for item in files
    ]
    session.execute(insert(PullRequestFileRecord), rows)


def _replace_ci_checks(
    session: Session,
    version_id: str,
    context: GitHubReviewContext,
    now: datetime,
) -> None:
    ci = context.ci
    if ci is None:
        return
    session.execute(
        delete(PullRequestCiCheckRecord).where(
            PullRequestCiCheckRecord.pull_request_version_id == version_id
        )
    )
    if not ci.checks:
        return
    rows = [
        {
            "id": str(
                uuid5(
                    NAMESPACE_URL,
                    "openreviewer:ci:"
                    f"{version_id}:{check.kind.value}:{check.external_key}",
                )
            ),
            "pull_request_version_id": version_id,
            "kind": check.kind.value,
            "external_key": check.external_key,
            "name": check.name,
            "status": check.status,
            "conclusion": check.conclusion,
            "app_id": check.app_id,
            "observed_at": now,
        }
        for check in ci.checks
    ]
    session.execute(insert(PullRequestCiCheckRecord), rows)


def _supersede_previous_versions(
    session: Session,
    current_run: ReviewRunRecord,
    now: datetime,
) -> int:
    """用两条批量 UPDATE 淘汰同一 PR 的其他 head SHA，查询次数为常数。"""

    replaceable_statuses = (
        ExecutionStatus.QUEUED.value,
        ExecutionStatus.WAITING_FOR_CI.value,
        ExecutionStatus.RUNNING.value,
        ExecutionStatus.READY_FOR_REVIEW.value,
        ExecutionStatus.COMPLETED.value,
    )
    previous_run_ids = select(ReviewRunRecord.id).where(
        ReviewRunRecord.repository_id == current_run.repository_id,
        ReviewRunRecord.pull_request_number == current_run.pull_request_number,
        ReviewRunRecord.id != current_run.id,
        ReviewRunRecord.head_sha != current_run.head_sha,
        ReviewRunRecord.execution_status.in_(replaceable_statuses),
    )
    session.execute(
        update(ReviewTaskRecord)
        .where(
            ReviewTaskRecord.review_run_id.in_(previous_run_ids),
            ReviewTaskRecord.execution_status.in_(replaceable_statuses),
        )
        .values(
            execution_status=ExecutionStatus.SUPERSEDED.value,
            workflow_status=ExecutionStatus.SUPERSEDED.value,
            workflow_paused_from=None,
            lease_owner=None,
            lease_expires_at=None,
            claimed_from_status=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    result = session.execute(
        update(ReviewRunRecord)
        .where(
            ReviewRunRecord.id.in_(previous_run_ids),
            ReviewRunRecord.execution_status.in_(replaceable_statuses),
        )
        .values(
            execution_status=ExecutionStatus.SUPERSEDED.value,
            workflow_status=ExecutionStatus.SUPERSEDED.value,
            workflow_paused_from=None,
            publish_attempt_token=None,
            coverage_status=CoverageStatus.STALE.value,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if not isinstance(result, CursorResult):
        raise TaskQueueError("superseded review update returned no row count")
    return max(0, int(result.rowcount or 0))
