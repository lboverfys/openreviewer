"""团队运行管理的边界契约；不依赖数据库或 Web 框架。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domain.enums import ReviewAgent
from domain.evaluation_workbench import EvaluationComparisonReport
from domain.security import ErrorCode, SafeApplicationError, SafeError


class PlatformNotFoundError(LookupError):
    """资源不存在或不在当前成员的范围内。"""


class PlatformConflictError(ValueError):
    """对象状态或版本已经变化。"""


class MonthlyBudgetExceededError(SafeApplicationError):
    def __init__(self, *, unknown_price: bool = False) -> None:
        super().__init__(
            SafeError(
                code=ErrorCode.MODEL_BUDGET_EXCEEDED,
                safe_message=(
                    "该仓库启用了月度预算，请先配置完整模型价格"
                    if unknown_price
                    else "仓库本月可用预算不足，任务已暂停"
                ),
                retryable=False,
                details={
                    "budget_reason": "repository_monthly_budget",
                    "unknown_price": unknown_price,
                },
            )
        )


class ModelChannelUnavailableError(SafeApplicationError):
    def __init__(self, now: datetime, *, retry_at: datetime | None = None) -> None:
        super().__init__(
            SafeError(
                code=ErrorCode.MODEL_BATCH_BUSY,
                safe_message="模型供应商通道繁忙或暂时熔断，将按退避时间重试",
                retryable=True,
                details={
                    "batch_retry_managed": True,
                    "provider_channel": True,
                    "retry_at": (retry_at or now + timedelta(seconds=10)).isoformat(),
                },
            )
        )


class PlatformModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", from_attributes=True, str_strip_whitespace=True
    )


class UsageMonth(PlatformModel):
    id: str
    installation_id: int
    repository: str
    month: str
    request_count: int
    input_tokens: int
    output_tokens: int
    estimated_cost_microusd: int
    reserved_cost_microusd: int
    unknown_count: int
    uncertain_count: int
    budget_microusd: int | None
    warning_percent: int
    warning: bool
    created_at: datetime


class UsageRequest(PlatformModel):
    id: str
    review_run_id: str
    repository: str
    agent: str
    purpose: str
    model: str
    status: str
    estimated_cost_microusd: int | None
    reserved_cost_microusd: int
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    response_status: int | None
    created_at: datetime


class RequestStatistics(PlatformModel):
    request_count: int
    estimated_cost_microusd: int
    unknown_count: int
    known_count: int = 0
    reserved_count: int = 0
    uncertain_count: int = 0
    settled_count: int = 0
    http_2xx_count: int = 0
    http_non_2xx_count: int = 0
    http_unknown_count: int = 0
    duration_sample_count: int = 0
    p50_duration_ms: int | None = None
    p95_duration_ms: int | None = None
    settled_priced_count: int = 0
    settled_reservation_microusd: int = 0
    settled_cost_microusd: int = 0


class UsageBreakdown(RequestStatistics):
    model: str
    purpose: str
    provider: str | None = None
    agent: str | None = None
    group_by: Literal["model", "agent"] = "model"
    total_request_count: int = 0
    total_estimated_cost_microusd: int = 0
    total_unknown_count: int = 0
    known_cost_share: float | None = None
    groups_truncated: bool = False


WorkStatus = Literal["open", "in_progress", "resolved", "wont_fix"]


class WorkItemCreate(PlatformModel):
    finding_id: str = Field(min_length=1, max_length=36)
    assignee: str | None = Field(default=None, min_length=1, max_length=100)
    due_at: datetime | None = None

    @model_validator(mode="after")
    def timezone_required(self):
        if self.due_at is not None and self.due_at.tzinfo is None:
            raise ValueError("截止时间必须包含时区")
        return self


class WorkItemUpdate(PlatformModel):
    expected_revision: int = Field(ge=1)
    status: WorkStatus
    assignee: str | None = Field(default=None, min_length=1, max_length=100)
    due_at: datetime | None = None
    note: str = Field(default="", max_length=2000)
    fix_pull_request_number: int | None = Field(default=None, ge=1, le=2_147_483_647)

    @model_validator(mode="after")
    def resolution_required(self):
        if self.due_at is not None and self.due_at.tzinfo is None:
            raise ValueError("截止时间必须包含时区")
        if self.status in {"resolved", "wont_fix"} and not self.note.strip():
            raise ValueError("确认修复或暂不修复时必须说明原因")
        return self


class WorkItemView(PlatformModel):
    id: str
    source_run_id: str
    source_finding_id: str
    repository: str
    pull_request_number: int
    title: str
    severity: str
    status: WorkStatus
    assignee: str | None
    due_at: datetime | None
    note: str
    fix_pull_request_number: int | None
    revision: int
    created_at: datetime
    updated_at: datetime


class ApprovalTodo(PlatformModel):
    id: str
    repository: str
    pull_request_number: int
    assignee: str | None
    requested_at: datetime | None
    due_at: datetime | None
    created_at: datetime


class ProfileCreate(PlatformModel):
    name: str = Field(min_length=1, max_length=120)
    repository: str = Field(min_length=3, max_length=255)
    note: str = Field(default="", max_length=1000)
    expected_ai_revision: int | None = Field(default=None, ge=0)
    base_profile_id: str | None = Field(default=None, min_length=1, max_length=36)
    role_instructions: dict[ReviewAgent, Annotated[str, Field(min_length=1, max_length=6000)]] = Field(default_factory=dict, max_length=4)
    supplementary_instructions: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_source(self):
        if self.base_profile_id is None:
            if self.expected_ai_revision is None:
                raise ValueError("保存当前配置需要 AI 配置版本")
            if self.role_instructions or self.supplementary_instructions is not None:
                raise ValueError("Prompt 编辑必须基于已保存的不可变方案")
        return self


class KnowledgeProposalWrite(PlatformModel):
    expected_work_revision: int = Field(ge=1)
    expected_library_revision: int = Field(ge=0)
    lesson: str = Field(min_length=1, max_length=4000)


class KnowledgeProposalView(PlatformModel):
    document_id: str
    source: str
    library_revision: int


class ProfileView(PlatformModel):
    id: str
    name: str
    repository: str
    note: str
    fingerprint: str
    ai_revision: int
    prompt_version: str
    prompt_content_sha256: str | None = None
    base_profile_id: str | None = None
    role_instructions: dict[str, str] = Field(default_factory=dict)
    supplementary_instructions: str = ""
    models: dict[str, str]
    knowledge_versions: dict[str, str]
    retrieval_settings: dict[str, object]
    created_by: str
    created_at: datetime


class ProfileActivate(PlatformModel):
    expected_repository_revision: int = Field(ge=1)
    evaluation_dataset_id: str | None = Field(default=None, min_length=1, max_length=36)
    evidence_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="", max_length=1000)


class ProfileQuality(PlatformModel):
    profile_id: str
    baseline_profile_id: str | None
    status: Literal["unverified", "regression", "reviewed"]
    evidence_token: str
    reasons: tuple[str, ...]
    report: EvaluationComparisonReport | None = None


class PlatformAudit(PlatformModel):
    id: str
    event_type: str
    actor: str
    repository: str
    object_id: str
    revision: int | None
    created_at: datetime


class RepositoryDiagnostic(PlatformModel):
    repository: str
    queued: int
    running: int
    paused: int
    failed: int
    completed: int
    oldest_queued_at: datetime | None
    mean_queue_ms: float | None
    p95_queue_ms: float | None
    mean_model_ms: float | None
    max_concurrent_reviews: int | None


class FailureDiagnostic(PlatformModel):
    code: str
    count: int


class CompletedWorkflowCost(PlatformModel):
    completed_runs: int
    priced_runs: int
    missing_ledger_runs: int
    incomplete_cost_runs: int
    mean_estimated_cost_microusd: float | None
    mean_turnaround_ms: float | None


class BatchHealth(PlatformModel):
    total: int
    pending: int
    running: int
    succeeded: int
    failed: int
    claimed: int
    reclaimed: int
    terminal_claimed: int
    single_claim_succeeded: int
    reused_batches: int
    estimated_avoided_input_tokens: int
    reused_input_unknown_batches: int


class EvidenceHealth(PlatformModel):
    total: int
    matched: int
    unmatched: int
    infrastructure: int
    not_covered: int
    unclassified: int
    automatic_coverage: float | None
    eligible_pass_rate: float | None


class EvidenceReasonCount(PlatformModel):
    status: str | None
    reason: str | None
    category: str
    count: int


class RetrievalCacheHealth(PlatformModel):
    groups: int
    query_recorded_groups: int
    query_all_hit_groups: int
    rerank_recorded_groups: int
    rerank_all_hit_groups: int


class IndexReuseHealth(PlatformModel):
    indexes: int
    parsed_files: int
    reused_files: int
    embedded_vectors: int
    reused_vectors: int


class ReviewInsights(PlatformModel):
    requests: RequestStatistics
    completed_cost: CompletedWorkflowCost
    batches: BatchHealth
    batch_errors: tuple[FailureDiagnostic, ...]
    batch_errors_truncated: bool = False
    evidence: EvidenceHealth
    evidence_reasons: tuple[EvidenceReasonCount, ...]
    evidence_reasons_truncated: bool = False
    retrieval_cache: RetrievalCacheHealth
    index_reuse: IndexReuseHealth


class DiagnosticReport(PlatformModel):
    since: datetime
    until: datetime
    repositories: tuple[RepositoryDiagnostic, ...]
    failures: tuple[FailureDiagnostic, ...]
    truncated: bool = False
    provider_channels: tuple[ProviderChannelView, ...] = ()
    insights: ReviewInsights | None = None


class ProviderChannelView(PlatformModel):
    key: str
    provider: str
    in_flight: int
    failure_count: int
    open_until: datetime | None


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
