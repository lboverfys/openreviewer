"""审查任务详情、阶段投影与人工控制用例。"""

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from domain.enums import ExecutionStatus, VerificationStatus
from domain.github import PullRequestSnapshot
from domain.pagination import CursorPage
from domain.repository_policy import RepositoryPolicySnapshot
from domain.review_coverage import ReviewCoverage
from domain.review_planning import ReviewFilePlan
from domain.review_progress import BatchProgress, BatchSnapshot
from services.rbac import ResourceScope
from services.task_queue import ReviewTarget


class ReviewManagementPersistenceError(RuntimeError):
    """任务详情或人工操作无法可靠地从数据库完成。"""


def _effective_scope(scope: ResourceScope | None) -> ResourceScope | None:
    """管理员全量范围不向旧仓储实现传递额外关键字参数。"""

    return None if scope is None or scope.unrestricted else scope


class ReviewNotFoundError(LookupError):
    """指定审查运行不存在。"""


class ReviewActionConflictError(ValueError):
    """当前任务状态不允许请求的人工操作。"""


class FindingNotFoundError(LookupError):
    """指定候选问题不属于当前审查运行。"""


class ReviewPublishUnavailableError(RuntimeError):
    """GitHub 人工发布器未配置或发布失败。"""


class ReviewIdentitySyncUnavailableError(RuntimeError):
    """GitHub PR 身份同步器未配置。"""


class ReviewIdentitySyncConflictError(ValueError):
    """GitHub 返回的 PR 身份与任务目标不一致。"""


class ReviewAction(StrEnum):
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    RETRY_STAGE = "retry_stage"
    APPROVE = "approve"
    REJECT = "reject"
    PUBLISH = "publish"
    EXPEDITE = "expedite"
    RETRY = "retry"
    CANCEL = "cancel"
    REVIEW_SNAPSHOT = "review_snapshot"
    RERUN = "rerun"
    # 新版交互动作；保留 RETRY/RERUN 供旧客户端继续工作。
    RETRY_FAILED_NODE = "retry_failed_node"
    NEW_REVIEW = "new_review"


class FindingDecision(StrEnum):
    VALID = "valid"
    FALSE_POSITIVE = "false_positive"
    DUPLICATE = "duplicate"
    OUT_OF_SCOPE = "out_of_scope"
    KNOWN_ISSUE = "known_issue"


@dataclass(frozen=True, slots=True)
class FindingCursor:
    created_at: datetime
    finding_id: str


