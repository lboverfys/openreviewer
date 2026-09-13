"""FastAPI 请求与响应的稳定 Pydantic 模型。"""

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from domain.enums import (
    EvidenceVerificationStatus,
    ExecutionStatus,
    ModelApiProtocol,
    ModelProvider,
    ModelReasoningEffort,
    ReviewAgent,
    VerificationStatus,
    WorkerStatus,
)
from domain.repository_policy import RepositoryPolicySnapshot
from domain.review_progress import BatchProgress
from services.agent_settings import AgentConfigDraft, AgentSettingsView
from services.ai_settings import (
    AiProviderDraft,
    AiSettingsView,
    ConfigurationAuditView,
)
from services.dashboard import DashboardSnapshot, ReviewListItem
from services.rag import (
    KnowledgeDocumentSummary,
    KnowledgeDocumentView,
    KnowledgeLibraryView,
    KnowledgeMutationView,
    KnowledgeVersionView,
)
from services.rbac import AccessRole, Permission
from services.review_management import FindingDecision, ReviewAction, ReviewDetails
from services.webhooks import WebhookReceipt


class HealthResponse(BaseModel):
    """不包含配置详情的公开存活探针响应。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    service: Literal["openreviewer"] = "openreviewer"


class ReadinessResponse(BaseModel):
    """不公开连接信息的数据库、迁移和 Worker 就绪结果。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["ready", "not_ready"]
    checks: dict[str, Literal["ok", "failed"]]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=512)


class AuthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    authenticated: Literal[True] = True
    username: str
    role: AccessRole
    permissions: tuple[Permission, ...]
    expires_at: datetime


class ReviewAcceptedResponse(BaseModel):
    """异步审查任务入队后的稳定确认响应。"""

    model_config = ConfigDict(frozen=True)

    review_run_id: str
    review_task_id: str
    review_version_key: str
    execution_status: ExecutionStatus
    accepted_at: datetime
    created: bool


class WebhookReceiptResponse(BaseModel):
    """GitHub 投递通过身份校验后的稳定确认响应。"""

    model_config = ConfigDict(frozen=True)

    accepted: bool
    delivery_id: str
    created: bool
    reason: str | None = None
    review_run_id: str | None = None
    review_task_id: str | None = None
    review_version_key: str | None = None
    execution_status: ExecutionStatus | None = None
    accepted_at: datetime | None = None

    @classmethod
    def from_receipt(cls, receipt: WebhookReceipt) -> "WebhookReceiptResponse":
        submission = receipt.submission
        return cls(
            accepted=receipt.accepted,
            delivery_id=receipt.delivery_id,
            created=receipt.created,
            reason=receipt.reason,
            review_run_id=(submission.review_run_id if submission else None),
            review_task_id=(submission.review_task_id if submission else None),
            review_version_key=(
                submission.review_version_key if submission else None
            ),
            execution_status=(submission.execution_status if submission else None),
            accepted_at=(submission.accepted_at if submission else None),
        )


class ReviewItemResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    review_run_id: str
    review_task_id: str
    repository: str
    pull_request_number: int
    head_sha: str
    pr_title: str | None
    pr_author_login: str | None
    pr_html_url: str | None
    head_repository: str | None
    head_ref: str | None
    base_repository: str | None
    base_ref: str | None
    execution_status: ExecutionStatus
    workflow_status: ExecutionStatus = ExecutionStatus.QUEUED
    attempt_count: int
    max_attempts: int
    last_error: str | None
    last_error_code: str | None = Field(max_length=64)
    last_error_retryable: bool | None
    last_error_details: dict[str, object] | None
    review_conclusion: str | None
    coverage_status: str
    model_review_completed_at: datetime | None
    finding_count: int
    unreviewed_finding_count: int
    model_attempt_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_item(cls, item: ReviewListItem) -> "ReviewItemResponse":
        """把服务层任务读模型转换为严格的 API 响应模型。

        参数：
            item: Dashboard 服务层返回的不可变 ``ReviewListItem``。字段名必须与
                响应模型一致，避免在每个路由里重复手写映射。

        返回：
            只包含公开字段的 ``ReviewItemResponse``；Pydantic 会再次执行类型和
            枚举序列化校验。

        该方法不访问数据库、不改变 ``item``，也不会把 ORM 对象直接暴露给 FastAPI。
        """
        return cls(**{field: getattr(item, field) for field in cls.model_fields})


class ReviewListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    items: tuple[ReviewItemResponse, ...]
    next_cursor: str | None


class ReviewEventResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    event_type: str
    payload: dict[str, object]
    occurred_at: datetime


class ReviewCiCheckResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    kind: str
    status: str
    conclusion: str | None
    observed_at: datetime


class ReviewFindingResponse(BaseModel):
    context_references: tuple[str, ...] = ()
    model_config = ConfigDict(frozen=True)

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
    verification_status: str = Field(
        deprecated=True,
        description="兼容旧客户端的机器定位校验字段；请改用 location_verification_status",
    )
    location_verification_status: VerificationStatus
    evidence_verification_status: EvidenceVerificationStatus
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


class ReviewEvaluationGateResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

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


class ReviewStageResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    detail_code: str | None


class ReviewDetailsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    repository_policy: RepositoryPolicySnapshot | None = None
    model_request_count: int | None = None
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
    last_error_details: dict[str, object] | None
    created_at: datetime
    updated_at: datetime
    pr_title: str | None
    pr_author_login: str | None
    pr_html_url: str | None
    head_repository: str | None
    head_ref: str | None
    base_repository: str | None
    base_ref: str | None
    identity_fetched_at: datetime | None
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
    plan_file_decisions: dict[str, int]
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
    current_stage: str
    phase: str
    stages: tuple[ReviewStageResponse, ...]
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
    evaluation_gates: tuple[ReviewEvaluationGateResponse, ...]
    findings: tuple[ReviewFindingResponse, ...]
    ci_checks: tuple[ReviewCiCheckResponse, ...]
    events: tuple[ReviewEventResponse, ...]
    # 每次响应都创建独立字典，避免不同请求之间共享可变状态。
    agent_statuses: dict[str, str] = Field(default_factory=dict)
    agent_summaries: dict[str, dict[str, object]] = Field(default_factory=dict)
    batch_progress: dict[str, BatchProgress] = Field(default_factory=dict)
    aggregation_status: str = "not_started"
    summary_status: str = "not_executed"
    partial_result: bool = False
    failed_agents: tuple[str, ...] = ()
    failed_batches: tuple[dict[str, object], ...] = ()

    @classmethod
    def from_details(cls, details: ReviewDetails) -> "ReviewDetailsResponse":
        stored = details.stored
        fields = {
            field: getattr(stored, field)
            for field in cls.model_fields
            if hasattr(stored, field)
        }
        fields.update(
            {
                "current_stage": details.current_stage,
                "phase": details.phase,
                "stages": tuple(
                    ReviewStageResponse(**asdict(stage)) for stage in details.stages
                ),
                "available_actions": details.available_actions,
                "finding_total_count": details.finding_total_count,
                "finding_next_cursor": details.finding_next_cursor,
                "location_verified_finding_count": (
                    details.location_verified_finding_count
                ),
                "location_rejected_finding_count": (
                    details.location_rejected_finding_count
                ),
                "location_unverified_finding_count": (
                    details.location_unverified_finding_count
                ),
                "valid_finding_count": details.valid_finding_count,
                "false_positive_finding_count": (
                    details.false_positive_finding_count
                ),
                "duplicate_finding_count": details.duplicate_finding_count,
                "out_of_scope_finding_count": (
                    details.out_of_scope_finding_count
                ),
                "known_issue_finding_count": details.known_issue_finding_count,
                "unreviewed_finding_count": details.unreviewed_finding_count,
                "new_finding_count": details.new_finding_count,
                "still_present_finding_count": (
                    details.still_present_finding_count
                ),
                "reintroduced_finding_count": (
                    details.reintroduced_finding_count
                ),
                "fixed_finding_count": details.fixed_finding_count,
                "evaluation_gates": tuple(
                    ReviewEvaluationGateResponse(**asdict(gate))
                    for gate in stored.evaluation_gates
                ),
                "findings": tuple(
                    ReviewFindingResponse(
                        **asdict(finding),
                        location_verification_status=(
                            finding.location_verification_status
                        ),
                    )
                    for finding in stored.findings
                ),
                "ci_checks": tuple(
                    ReviewCiCheckResponse(**asdict(check))
                    for check in stored.ci_checks
                ),
                "events": tuple(
                    ReviewEventResponse(**asdict(event))
                    for event in stored.events
                ),
            }
        )
        return cls(**fields)


class ReviewActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ReviewAction
    target_stage: ExecutionStatus | None = None
    retry_scope: Literal["failed_node", "stage", "new_review"] | None = None
    agent: ReviewAgent | None = None
    batch_number: int | None = Field(default=None, ge=1, le=3000)
    state_version: str | None = Field(default=None, min_length=1, max_length=128)
    head_sha: str | None = Field(default=None, min_length=40, max_length=64)


class ReviewChangeTokenResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    change_token: str


class ReviewActionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: ReviewAction
    review_run_id: str
    review_task_id: str
    execution_status: ExecutionStatus
    workflow_status: ExecutionStatus | None = None


class ReviewFindingDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: FindingDecision


class WorkerResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    configured: bool
    online: bool
    worker_id: str | None
    status: WorkerStatus | None
    current_task_id: str | None
    started_at: datetime | None
    last_seen_at: datetime | None


class DashboardResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    total_reviews: int
    status_counts: dict[ExecutionStatus, int]
    worker: WorkerResponse
    workers: tuple[WorkerResponse, ...]
    recent_reviews: tuple[ReviewItemResponse, ...]
    next_cursor: str | None
    worker_online_count: int | None = None
    worker_busy_count: int | None = None

    @classmethod
    def from_snapshot(cls, snapshot: DashboardSnapshot) -> "DashboardResponse":
        """把服务层 Dashboard 快照转换为稳定的 JSON 响应结构。

        参数：
            snapshot: ``DashboardService`` 组装的不可变快照，包含状态计数、Worker
                状态和最近任务。

        返回：
            FastAPI 可以直接序列化的 ``DashboardResponse``。映射会把只读映射复制
            成普通字典，并逐条转换最近任务，避免响应依赖服务层对象的可变行为。

        该方法只做边界适配，不重新计算在线状态、不补查数据库，也不隐藏任务错误
        文本；敏感错误的安全处理必须在持久化/服务边界完成。
        """
        return cls(
            generated_at=snapshot.generated_at,
            total_reviews=snapshot.total_reviews,
            worker_online_count=snapshot.worker_online_count,
            worker_busy_count=snapshot.worker_busy_count,
            status_counts=dict(snapshot.status_counts),
            worker=WorkerResponse(
                **{
                    field: getattr(snapshot.worker, field)
                    for field in WorkerResponse.model_fields
                }
            ),
            workers=tuple(
                WorkerResponse(
                    **{
                        field: getattr(worker, field)
                        for field in WorkerResponse.model_fields
                    }
                )
                for worker in snapshot.workers
            ),
            recent_reviews=tuple(
                ReviewItemResponse.from_item(item)
                for item in snapshot.recent_reviews
            ),
            next_cursor=snapshot.next_cursor,
        )


class AiProviderResponse(BaseModel):
    """一个供应商可公开给管理页面的脱敏配置。"""

    model_config = ConfigDict(frozen=True)

    provider: ModelProvider
    configured: bool
    active: bool
    model: str
    api_protocol: ModelApiProtocol
    api_base_url: str | None
    reasoning_effort: ModelReasoningEffort
    api_key_configured: bool
    api_key_mask: str | None
    context_window_tokens: int
    max_output_tokens: int
    max_batch_input_tokens: int
    connect_timeout_seconds: float
    read_timeout_seconds: float
    write_timeout_seconds: float
    pool_timeout_seconds: float
    max_request_bytes: int
    max_response_bytes: int
    input_usd_per_million: str | None
    output_usd_per_million: str | None
    cache_read_usd_per_million: str | None
    cache_write_usd_per_million: str | None
    test_status: Literal["untested", "succeeded", "failed"]
    tested_at: datetime | None
    updated_at: datetime | None


class AiSettingsResponse(BaseModel):
    """管理页面需要的完整 AI 配置，不包含密钥明文或密文。"""

    model_config = ConfigDict(frozen=True)

    revision: int
    active_provider: ModelProvider | None
    max_units: int
    max_scope_depth: int
    max_unit_input_bytes: int
    max_total_input_bytes: int
    updated_at: datetime | None
    updated_by: str | None
    providers: tuple[AiProviderResponse, ...]

    @classmethod
    def from_view(cls, view: AiSettingsView) -> "AiSettingsResponse":
        price_names = (
            "input_usd_per_million",
            "output_usd_per_million",
            "cache_read_usd_per_million",
            "cache_write_usd_per_million",
        )
        providers = []
        for item in view.providers:
            payload = {
                field: getattr(item, field)
                for field in AiProviderResponse.model_fields
                if field not in price_names
            }
            payload.update(
                {
                    name: (
                        format(getattr(item, name), "f")
                        if getattr(item, name) is not None
                        else None
                    )
                    for name in price_names
                }
            )
            providers.append(AiProviderResponse(**payload))
        return cls(
            revision=view.revision,
            active_provider=view.active_provider,
            max_units=view.max_units,
            max_scope_depth=view.max_scope_depth,
            max_unit_input_bytes=view.max_unit_input_bytes,
            max_total_input_bytes=view.max_total_input_bytes,
            updated_at=view.updated_at,
            updated_by=view.updated_by,
            providers=tuple(providers),
        )