def encode_finding_cursor(created_at: datetime, finding_id: str) -> str:
    """把 Finding 的稳定排序键编码为不透明 Base64URL 游标。"""

    normalized = (
        created_at.replace(tzinfo=UTC)
        if created_at.tzinfo is None
        else created_at.astimezone(UTC)
    )
    payload = json.dumps(
        {
            "v": 1,
            "created_at": normalized.isoformat(timespec="microseconds"),
            "finding_id": finding_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_finding_cursor(value: str) -> FindingCursor:
    """严格解析 Finding 游标，拒绝未知字段、版本、时区和非 URL 安全数据。"""

    if not value or len(value) > 512:
        raise ValueError("finding cursor is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        decoded = json.loads(payload.decode("utf-8"))
        if not isinstance(decoded, dict) or set(decoded) != {
            "v",
            "created_at",
            "finding_id",
        }:
            raise ValueError
        finding_id = decoded["finding_id"]
        if decoded["v"] != 1 or not isinstance(finding_id, str):
            raise ValueError
        if not 1 <= len(finding_id) <= 36:
            raise ValueError
        created_at = datetime.fromisoformat(decoded["created_at"])
        if created_at.tzinfo is None:
            raise ValueError
    except (
        UnicodeError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise ValueError("finding cursor is invalid") from exc
    return FindingCursor(
        created_at=created_at.astimezone(UTC),
        finding_id=finding_id,
    )


@dataclass(frozen=True, slots=True)
class StoredReviewEvent:
    id: str
    event_type: str
    payload: Mapping[str, object]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class StoredCiCheck:
    name: str
    kind: str
    status: str
    conclusion: str | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class StoredFinding:
    id: str
    fingerprint: str
    head_sha: str
    severity: str
    category: str
    title: str
    evidence: str
    impact: str
    suggestion: str
    required_test: str | None
    confidence: float
    verification_status: str
    evidence_verification_status: str
    evidence_verification_reason: str
    evidence_verified_at: datetime | None
    adjudication_status: str
    lifecycle_status: str
    occurrence_count: int
    previous_review_run_id: str | None
    location_file: str | None
    location_start_line: int | None
    location_end_line: int | None
    location_side: str | None
    location_in_diff: bool
    location_symbol: str | None
    rule_reference: str | None
    reviewed_at: datetime | None
    reviewed_by: str | None
    created_at: datetime
    context_references: tuple[str, ...] = ()

    @property
    def location_verification_status(self) -> VerificationStatus:
        """兼容旧存储名，明确表示这里只校验 Diff 定位。"""

        return VerificationStatus(self.verification_status)

@dataclass(frozen=True, slots=True)
class StoredEvaluationGate:
    category: str
    sample_count: int
    valid_count: int
    false_positive_count: int
    duplicate_count: int
    out_of_scope_count: int
    known_issue_count: int
    rejected_count: int
    high_severity_sample_count: int
    high_severity_false_positive_count: int
    high_severity_rejected_count: int
    precision: float
    high_severity_false_positive_rate: float
    admitted: bool
    reason: str


@dataclass(frozen=True, slots=True)
class StoredFindingCounts:
    total: int
    location_verified: int
    location_rejected: int
    location_unverified: int
    valid: int
    false_positive: int
    duplicate: int
    out_of_scope: int
    known_issue: int
    unreviewed: int
    new: int
    still_present: int
    reintroduced: int


@dataclass(frozen=True, slots=True)
class StoredReviewDetails:
    review_run_id: str
    review_task_id: str
    change_token: str
    review_version_key: str
    installation_id: int
    repository_id: int
    repository: str
    pull_request_number: int
    head_sha: str
    execution_status: ExecutionStatus
    workflow_status: ExecutionStatus
    review_conclusion: str | None
    coverage_status: str
    priority: int
    attempt_count: int
    model_attempt_count: int
    max_attempts: int
    ci_poll_count: int
    available_at: datetime
    claimed_from_status: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error: str | None
    last_error_code: str | None
    last_error_retryable: bool | None
    last_error_details: Mapping[str, object] | None
    created_at: datetime
    updated_at: datetime
    pr_title: str | None
    pr_state: str | None
    pr_is_draft: bool | None
    changed_files_count: int | None
    files_complete: bool | None
    diff_complete: bool | None
    context_fetched_at: datetime | None
    ci_state: str | None
    ci_checks_complete: bool | None
    ci_checked_at: datetime | None
    review_plan_id: str | None
    plan_created_at: datetime | None
    plan_file_count: int | None
    plan_unit_count: int | None
    plan_rule_count: int | None
    plan_input_bytes: int | None
    plan_rules_complete: bool | None
    plan_file_decisions: Mapping[str, int]
    model_review_completed_at: datetime | None
    model_call_id: str | None
    model_provider: str | None
    model_protocol: str | None
    model_name: str | None
    model_status: str | None
    model_response_status: int | None
    model_duration_ms: int | None
    model_input_tokens: int | None
    model_output_tokens: int | None
    model_cache_read_tokens: int | None
    model_cache_write_tokens: int | None
    model_reasoning_tokens: int | None
    model_cost_microusd: int | None
    model_finding_count: int | None
    model_created_at: datetime | None
    fixed_finding_count: int
    finding_counts: StoredFindingCounts
    finding_has_more: bool
    evaluation_gates: tuple[StoredEvaluationGate, ...]
    findings: tuple[StoredFinding, ...]
    ci_checks: tuple[StoredCiCheck, ...]
    events: tuple[StoredReviewEvent, ...]
    pr_author_login: str | None = None
    pr_html_url: str | None = None
    head_repository: str | None = None
    head_ref: str | None = None
    base_repository: str | None = None
    base_ref: str | None = None
    identity_fetched_at: datetime | None = None
    agent_statuses: Mapping[str, str] = field(default_factory=dict)
    agent_summaries: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    aggregation_status: str = "not_started"
    summary_status: str = "not_executed"
    partial_result: bool = False
    failed_agents: tuple[str, ...] = ()
    failed_batches: tuple[Mapping[str, object], ...] = ()
    batch_progress: Mapping[str, BatchProgress] = field(default_factory=dict)
    repository_policy: RepositoryPolicySnapshot | None = None
    model_request_count: int | None = None
    snapshot_review: bool = False

    excluded_file_examples: tuple[ReviewFilePlan, ...] = ()
    coverage_exclusions_acknowledged: bool = False

    @property
    def coverage(self) -> ReviewCoverage:
        return ReviewCoverage(
            self.coverage_status,
            model_completed=self.model_review_completed_at is not None,
            rules_complete=self.plan_rules_complete,
            file_decisions=self.plan_file_decisions,
            exclusions_acknowledged=self.coverage_exclusions_acknowledged,
        )

    @property
    def coverage_requires_acknowledgement(self) -> bool:
        return self.coverage.can_acknowledge and not self.coverage_exclusions_acknowledged

    @property
    def coverage_block_reason(self) -> str | None:
        return self.coverage.block_reason


@dataclass(frozen=True, slots=True)
class ReviewStage:
    key: str
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    detail_code: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewDetails:
    stored: StoredReviewDetails
    current_stage: str
    phase: str
    stages: tuple[ReviewStage, ...]
    available_actions: tuple[ReviewAction, ...]
    finding_total_count: int
    finding_next_cursor: str | None
    location_verified_finding_count: int
    location_rejected_finding_count: int
    location_unverified_finding_count: int
    valid_finding_count: int
    false_positive_finding_count: int
    duplicate_finding_count: int
    out_of_scope_finding_count: int
    known_issue_finding_count: int
    unreviewed_finding_count: int
    new_finding_count: int
    still_present_finding_count: int
    reintroduced_finding_count: int
    fixed_finding_count: int


@dataclass(frozen=True, slots=True)
class ReviewIdentityTarget:
    target: ReviewTarget
    fetched_at: datetime | None


class PullRequestIdentityLoader(Protocol):
    def load_pull_request(self, target: ReviewTarget) -> PullRequestSnapshot: ...


class ReviewManagementRepository(Protocol):
    def excluded_file_page(self, review_run_id: str, *, plan_id: str, limit: int = 10,
                           cursor: str | None = None, scope: ResourceScope | None = None) -> CursorPage[ReviewFilePlan]:
        ...

    def batch_page(self, review_run_id: str, agent: str, *, after: int = 0,
                   limit: int = 10, scope: ResourceScope | None = None) -> CursorPage[BatchSnapshot]:
        ...

    def finding_page(self, review_run_id: str, *, limit: int = 10, cursor: str | None = None,
                     severity: str | None = None, adjudication_status: str | None = None,
                     query: str = "", scope: ResourceScope | None = None) -> CursorPage[StoredFinding]:
        ...

    def event_page(self, review_run_id: str, *, limit: int = 10, cursor: str | None = None,
                   event_filter: str = "all", scope: ResourceScope | None = None) -> CursorPage[StoredReviewEvent]:
        ...

    def get(
        self,
        review_run_id: str,
        *,
        finding_limit: int = 50,
        finding_cursor: FindingCursor | None = None,
        finding_adjudication_status: str | None = None,
        scope: ResourceScope | None = None,
        view: str = "full",
    ) -> StoredReviewDetails:
        """返回单条任务及其有界事件、CI 和 Finding 快照。"""

    def change_token(
        self,
        review_run_id: str,
        *,
        scope: ResourceScope | None = None,
    ) -> str:
        """用一次有界查询返回任务详情的轻量变化令牌。"""

    def get_identity_target(
        self,
        review_run_id: str,
        *,
        scope: ResourceScope | None = None,
    ) -> ReviewIdentityTarget:
        """读取一次历史任务的 GitHub PR 身份，供事务外回查。"""

    def save_pull_request_identity(
        self,
        review_run_id: str,
        snapshot: PullRequestSnapshot,
        *,
        actor: str,
        request_id: str,
        scope: ResourceScope | None = None,
    ) -> None:
        """在短事务内保存已校验的 GitHub PR 身份。"""

    def apply_action(
        self,
        review_run_id: str,
        action: ReviewAction,
        *,
        actor: str,
        request_id: str,
        target_stage: str | None = None,
        retry_scope: str | None = None,
        agent: str | None = None,
        batch_number: int | None = None,
        state_version: str | None = None,
        head_sha: str | None = None,
        capture_model_outputs: bool = False,
        acknowledge_exclusions: bool = False,
        review_profile_id: str | None = None,
        scope: ResourceScope | None = None,
    ) -> tuple[str, str, ExecutionStatus]:
        """幂等执行任务控制动作并返回运行、任务和新状态。"""

    def review_finding(
        self,
        review_run_id: str,
        finding_id: str,
        decision: FindingDecision,
        *,
        actor: str,
        request_id: str,
        scope: ResourceScope | None = None,
    ) -> None:
        """幂等保存人工 Finding 裁决和对应审计事件。"""

    def publish(
        self,
        review_run_id: str,
        *,
        actor: str,
        request_id: str,
        state_version: str | None = None,
        head_sha: str | None = None,
        scope: ResourceScope | None = None,
    ) -> tuple[str, str, ExecutionStatus]:
        """在批准门之后执行一次人工 GitHub 发布。"""


class ReviewManagementService:
    def __init__(
        self,
        repository: ReviewManagementRepository,
        *,
        identity_loader: PullRequestIdentityLoader | None = None,
    ) -> None:
        self._repository = repository
        self._identity_loader = identity_loader

    def excluded_file_page(self, review_run_id: str, *, plan_id: str, limit: int = 10,
                           cursor: str | None = None, scope: ResourceScope | None = None) -> CursorPage[ReviewFilePlan]:
        return self._repository.excluded_file_page(review_run_id, plan_id=plan_id,
            limit=limit, cursor=cursor, scope=_effective_scope(scope))

    def batch_page(self, review_run_id: str, agent: str, *, after: int = 0,
                   limit: int = 10, scope: ResourceScope | None = None) -> CursorPage[BatchSnapshot]:
        if agent not in {"security", "convention", "logic", "summary"} or not 0 <= after <= 3000 or not 1 <= limit <= 100:
            raise ValueError("批次分页参数无效")
        return self._repository.batch_page(review_run_id, agent, after=after,
            limit=limit, scope=_effective_scope(scope))

    def finding_page(self, review_run_id: str, *, limit: int = 10, cursor: str | None = None,
                     severity: str | None = None, adjudication_status: str | None = None,
                     query: str = "", scope: ResourceScope | None = None) -> CursorPage[StoredFinding]:
        return self._repository.finding_page(review_run_id, limit=limit, cursor=cursor,
            severity=severity, adjudication_status=adjudication_status, query=query, scope=_effective_scope(scope))

    def event_page(self, review_run_id: str, *, limit: int = 10, cursor: str | None = None,
                   event_filter: str = "all", scope: ResourceScope | None = None) -> CursorPage[StoredReviewEvent]:
        return self._repository.event_page(review_run_id, limit=limit, cursor=cursor,
            event_filter=event_filter, scope=_effective_scope(scope))

    def change_token(
        self,
        review_run_id: str,
        *,
        scope: ResourceScope | None = None,
    ) -> str:
        """读取任务变化令牌，供详情页避免无变化时重复加载完整快照。"""

        effective_scope = _effective_scope(scope)
        if effective_scope is None:
            return self._repository.change_token(review_run_id)
        return self._repository.change_token(review_run_id, scope=effective_scope)

    def sync_identity(
        self,
        review_run_id: str,
        *,
        actor: str,
        request_id: str,
        loader: PullRequestIdentityLoader | None = None,
        scope: ResourceScope | None = None,
    ) -> ReviewDetails:
        """事务外回查 GitHub 并保存历史 PR 身份，再返回最新详情。"""

        effective_scope = _effective_scope(scope)
        identity = (
            self._repository.get_identity_target(review_run_id)
            if effective_scope is None
            else self._repository.get_identity_target(
                review_run_id,
                scope=effective_scope,
            )
        )
        if identity.fetched_at is not None:
            return self.details(review_run_id, scope=effective_scope)
        identity_loader = loader or self._identity_loader
        if identity_loader is None:
            raise ReviewIdentitySyncUnavailableError(
                "GitHub PR 身份同步器尚未配置"
            )
        snapshot = identity_loader.load_pull_request(identity.target)
        if effective_scope is None:
            self._repository.save_pull_request_identity(
                review_run_id,
                snapshot,
                actor=actor,
                request_id=request_id,
            )
        else:
            self._repository.save_pull_request_identity(
                review_run_id,
                snapshot,
                actor=actor,
                request_id=request_id,
                scope=effective_scope,
            )
        return self.details(review_run_id, scope=effective_scope)

    def details(
        self,
        review_run_id: str,
        *,
        finding_limit: int = 50,
        finding_cursor: str | None = None,
        scope: ResourceScope | None = None,
        view: str = "full",
    ) -> ReviewDetails:
        if not 1 <= finding_limit <= 100:
            raise ValueError("finding limit must be between 1 and 100")
        decoded_cursor = (
            decode_finding_cursor(finding_cursor)
            if finding_cursor is not None
            else None
        )
        effective_scope = _effective_scope(scope)
        if view != "full":
            stored = self._repository.get(
                review_run_id, finding_limit=finding_limit, finding_cursor=decoded_cursor,
                scope=effective_scope, view=view,
            )
        elif effective_scope is None:
            stored = self._repository.get(
                review_run_id,
                finding_limit=finding_limit,
                finding_cursor=decoded_cursor,
            )
        else:
            stored = self._repository.get(
                review_run_id,
                finding_limit=finding_limit,
                finding_cursor=decoded_cursor,
                scope=effective_scope,
            )
        counts = stored.finding_counts
        current_stage, phase = self._current_stage(stored, counts.unreviewed)
        return ReviewDetails(
            stored=stored,
            current_stage=current_stage,
            phase=phase,
            stages=self._stages(stored, current_stage, phase, counts.unreviewed),
            available_actions=self._available_actions(stored, counts.unreviewed),
            finding_total_count=counts.total,
            finding_next_cursor=(
                encode_finding_cursor(
                    stored.findings[-1].created_at,
                    stored.findings[-1].id,
                )
                if stored.finding_has_more and stored.findings
                else None
            ),
            location_verified_finding_count=counts.location_verified,
            location_rejected_finding_count=counts.location_rejected,
            location_unverified_finding_count=counts.location_unverified,
            valid_finding_count=counts.valid,
            false_positive_finding_count=counts.false_positive,
            duplicate_finding_count=counts.duplicate,
            out_of_scope_finding_count=counts.out_of_scope,
            known_issue_finding_count=counts.known_issue,
            unreviewed_finding_count=counts.unreviewed,
            new_finding_count=counts.new,
            still_present_finding_count=counts.still_present,
            reintroduced_finding_count=counts.reintroduced,
            fixed_finding_count=stored.fixed_finding_count,
        )

    def apply_action(
        self,
        review_run_id: str,
        action: ReviewAction,
        *,
        actor: str,
        request_id: str,
        target_stage: str | None = None,
        retry_scope: str | None = None,
        agent: str | None = None,
        batch_number: int | None = None,
        state_version: str | None = None,
        head_sha: str | None = None,
        capture_model_outputs: bool = False,
        acknowledge_exclusions: bool = False,
        review_profile_id: str | None = None,
        scope: ResourceScope | None = None,
    ) -> tuple[str, str, ExecutionStatus]:
        effective_scope = _effective_scope(scope)
        if acknowledge_exclusions and action is not ReviewAction.APPROVE:
            raise ReviewActionConflictError("未审查文件的人工确认只适用于批准操作")
        if capture_model_outputs and action is not ReviewAction.REVIEW_SNAPSHOT:
            raise ReviewActionConflictError("输出证据留存只适用于显式历史版本评测试跑")
        if review_profile_id is not None and action is not ReviewAction.REVIEW_SNAPSHOT:
            raise ReviewActionConflictError("候选方案只适用于历史版本评测试跑")
        if action in {ReviewAction.RERUN, ReviewAction.NEW_REVIEW}:
            if retry_scope not in {None, "new_review"}:
                raise ReviewActionConflictError("重试范围与当前操作不匹配")
            if self._identity_loader is None:
                raise ReviewActionConflictError("GitHub 连接未配置，无法确认最新提交；可复查已保存的版本")
            identity = self._repository.get_identity_target(review_run_id, scope=effective_scope)
            latest = self._identity_loader.load_pull_request(identity.target)
            if latest.repository_id != identity.target.repository_id or latest.pull_request_number != identity.target.pull_request_number:
                raise ReviewActionConflictError("GitHub 返回的仓库或 PR 与任务不一致")
            head_sha = latest.head_sha
            action = ReviewAction.NEW_REVIEW
            retry_scope = "new_review"
        if action is ReviewAction.PUBLISH:
            if effective_scope is None:
                return self._repository.publish(
                    review_run_id,
                    actor=actor,
                    request_id=request_id,
                    state_version=state_version,
                    head_sha=head_sha,
                )
            return self._repository.publish(
                review_run_id,
                actor=actor,
                request_id=request_id,
                state_version=state_version,
                head_sha=head_sha,
                scope=effective_scope,
            )
        if effective_scope is None:
            return self._repository.apply_action(
                review_run_id,
                action,
                actor=actor,
                request_id=request_id,
                target_stage=target_stage,
                retry_scope=retry_scope,
                agent=agent,
                batch_number=batch_number,
                state_version=state_version,
                head_sha=head_sha,
                capture_model_outputs=capture_model_outputs,
                acknowledge_exclusions=acknowledge_exclusions,
                review_profile_id=review_profile_id,
            )
        return self._repository.apply_action(
            review_run_id,
            action,
            actor=actor,
            request_id=request_id,
            target_stage=target_stage,
            retry_scope=retry_scope,
            agent=agent,
            batch_number=batch_number,
            state_version=state_version,
            head_sha=head_sha,
            capture_model_outputs=capture_model_outputs,
            acknowledge_exclusions=acknowledge_exclusions,
            review_profile_id=review_profile_id,
            scope=effective_scope,
        )

    def review_finding(
        self,
        review_run_id: str,
        finding_id: str,
        decision: FindingDecision,
        *,
        actor: str,
        request_id: str,
        scope: ResourceScope | None = None,
    ) -> ReviewDetails:
        effective_scope = _effective_scope(scope)
        if effective_scope is None:
            self._repository.review_finding(
                review_run_id,
                finding_id,
                decision,
                actor=actor,
                request_id=request_id,
            )
        else:
            self._repository.review_finding(
                review_run_id,
                finding_id,
                decision,
                actor=actor,
                request_id=request_id,
                scope=effective_scope,
            )
        return self.details(review_run_id, scope=effective_scope)

    @staticmethod
    def _current_stage(
        item: StoredReviewDetails,
        unreviewed_findings: int,
    ) -> tuple[str, str]:
        status = item.workflow_status
        if status is ExecutionStatus.SUPERSEDED:
            return "result", "superseded"
        if status is ExecutionStatus.CANCELLED:
            return "result", "cancelled"
        if status is ExecutionStatus.TIMED_OUT:
            return "ci", "ci_timed_out"
        if status is ExecutionStatus.PAUSED:
            paused_from: str | None = None
            for event in reversed(item.events):
                if event.event_type != "review.workflow.pause":
                    continue
                paused_from_value = event.payload.get("paused_from")
                if isinstance(paused_from_value, str):
                    paused_from = paused_from_value
                    break
            paused_stage = {
                ExecutionStatus.QUEUED.value: "context",
                ExecutionStatus.CI.value: "ci",
                ExecutionStatus.PLANNING.value: "planning",
                ExecutionStatus.AGENT_BATCHES.value: "agent_batches",
                ExecutionStatus.AGGREGATING.value: "aggregating",
                ExecutionStatus.AWAITING_APPROVAL.value: "approval",
                ExecutionStatus.AWAITING_PUBLISH.value: "publish",
            }.get(paused_from, "context") if paused_from is not None else "context"
            return paused_stage, "paused"
        if status is ExecutionStatus.COMPLETED:
            return "result", "completed"
        if status is ExecutionStatus.FAILED:
            if item.review_plan_id is not None:
                return "model", "model_failed"
            if item.context_fetched_at is not None:
                return "planning", "planning_failed"
            return "context", "context_failed"
        if status is ExecutionStatus.AWAITING_PUBLISH:
            if item.coverage_block_reason:
                return "publish", "coverage_incomplete"
            return "publish", "awaiting_publish"
        if status is ExecutionStatus.PUBLISHING:
            return "publish", "publishing"
        if status is ExecutionStatus.AWAITING_APPROVAL:
            if item.coverage_requires_acknowledgement and not unreviewed_findings:
                return "approval", "awaiting_coverage_confirmation"
            if item.coverage_block_reason and not item.coverage_requires_acknowledgement:
                return "approval", "coverage_incomplete"
            return (
                "approval",
                "awaiting_finding_adjudication"
                if unreviewed_findings
                else "awaiting_approval",
            )
        if status is ExecutionStatus.APPROVED:
            return "approval", "approved"
        if status is ExecutionStatus.REJECTED:
            return "approval", "rejected"
        if item.execution_status is ExecutionStatus.FAILED and status in {
            ExecutionStatus.CI, ExecutionStatus.PLANNING, ExecutionStatus.AGENT_BATCHES, ExecutionStatus.AGGREGATING,
        }:
            return ("ci", "ci_failed") if status is ExecutionStatus.CI else (
                "planning", "planning_failed"
            ) if status is ExecutionStatus.PLANNING else (
                "aggregating" if status is ExecutionStatus.AGGREGATING else "agent_batches", "model_failed"
            )
        direct_stage = {
            ExecutionStatus.CI: ("ci", "ci_running"),
            ExecutionStatus.PLANNING: ("planning", "planning_running"),
            ExecutionStatus.AGENT_BATCHES: ("agent_batches", "agent_batches_running"),
            ExecutionStatus.AGGREGATING: ("aggregating", "aggregating_running"),
        }.get(status)
        if direct_stage is not None:
            if item.execution_status is not ExecutionStatus.RUNNING:
                queued_phase = "waiting_ci" if status is ExecutionStatus.CI else (
                    "planning_queued" if status is ExecutionStatus.PLANNING else "model_queued"
                )
                return direct_stage[0], queued_phase
            if status is ExecutionStatus.AGENT_BATCHES:
                retrieval = next((event for event in reversed(item.events)
                                  if event.event_type in {"review.model.retrieval_started", "review.model.retrieval_completed"}), None)
                if retrieval is not None and retrieval.event_type == "review.model.retrieval_started":
                    return "agent_batches", "retrieval_started"
            return direct_stage
        if item.review_plan_id is not None:
            return (
                "model",
                "model_running"
                if status is ExecutionStatus.RUNNING
                else (
                    "model_retry_waiting"
                    if item.last_error is not None
                    else "model_queued"
                ),
            )
        if item.context_fetched_at is not None and item.ci_state in {
            "not_configured",
            "success",
            "failure",
        }:
            return (
                "planning",
                "planning_running"
                if status is ExecutionStatus.RUNNING
                else "planning_queued",
            )
        if item.context_fetched_at is not None:
            return (
                "ci",
                "ci_checking"
                if status is ExecutionStatus.RUNNING
                else "waiting_ci",
            )
        return (
            "context",
            "context_loading"
            if status is ExecutionStatus.RUNNING
            else "queued",
        )

    @staticmethod
    def _available_actions(
        item: StoredReviewDetails,
        unreviewed_findings: int,
    ) -> tuple[ReviewAction, ...]:
        status = item.workflow_status
        snapshot_actions = (ReviewAction.REVIEW_SNAPSHOT,) if (
            item.context_fetched_at is not None and item.files_complete is True and item.diff_complete is True
        ) else ()
        if status is ExecutionStatus.CANCELLED:
            return (*snapshot_actions, ReviewAction.NEW_REVIEW)
        if status is ExecutionStatus.SUPERSEDED:
            # 旧 SHA 的结果不可再重试；用户只能从当前详情入口创建一条
            # 新审查记录，避免把已被新提交替代的任务重新排回队列。
            return (*snapshot_actions, ReviewAction.NEW_REVIEW)
        summary_retry_available = item.summary_status == "failed"
        if item.coverage_status == "partial" and (
            item.failed_agents or item.failed_batches
        ):
            return (
                ReviewAction.RETRY_FAILED_NODE,
                ReviewAction.RETRY_STAGE,
                ReviewAction.NEW_REVIEW,
            )
        if status is ExecutionStatus.FAILED:
            return (ReviewAction.RETRY, ReviewAction.RETRY_STAGE, *snapshot_actions, ReviewAction.RERUN)
        if status is ExecutionStatus.TIMED_OUT:
            return (ReviewAction.RETRY, ReviewAction.RERUN)
        if status is ExecutionStatus.AWAITING_APPROVAL:
            if item.coverage_block_reason and not item.coverage_requires_acknowledgement:
                return (ReviewAction.NEW_REVIEW, ReviewAction.REJECT, ReviewAction.PAUSE)
            if unreviewed_findings:
                approval_actions: tuple[ReviewAction, ...] = (
                    ReviewAction.REJECT,
                    ReviewAction.PAUSE,
                )
            else:
                approval_actions = (
                    ReviewAction.APPROVE,
                    ReviewAction.REJECT,
                    ReviewAction.PAUSE,
                )
            if summary_retry_available:
                return (ReviewAction.RETRY_FAILED_NODE, *approval_actions)
            return approval_actions
        if status is ExecutionStatus.AWAITING_PUBLISH:
            if item.coverage_block_reason:
                return (ReviewAction.NEW_REVIEW, ReviewAction.REJECT)
            return (ReviewAction.PUBLISH, ReviewAction.REJECT)
        if status is ExecutionStatus.REJECTED:
            return (ReviewAction.RETRY_STAGE, *snapshot_actions, ReviewAction.RERUN)
        if status is ExecutionStatus.PAUSED:
            return (ReviewAction.RESUME, ReviewAction.CANCEL)
        if item.model_review_completed_at is not None and status is ExecutionStatus.COMPLETED:
            if summary_retry_available:
                return (ReviewAction.RETRY_FAILED_NODE, ReviewAction.RERUN)
            return (*snapshot_actions, ReviewAction.RERUN)
        if status is ExecutionStatus.QUEUED:
            return (
                ReviewAction.START,
                ReviewAction.EXPEDITE,
                ReviewAction.PAUSE,
                ReviewAction.CANCEL,
            )
        if status in {
            ExecutionStatus.WAITING_FOR_CI,
            ExecutionStatus.READY_FOR_REVIEW,
        }:
            return (ReviewAction.EXPEDITE, ReviewAction.CANCEL)
        if status in {
            ExecutionStatus.CI,
            ExecutionStatus.PLANNING,
            ExecutionStatus.AGENT_BATCHES,
            ExecutionStatus.AGGREGATING,
        }:
            return (ReviewAction.PAUSE, ReviewAction.CANCEL)
        if status is ExecutionStatus.RUNNING:
            return (ReviewAction.CANCEL,)
        return ()

    @staticmethod
    def _stages(
        item: StoredReviewDetails,
        current_stage: str,
        phase: str,
        unverified_findings: int,
    ) -> tuple[ReviewStage, ...]:
        order = (
            "intake",
            "context",
            "ci",
            "planning",
            "model",
            "agent_batches",
            "aggregating",
            "approval",
            "publish",
            "result",
        )
        current_index = order.index(current_stage)
        terminal = phase in {"cancelled", "superseded", "rejected"}
        failed_phase = phase.endswith("failed") or phase == "ci_timed_out"
        event_times = {event.event_type: event.occurred_at for event in item.events}
        completed_times = {
            "intake": event_times.get("review.requested", item.created_at),
            "context": item.context_fetched_at,
            "ci": (
                item.ci_checked_at
                if item.ci_state in {"not_configured", "success", "failure"}
                else None
            ),
            "planning": item.plan_created_at,
            "model": item.model_review_completed_at,
            "agent_batches": event_times.get(
                "review.model.batches_persisted",
                item.plan_created_at,
            ),
            "aggregating": event_times.get(
                "review.model.aggregating_started",
                item.model_review_completed_at,
            ),
            "approval": event_times.get(
                "review.workflow.approve",
                item.model_review_completed_at,
            ),
            "publish": event_times.get(
                "review.manual.publish_completed",
                event_times.get("review.manual.publish_started"),
            ),
            "result": item.model_review_completed_at,
        }
        reliable_completion = {
            "intake": item.created_at, "context": item.context_fetched_at,
            "ci": completed_times["ci"], "planning": item.plan_created_at,
            "model": item.model_review_completed_at, "agent_batches": item.model_review_completed_at,
            "aggregating": item.model_review_completed_at,
            "approval": event_times.get("review.workflow.approve"),
            "publish": event_times.get("review.manual.publish_completed"),
        }
        result: list[ReviewStage] = []
        for index, key in enumerate(order):
            if item.snapshot_review and key in {"ci", "approval", "publish"}:
                stage_status = "skipped"
            elif terminal:
                # 终态没有仍在运行的节点，也不凭“到了最后一列”虚构已完成步骤。
                stage_status = phase if key == current_stage else "completed" if reliable_completion.get(key) else "skipped"
            elif index < current_index:
                stage_status = "completed"
            elif index == current_index:
                stage_status = (
                    "failed"
                    if failed_phase
                    else "paused" if phase == "paused"
                    else "completed" if phase == "completed" else "current"
                )
            else:
                stage_status = "pending"
            result.append(
                ReviewStage(
                    key=key,
                    status=stage_status,
                    started_at=(item.created_at if key == "intake" else None),
                    completed_at=(None if stage_status == "skipped" else
                        item.updated_at if terminal and key == current_stage else
                        reliable_completion.get(key) if terminal else completed_times[key]),
                    detail_code=(phase if index == current_index else None),
                )
            )
        return tuple(result)