class AiAgentResponse(BaseModel):
    """一个固定 DAG Agent 的脱敏独立配置。"""

    model_config = ConfigDict(frozen=True)

    agent: ReviewAgent
    configured: bool
    enabled: bool
    use_shared_connection: bool
    model_override: str | None
    shared_connection_configured: bool
    shared_connection_ready: bool
    provider: ModelProvider
    model: str
    api_protocol: ModelApiProtocol
    api_base_url: str | None
    reasoning_effort: ModelReasoningEffort
    api_key_configured: bool
    api_key_mask: str | None
    context_window_tokens: int
    max_output_tokens: int
    max_batch_input_tokens: int
    connect_timeout_seconds: float
    read_timeout_seconds: float
    write_timeout_seconds: float
    pool_timeout_seconds: float
    max_retries: int
    test_status: Literal["untested", "succeeded", "failed"]
    tested_at: datetime | None
    updated_at: datetime | None
    input_usd_per_million: Decimal | None = None
    output_usd_per_million: Decimal | None = None
    cache_read_usd_per_million: Decimal | None = None
    cache_write_usd_per_million: Decimal | None = None


class AiAgentSettingsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    revision: int
    agents: tuple[AiAgentResponse, ...]

    @classmethod
    def from_view(cls, view: AgentSettingsView) -> "AiAgentSettingsResponse":
        return cls(
            revision=view.revision,
            agents=tuple(
                AiAgentResponse(
                    **{
                        name: getattr(item, name)
                        for name in AiAgentResponse.model_fields
                    }
                )
                for item in view.agents
            ),
        )


class AiAgentUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    provider: ModelProvider
    model: str = Field(min_length=1, max_length=200)
    use_shared_connection: bool = False
    model_override: str | None = Field(default=None, max_length=200)
    api_protocol: ModelApiProtocol = ModelApiProtocol.CHAT_COMPLETIONS
    api_base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, min_length=1, max_length=65_536)
    clear_api_key: bool = False
    reasoning_effort: ModelReasoningEffort = ModelReasoningEffort.NONE
    context_window_tokens: int = Field(default=128_000, ge=8192, le=4_000_000)
    max_output_tokens: int = Field(default=8192, ge=256, le=131_072)
    max_batch_input_tokens: int = Field(default=64_000, ge=4096, le=4_000_000)
    connect_timeout_seconds: float = Field(default=5.0, gt=0, le=3600)
    read_timeout_seconds: float = Field(default=180.0, gt=0, le=3600)
    write_timeout_seconds: float = Field(default=30.0, gt=0, le=3600)
    pool_timeout_seconds: float = Field(default=5.0, gt=0, le=3600)
    max_retries: int = Field(default=2, ge=0, le=10)
    input_usd_per_million: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    output_usd_per_million: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    cache_read_usd_per_million: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    cache_write_usd_per_million: Decimal | None = Field(default=None, ge=0, le=1_000_000)

    def to_draft(self) -> AgentConfigDraft:
        return AgentConfigDraft(
            **{
                name: getattr(self, name)
                for name in AgentConfigDraft.__dataclass_fields__
            }
        )


class AiAgentEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_revision: int = Field(ge=0)
    enabled: bool


class KnowledgeCitationResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    heading: str
    score: float
    excerpt: str
    version: str


class KnowledgeSearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str
    items: tuple[KnowledgeCitationResponse, ...]


class KnowledgeVersionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int
    content_sha256: str
    byte_size: int
    created_by: str
    created_at: datetime

    @classmethod
    def from_view(cls, view: KnowledgeVersionView) -> "KnowledgeVersionResponse":
        return cls(**{name: getattr(view, name) for name in cls.model_fields})


class KnowledgeDocumentSummaryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    source: str
    title: str
    enabled: bool
    archived: bool
    current_version: int
    content_sha256: str
    byte_size: int
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(
        cls,
        view: KnowledgeDocumentSummary,
    ) -> "KnowledgeDocumentSummaryResponse":
        return cls(**{name: getattr(view, name) for name in cls.model_fields})


class KnowledgeDocumentResponse(KnowledgeDocumentSummaryResponse):
    content: str
    versions: tuple[KnowledgeVersionResponse, ...]
    version_next_cursor: str | None = None

    @classmethod
    def from_document_view(
        cls,
        view: KnowledgeDocumentView,
    ) -> "KnowledgeDocumentResponse":
        return cls(
            **{
                name: getattr(view, name)
                for name in KnowledgeDocumentSummaryResponse.model_fields
            },
            version_next_cursor=view.version_next_cursor,
            content=view.content,
            versions=tuple(
                KnowledgeVersionResponse.from_view(item) for item in view.versions
            ),
        )


class KnowledgeDocumentListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    revision: int
    total: int
    enabled_count: int
    total_enabled_bytes: int
    items: tuple[KnowledgeDocumentSummaryResponse, ...]
    offset: int = 0
    has_more: bool = False

    @classmethod
    def from_view(
        cls,
        view: KnowledgeLibraryView,
    ) -> "KnowledgeDocumentListResponse":
        return cls(
            revision=view.revision,
            offset=view.offset, has_more=view.has_more,
            total=view.total,
            enabled_count=view.enabled_count,
            total_enabled_bytes=view.total_enabled_bytes,
            items=tuple(
                KnowledgeDocumentSummaryResponse.from_view(item)
                for item in view.items
            ),
        )


class KnowledgeMutationResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    revision: int
    document: KnowledgeDocumentResponse

    @classmethod
    def from_view(
        cls,
        view: KnowledgeMutationView,
    ) -> "KnowledgeMutationResponse":
        return cls(
            revision=view.revision,
            document=KnowledgeDocumentResponse.from_document_view(view.document),
        )


class KnowledgeDocumentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    source: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=524_288)
    enabled: bool = True


class KnowledgeDocumentUpdateRequest(KnowledgeDocumentCreateRequest):
    expected_document_version: int = Field(ge=1)


class KnowledgeDocumentStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_revision: int = Field(ge=0)
    expected_document_version: int = Field(ge=1)


class AiProviderUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    model: str = Field(min_length=1, max_length=200)
    api_protocol: ModelApiProtocol
    api_base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, min_length=1, max_length=65_536)
    clear_api_key: bool = False
    reasoning_effort: ModelReasoningEffort = ModelReasoningEffort.NONE
    context_window_tokens: int = Field(ge=8_192, le=4_000_000)
    max_output_tokens: int = Field(ge=256, le=131_072)
    max_batch_input_tokens: int = Field(
        default=64_000,
        ge=4_096,
        le=4_000_000,
    )
    connect_timeout_seconds: float = Field(gt=0, le=3600)
    read_timeout_seconds: float = Field(gt=0, le=3600)
    write_timeout_seconds: float = Field(gt=0, le=3600)
    pool_timeout_seconds: float = Field(gt=0, le=3600)
    max_request_bytes: int = Field(ge=65_536, le=10 * 1024 * 1024)
    max_response_bytes: int = Field(ge=65_536, le=16 * 1024 * 1024)
    input_usd_per_million: Decimal | None = Field(
        default=None, ge=0, le=1_000_000, decimal_places=6
    )
    output_usd_per_million: Decimal | None = Field(
        default=None, ge=0, le=1_000_000, decimal_places=6
    )
    cache_read_usd_per_million: Decimal | None = Field(
        default=None, ge=0, le=1_000_000, decimal_places=6
    )
    cache_write_usd_per_million: Decimal | None = Field(
        default=None, ge=0, le=1_000_000, decimal_places=6
    )

    def to_draft(self) -> AiProviderDraft:
        return AiProviderDraft(
            **{
                field: getattr(self, field)
                for field in AiProviderDraft.__dataclass_fields__
            }
        )


class AiRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_revision: int = Field(ge=0)


class ReviewPolicyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_revision: int = Field(ge=0)
    max_units: int = Field(ge=1, le=3000)
    max_scope_depth: int = Field(ge=1, le=64)
    max_unit_input_bytes: int = Field(ge=4096, le=10 * 1024 * 1024)
    max_total_input_bytes: int = Field(ge=4096, le=100 * 1024 * 1024)


class ConfigurationAuditResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    revision: int
    actor: str
    action: str
    changed_fields: tuple[str, ...]
    created_at: datetime

    @classmethod
    def from_view(
        cls,
        view: ConfigurationAuditView,
    ) -> "ConfigurationAuditResponse":
        return cls(
            **{field: getattr(view, field) for field in cls.model_fields}
        )


class ConfigurationAuditListResponse(BaseModel):
    next_cursor: str | None = None
    model_config = ConfigDict(frozen=True)

    items: tuple[ConfigurationAuditResponse, ...]
