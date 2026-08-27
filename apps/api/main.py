"""OpenReviewer 的 FastAPI 入口。"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import os
from threading import RLock
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from domain.enums import (
    ExecutionStatus,
    ModelApiProtocol,
    ModelProvider,
    ModelReasoningEffort,
    ReviewAgent,
    WorkerStatus,
)
from domain.security import (
    ErrorCode,
    SafeApplicationError,
    SafeError,
    install_redacting_log_filters,
)
from domain.models import ReviewRequest
from persistence.dashboard import SqlAlchemyDashboardRepository
from persistence.database import Database, DatabaseConfigurationError
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.review_management import SqlAlchemyReviewManagementRepository
from persistence.webhooks import SqlAlchemyGitHubWebhookRepository
from services.auth import (
    AuthConfigurationError,
    AuthService,
    AuthSettings,
    InvalidSessionError,
    LoginAttemptLimiter,
    LoginRateLimitError,
    SessionPrincipal,
)
from services.ai_settings import (
    AiConnectionTestError,
    AiProviderDraft,
    AiProviderNotReadyError,
    AiSecretCipher,
    AiSettingsConfigurationError,
    AiSettingsConflictError,
    AiSettingsPersistenceError,
    AiSettingsService,
    AiSettingsValidationError,
    AiSettingsView,
    ConfigurationAuditView,
    ReviewPolicyDraft,
)
from services.agent_settings import (
    AgentConfigDraft,
    AgentConfigView,
    AgentSettingsService,
    AgentSettingsView,
)
from services.rag import (
    KnowledgeConflictError,
    KnowledgeDocumentSummary,
    KnowledgeDocumentView,
    KnowledgeLibraryView,
    KnowledgeMutationView,
    KnowledgeNotFoundError,
    KnowledgePersistenceError,
    KnowledgeValidationError,
    KnowledgeVersionView,
    ManagedMarkdownKnowledgeBase,
    MarkdownKnowledgeBase,
    RagCitation,
)
from services.dashboard import (
    DashboardPersistenceError,
    DashboardService,
    DashboardSnapshot,
    ReviewListItem,
)
from services.github import GitHubApiClient
from services.github_auth import GitHubAppSettings, GitHubAppTokenProvider
from services.github_context import GitHubReviewContextLoader
from services.github_publisher import GitHubReviewPublisher
from services.reviews import (
    IdempotencyConflictError,
    ReviewPersistenceError,
    ReviewService,
)
from services.review_management import (
    FindingDecision,
    PullRequestIdentityLoader,
    ReviewAction,
    ReviewActionConflictError,
    ReviewIdentitySyncConflictError,
    ReviewIdentitySyncUnavailableError,
    ReviewManagementPersistenceError,
    ReviewPublishUnavailableError,
    ReviewManagementService,
    ReviewNotFoundError,
    FindingNotFoundError,
    ReviewDetails,
)
from services.webhooks import (
    GitHubWebhookService,
    GitHubWebhookSettings,
    WebhookConfigurationError,
    WebhookReceipt,
    WebhookRequestError,
)


class HealthResponse(BaseModel):
    """不包含配置详情的公开存活探针响应。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    service: Literal["openreviewer"] = "openreviewer"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=512)


class AuthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    authenticated: Literal[True] = True
    username: str
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
    unverified_finding_count: int
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
    model_config = ConfigDict(frozen=True)

    id: str
    severity: str
    category: str
    title: str
    evidence: str
    impact: str
    suggestion: str
    required_test: str | None
    confidence: float
    verification_status: str
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


class ReviewStageResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    detail_code: str | None


class ReviewDetailsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    review_run_id: str
    review_task_id: str
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
    verified_finding_count: int
    rejected_finding_count: int
    unverified_finding_count: int
    findings: tuple[ReviewFindingResponse, ...]
    ci_checks: tuple[ReviewCiCheckResponse, ...]
    events: tuple[ReviewEventResponse, ...]

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
                "verified_finding_count": details.verified_finding_count,
                "rejected_finding_count": details.rejected_finding_count,
                "unverified_finding_count": details.unverified_finding_count,
                "findings": tuple(
                    ReviewFindingResponse(**asdict(finding))
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
    recent_reviews: tuple[ReviewItemResponse, ...]

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
            status_counts=dict(snapshot.status_counts),
            worker=WorkerResponse(
                **{
                    field: getattr(snapshot.worker, field)
                    for field in WorkerResponse.model_fields
                }
            ),
            recent_reviews=tuple(
                ReviewItemResponse.from_item(item)
                for item in snapshot.recent_reviews
            ),
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

    @classmethod
    def from_view(
        cls,
        view: KnowledgeDocumentView,
    ) -> "KnowledgeDocumentResponse":
        return cls(
            **{
                name: getattr(view, name)
                for name in KnowledgeDocumentSummaryResponse.model_fields
            },
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

    @classmethod
    def from_view(
        cls,
        view: KnowledgeLibraryView,
    ) -> "KnowledgeDocumentListResponse":
        return cls(
            revision=view.revision,
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
            document=KnowledgeDocumentResponse.from_view(view.document),
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
    max_response_bytes: int = Field(ge=65_536, le=10 * 1024 * 1024)
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
    model_config = ConfigDict(frozen=True)

    items: tuple[ConfigurationAuditResponse, ...]


def create_app(
    review_service: ReviewService | None = None,
    auth_service: AuthService | None = None,
    dashboard_service: DashboardService | None = None,
    login_limiter: LoginAttemptLimiter | None = None,
    webhook_service: GitHubWebhookService | None = None,
    ai_settings_service: AiSettingsService | None = None,
    agent_settings_service: AgentSettingsService | None = None,
    knowledge_base: MarkdownKnowledgeBase | None = None,
    review_management_service: ReviewManagementService | None = None,
    identity_loader: PullRequestIdentityLoader | None = None,
) -> FastAPI:
    """创建带依赖注入边界的 FastAPI 应用实例。

    参数：
        review_service: 可选审查提交服务；不传时在第一次创建任务时懒加载数据库
            仓储。测试通常传入 SQLite 仓储，避免依赖真实 PostgreSQL。
        auth_service: 可选认证服务；不传时在第一次需要认证的请求时读取环境配置。
        dashboard_service: 可选 Dashboard 查询服务；不传时按需创建数据库适配器。
        login_limiter: 可选登录失败限流器；不传时创建当前 API 进程专用实例。

    返回：
        已注册健康检查、认证、Dashboard、SSE 和任务创建路由的 ``FastAPI`` 应用。

    依赖初始化采用懒加载：只访问 ``/healthz`` 不会触发数据库或认证配置读取；
    应用关闭时只释放本函数自己创建的数据库，不会销毁调用方注入的测试资源。
    各路由内部把配置/持久化异常转换成稳定 HTTP 状态，避免泄露底层凭据。
    """

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """管理 FastAPI 应用生命周期。

        数据库采用延迟初始化，只有真正访问需要持久化的接口时才建立连接。应用
        关闭时从 ``state`` 取出由本实例拥有的数据库并释放连接池，注入的测试
        服务不会被错误地销毁。

        参数：
            application: FastAPI 传入的当前应用对象，用于读取本实例的状态容器。

        生命周期：
            进入时不主动连接数据库；路由完成后先让应用退出，再释放懒加载的
            ``owned_database``。异常不会吞掉，仍交由 ASGI 服务器报告。
        """
        install_redacting_log_filters()
        yield
        database: Database | None = application.state.owned_database
        github_api: GitHubApiClient | None = application.state.owned_github_api
        if github_api is not None:
            github_api.close()
        if database is not None:
            database.dispose()

    application = FastAPI(
        title="OpenReviewer API",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.review_service = review_service
    application.state.auth_service = auth_service
    application.state.dashboard_service = dashboard_service
    application.state.webhook_service = webhook_service
    application.state.ai_settings_service = ai_settings_service
    application.state.agent_settings_service = agent_settings_service
    application.state.knowledge_base = knowledge_base
    application.state.review_management_service = review_management_service
    application.state.identity_loader = identity_loader
    application.state.login_limiter = login_limiter or LoginAttemptLimiter()
    application.state.owned_database = None
    application.state.owned_github_api = None
    initialization_lock = RLock()

    def get_database() -> Database:
        """懒加载并缓存数据库连接。

        使用进程内锁避免并发请求同时创建多个引擎；配置缺失时转换为 503，让
        ``/healthz`` 仍可用于存活检查，同时不泄露数据库凭据或具体配置细节。

        返回：
            当前应用缓存的 ``Database`` 实例；第一次调用成功后复用同一连接池。

        异常：
            HTTPException(503): 数据库配置缺失或无效。底层配置异常被保留为原因，
            但响应只返回稳定的“持久化未配置”提示。

        进程内锁只防止同一 API 副本重复初始化，不能替代数据库连接池或跨进程锁。
        """
        configured_database: Database | None = application.state.owned_database
        if configured_database is not None:
            return configured_database
        with initialization_lock:
            configured_database = application.state.owned_database
            if configured_database is not None:
                return configured_database
            try:
                configured_database = Database.from_environment()
            except DatabaseConfigurationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="review persistence is not configured",
                ) from exc
            application.state.owned_database = configured_database
            return configured_database

    def get_review_service() -> ReviewService:
        """返回注入的或按需构造的审查提交服务。

        返回：
            优先返回 ``create_app`` 调用方注入的服务；否则用懒加载数据库会话工厂
            创建 ``SqlAlchemyReviewRepository`` 和 ``ReviewService``，并缓存到应用状态。

        该函数不提交任务；真正的请求校验、指纹计算和事务写入发生在 POST 路由
        调用服务的 ``submit`` 方法时。初始化数据库失败会通过 ``get_database``
        转换为 503。
        """
        configured_service: ReviewService | None = application.state.review_service
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.review_service
            if configured_service is None:
                configured_service = ReviewService(
                    SqlAlchemyReviewRepository(get_database().sessions)
                )
                application.state.review_service = configured_service
            return configured_service

    def get_auth_service() -> AuthService:
        """返回认证服务，并在首次使用时校验所有安全配置。

        返回：
            注入的认证服务，或根据环境变量创建并缓存的 ``AuthService``。

        异常：
            HTTPException(503): 管理员用户名、Argon2id 哈希、会话密钥或 TTL 配置
            缺失/非法。具体配置错误不会直接返回给客户端。

        健康检查不依赖该函数，因此认证配置故障不会阻止容器报告进程存活。
        """
        configured_service: AuthService | None = application.state.auth_service
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.auth_service
            if configured_service is None:
                try:
                    configured_service = AuthService(AuthSettings.from_environment())
                except AuthConfigurationError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="administrator authentication is not configured",
                    ) from exc
                application.state.auth_service = configured_service
            return configured_service

    def get_dashboard_service() -> DashboardService:
        """返回注入的或按需构造的 Dashboard 查询服务。

        返回：
            注入的服务，或使用懒加载数据库会话工厂创建并缓存的
            ``DashboardService``。

        该函数只准备查询边界，不执行 Dashboard 查询；数据库读取错误由调用方
        ``dashboard_snapshot`` 统一转换为 503。
        """
        configured_service: DashboardService | None = (
            application.state.dashboard_service
        )
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.dashboard_service
            if configured_service is None:
                configured_service = DashboardService(
                    SqlAlchemyDashboardRepository(get_database().sessions)
                )
                application.state.dashboard_service = configured_service
            return configured_service

    def get_webhook_service() -> GitHubWebhookService:
        """返回注入的服务，或按需配置验签接入服务。"""

        configured_service: GitHubWebhookService | None = (
            application.state.webhook_service
        )
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.webhook_service
            if configured_service is None:
                try:
                    settings = GitHubWebhookSettings.from_environment()
                    database = get_database()
                except (WebhookConfigurationError, HTTPException) as exc:
                    raise SafeApplicationError(
                        SafeError(
                            code=ErrorCode.WEBHOOK_NOT_CONFIGURED,
                            safe_message="GitHub Webhook 接入尚未完成配置",
                            retryable=True,
                        )
                    ) from exc
                configured_service = GitHubWebhookService(
                    SqlAlchemyGitHubWebhookRepository(database.sessions),
                    settings,
                )
                application.state.webhook_service = configured_service
            return configured_service

    def get_ai_settings_service() -> AiSettingsService:
        """返回注入的或按需创建的动态 AI 配置服务。"""

        configured_service: AiSettingsService | None = (
            application.state.ai_settings_service
        )
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.ai_settings_service
            if configured_service is None:
                try:
                    cipher = AiSecretCipher.from_environment()
                    database = get_database()
                except AiSettingsConfigurationError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="AI settings encryption is not configured",
                    ) from exc
                configured_service = AiSettingsService(database.sessions, cipher)
                application.state.ai_settings_service = configured_service
            return configured_service

    def get_agent_settings_service() -> AgentSettingsService:
        """返回固定 DAG 的独立 Agent 配置服务。"""

        configured_service: AgentSettingsService | None = (
            application.state.agent_settings_service
        )
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.agent_settings_service
            if configured_service is None:
                try:
                    cipher = AiSecretCipher.from_environment()
                    database = get_database()
                except AiSettingsConfigurationError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="AI settings encryption is not configured",
                    ) from exc
                configured_service = AgentSettingsService(database.sessions, cipher)
                application.state.agent_settings_service = configured_service
            return configured_service

    def get_knowledge_base() -> MarkdownKnowledgeBase:
        configured = application.state.knowledge_base
        if configured is None:
            with initialization_lock:
                configured = application.state.knowledge_base
                if configured is None:
                    configured = ManagedMarkdownKnowledgeBase(
                        get_database().sessions,
                        os.environ.get("OPENREVIEWER_KNOWLEDGE_ROOT", "knowledge"),
                    )
                    application.state.knowledge_base = configured
        return configured

    def get_managed_knowledge_base() -> ManagedMarkdownKnowledgeBase:
        configured = get_knowledge_base()
        if not isinstance(configured, ManagedMarkdownKnowledgeBase):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="knowledge management is not configured",
            )
        return configured

    def get_review_management_service() -> ReviewManagementService:
        """返回任务详情与人工控制服务。"""

        configured_service: ReviewManagementService | None = (
            application.state.review_management_service
        )
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.review_management_service
            if configured_service is None:
                publisher = None
                identity_loader = application.state.identity_loader
                # 详情读取不依赖 GitHub 凭据。只有 App ID 和私钥路径都存在时
                # 才装配人工发布器；配置缺失会在用户点击发布时准确返回 503，
                # 配置存在但无效则同样不会伪造发布成功。
                if (
                    os.environ.get("OPENREVIEWER_GITHUB_APP_ID", "").strip()
                    and os.environ.get(
                        "OPENREVIEWER_GITHUB_PRIVATE_KEY_FILE",
                        "",
                    ).strip()
                ):
                    github_api: GitHubApiClient | None = None
                    try:
                        github_api = GitHubApiClient()
                        github_tokens = GitHubAppTokenProvider(
                            github_api,
                            GitHubAppSettings.from_environment(),
                        )
                        configured_identity_loader = GitHubReviewContextLoader(
                            github_api,
                            github_tokens,
                        )
                        configured_publisher = GitHubReviewPublisher(
                            github_api,
                            github_tokens,
                        )
                        identity_loader = configured_identity_loader
                        publisher = configured_publisher
                        application.state.owned_github_api = github_api
                    except (ValueError, OSError):
                        if github_api is not None:
                            github_api.close()
                configured_service = ReviewManagementService(
                    SqlAlchemyReviewManagementRepository(
                        get_database().sessions,
                        publisher=publisher,
                    ),
                    identity_loader=identity_loader,
                )
                application.state.review_management_service = configured_service
            return configured_service

    def ai_settings_response() -> AiSettingsResponse:
        try:
            return AiSettingsResponse.from_view(get_ai_settings_service().get())
        except AiSettingsPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI settings are temporarily unavailable",
            ) from exc

    def translate_ai_settings_error(exc: Exception) -> HTTPException:
        """把配置领域错误转换成稳定且不含敏感信息的 HTTP 错误。"""

        if isinstance(exc, AiSettingsConflictError):
            return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        if isinstance(exc, (AiSettingsValidationError, AiProviderNotReadyError)):
            return HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            )
        if isinstance(exc, AiSettingsPersistenceError):
            return HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI settings are temporarily unavailable",
            )
        if isinstance(exc, AiSettingsConfigurationError):
            return HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI settings encryption is not configured",
            )
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI settings operation failed",
        )

    def translate_knowledge_error(exc: Exception) -> HTTPException:
        if isinstance(exc, KnowledgeNotFoundError):
            return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
        if isinstance(exc, KnowledgeConflictError):
            return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        if isinstance(exc, KnowledgeValidationError):
            return HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            )
        if isinstance(exc, KnowledgePersistenceError):
            return HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="knowledge base is temporarily unavailable",
            )
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="knowledge operation failed",
        )

    def require_principal(request: Request) -> SessionPrincipal:
        """从请求 Cookie 验证当前管理员身份。

        参数：
            request: FastAPI 当前 HTTP 请求，用于读取认证服务决定的 Cookie 名称。

        返回：
            经过 HMAC、主体和有效期校验的 ``SessionPrincipal``，供路由作为认证
            依赖使用。

        异常：
            HTTPException(401): Cookie 缺失、签名错误、主体不匹配或会话已过期；
            响应同时禁止缓存并带 ``WWW-Authenticate: Session``。
            HTTPException(503): 认证服务配置无法加载。
        """
        service = get_auth_service()
        try:
            return service.verify_session(
                request.cookies.get(service.settings.cookie_name)
            )
        except InvalidSessionError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
                headers={
                    "WWW-Authenticate": "Session",
                    "Cache-Control": "no-store",
                },
            ) from exc

    def require_same_origin(request: Request) -> None:
        """校验带副作用请求的 Origin 与当前代理入口一致。

        没有 Origin 的非浏览器调用保持兼容；有 Origin 时优先使用反向代理传入
        的协议，并只比较 scheme 和 host，防止跨站页面借用管理员 Cookie 发起
        登录、登出或创建任务请求。

        参数：
            request: 要检查的请求。只读取 ``Origin``、``Host`` 和可选的
                ``X-Forwarded-Proto`` 请求头。

        返回：
            校验通过时返回 ``None``。

        异常：
            HTTPException(403): Origin 的 scheme/host 与当前代理入口不一致。

        该依赖不验证登录身份，也不检查 CSRF Token；身份验证由
        ``require_principal`` 单独负责。没有 Origin 的请求不会被此函数拦截。
        """
        origin = request.headers.get("origin")
        if not origin:
            return
        forwarded_scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        expected = f"{forwarded_scheme}://{request.headers.get('host', '')}"
        parsed = urlsplit(origin)
        normalized_origin = f"{parsed.scheme}://{parsed.netloc}"
        if normalized_origin != expected:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cross-origin request rejected",
            )

    def dashboard_snapshot(limit: int) -> DashboardResponse:
        """读取 Dashboard 快照并把持久化故障转换为统一的 503。

        参数：
            limit: 最近任务数量，路由层的 ``Query`` 已限制为 1 到 100；SSE 使用
                固定值 50。

        返回：
            由服务层生成并映射后的 ``DashboardResponse``。

        异常：
            HTTPException(503): 数据库不可用、状态值损坏或其他 Dashboard 持久化
            错误。不会把数据库连接串、堆栈或凭据放入响应体。
        """
        try:
            snapshot = get_dashboard_service().snapshot(limit)
        except DashboardPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="dashboard data is temporarily unavailable",
            ) from exc
        return DashboardResponse.from_snapshot(snapshot)

    @application.middleware("http")
    async def add_security_headers(request: Request, call_next):
        """为每个响应补充基础安全响应头。

        认证接口额外禁止缓存，避免浏览器或中间代理保留登录结果和会话相关
        响应。更完整的 CSP、TLS 和路径白名单由外层 Nginx 负责。

        参数：
            request: 当前请求，用于判断是否为认证路径。
            call_next: Starlette 提供的下一个处理器，负责真正执行路由。

        返回：
            下游响应对象，附加 ``nosniff``、防点击劫持和 Referrer 策略；认证路径
            额外覆盖为 ``Cache-Control: no-store``。

        中间件不改变响应体，也不承担认证或限流逻辑；异常仍交给 FastAPI/ASGI
        错误处理链处理。
        """
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/v1/auth"):
            response.headers["Cache-Control"] = "no-store"
        return response

    error_statuses = {
        ErrorCode.WEBHOOK_INVALID_SIGNATURE: status.HTTP_401_UNAUTHORIZED,
        ErrorCode.WEBHOOK_INVALID_PAYLOAD: status.HTTP_400_BAD_REQUEST,
        ErrorCode.WEBHOOK_PAYLOAD_TOO_LARGE: status.HTTP_413_CONTENT_TOO_LARGE,
        ErrorCode.WEBHOOK_DELIVERY_CONFLICT: status.HTTP_409_CONFLICT,
        ErrorCode.WEBHOOK_NOT_CONFIGURED: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.WEBHOOK_PERSISTENCE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.GITHUB_AUTHENTICATION_FAILED: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.GITHUB_PERMISSION_DENIED: status.HTTP_403_FORBIDDEN,
        ErrorCode.GITHUB_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        ErrorCode.GITHUB_RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
        ErrorCode.GITHUB_TIMEOUT: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.GITHUB_SERVER_ERROR: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.GITHUB_REQUEST_REJECTED: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.GITHUB_INVALID_RESPONSE: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.GITHUB_RESPONSE_TOO_LARGE: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.MODEL_AUTHENTICATION_FAILED: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.MODEL_PERMISSION_DENIED: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.MODEL_RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
        ErrorCode.MODEL_TIMEOUT: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.MODEL_SERVER_ERROR: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.MODEL_REQUEST_REJECTED: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.MODEL_INVALID_RESPONSE: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.MODEL_RESPONSE_TOO_LARGE: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.MODEL_OUTPUT_REFUSED: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.MODEL_OUTPUT_TRUNCATED: status.HTTP_502_BAD_GATEWAY,
    }

    @application.exception_handler(SafeApplicationError)
    async def safe_application_error_handler(
        _request: Request,
        error: SafeApplicationError,
    ) -> JSONResponse:
        """只公开稳定且已脱敏的错误契约。"""

        response_status = error_statuses.get(
            error.error.code,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        return JSONResponse(
            status_code=response_status,
            content={"error": error.error.public_payload()},
            headers={"Cache-Control": "no-store"},
        )

    @application.get(
        "/healthz",
        response_model=HealthResponse,
        include_in_schema=False,
    )
    async def healthz() -> HealthResponse:
        """返回最小化的进程存活响应。

        返回：
            固定的 ``{"status": "ok", "service": "openreviewer"}`` 模型。

        该路由故意不读取数据库、不验证管理员配置，也不检查 Worker；它只证明
        API 进程和 ASGI 路由仍能响应。数据库/认证可用性由受保护业务接口另行体现。
        """
        return HealthResponse()

    @application.post(
        "/webhooks/github",
        response_model=WebhookReceiptResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def receive_github_webhook(request: Request) -> WebhookReceiptResponse:
        """限制大小、验签并原子入队一条受支持的 GitHub 投递。"""

        service = get_webhook_service()
        content_type = request.headers.get("content-type", "")
        if content_type.split(";", 1)[0].strip().casefold() != "application/json":
            raise WebhookRequestError(
                SafeError(
                    code=ErrorCode.WEBHOOK_INVALID_PAYLOAD,
                    safe_message="GitHub Webhook 必须使用 application/json",
                    retryable=False,
                )
            )
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise WebhookRequestError(
                    SafeError(
                        code=ErrorCode.WEBHOOK_INVALID_PAYLOAD,
                        safe_message="Webhook Content-Length 格式无效",
                        retryable=False,
                    )
                ) from exc
            if declared_size < 0:
                raise WebhookRequestError(
                    SafeError(
                        code=ErrorCode.WEBHOOK_INVALID_PAYLOAD,
                        safe_message="Webhook Content-Length 格式无效",
                        retryable=False,
                    )
                )
            if declared_size > service.settings.max_body_bytes:
                raise WebhookRequestError(
                    SafeError(
                        code=ErrorCode.WEBHOOK_PAYLOAD_TOO_LARGE,
                        safe_message="GitHub Webhook 请求体超过允许大小",
                        retryable=False,
                    )
                )

        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > service.settings.max_body_bytes - len(body):
                raise WebhookRequestError(
                    SafeError(
                        code=ErrorCode.WEBHOOK_PAYLOAD_TOO_LARGE,
                        safe_message="GitHub Webhook 请求体超过允许大小",
                        retryable=False,
                    )
                )
            body.extend(chunk)
        event_name = request.headers.get("x-github-event", "")
        delivery_id = request.headers.get("x-github-delivery", "")
        signature = request.headers.get("x-hub-signature-256", "")
        receipt = await run_in_threadpool(
            service.receive,
            event_name=event_name,
            delivery_id=delivery_id,
            signature=signature,
            body=bytes(body),
        )
        return WebhookReceiptResponse.from_receipt(receipt)

    @application.post(
        "/api/v1/auth/login",
        response_model=AuthResponse,
    )
    def login(
        credentials: LoginRequest,
        request: Request,
        response: Response,
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AuthResponse:
        """验证管理员凭据并设置 HttpOnly、SameSite 会话 Cookie。

        限流键由客户端地址和大小写折叠后的用户名组成；失败返回统一 401，成功
        后清除失败记录并生成服务端签名会话。密码和 Token 都不会写入响应体之外
        的持久化存储。

        参数：
            credentials: 已经过字段长度、去空白和额外字段校验的用户名/密码体。
            request: 用于提取客户端地址并参与同源校验。
            response: FastAPI 响应对象，用于设置签名会话 Cookie。

        返回：
            ``AuthResponse``，只包含管理员用户名和会话到期时间；Token 仅通过
            HttpOnly Cookie 下发。

        异常：
            HTTPException(403): Origin 与当前入口不一致。
            HTTPException(429): 当前客户端/账号在 15 分钟窗口内失败次数达到上限。
            HTTPException(401): 用户名或密码不匹配，故意不区分具体原因。
            HTTPException(503): 认证配置无法加载。
        """
        client_address = request.headers.get("x-real-ip") or (
            request.client.host if request.client is not None else "unknown"
        )
        limiter_key = f"{client_address}|{credentials.username.casefold()}"
        limiter: LoginAttemptLimiter = application.state.login_limiter
        try:
            limiter.check(limiter_key)
        except LoginRateLimitError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many login attempts; try again later",
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc

        service = get_auth_service()
        if not service.verify_credentials(credentials.username, credentials.password):
            limiter.record_failure(limiter_key)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid username or password",
            )

        limiter.reset(limiter_key)
        token, principal = service.create_session()
        response.set_cookie(
            key=service.settings.cookie_name,
            value=token,
            max_age=int(service.settings.session_ttl.total_seconds()),
            expires=principal.expires_at,
            path="/",
            secure=service.settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return AuthResponse(
            username=principal.username,
            expires_at=principal.expires_at,
        )

    @application.post(
        "/api/v1/auth/logout",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def logout(
        request: Request,
        response: Response,
        _: Annotated[None, Depends(require_same_origin)],
    ) -> None:
        """要求浏览器删除当前会话 Cookie。

        参数：
            request: 当前请求，保留在签名中以便同源依赖读取其 Origin。
            response: 用于写入过期的 ``Set-Cookie``。

        返回：
            无响应体的 204；即使客户端没有有效 Cookie，删除操作也保持幂等。

        异常：
            HTTPException(403): 请求带有不匹配的 Origin。

        当前服务不维护 Token 吊销表，因此注销的直接效果是浏览器不再发送该
        Cookie；已经复制出的旧 Token 在自然到期前仍可被密码学验证。
        """
        service = get_auth_service()
        response.delete_cookie(
            key=service.settings.cookie_name,
            path="/",
            secure=service.settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )

    @application.get(
        "/api/v1/auth/me",
        response_model=AuthResponse,
    )
    def current_user(
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> AuthResponse:
        """返回当前已验证管理员的公开会话信息。

        参数：
            principal: ``require_principal`` 已验证的会话主体。

        返回：
            用户名和绝对到期时间；不返回签名 Token、密码哈希或签名密钥。

        无额外数据库访问，调用失败只可能来自认证依赖或响应模型转换。
        """
        return AuthResponse(
            username=principal.username,
            expires_at=principal.expires_at,
        )

    @application.get(
        "/api/v1/settings/ai",
        response_model=AiSettingsResponse,
    )
    def get_ai_settings(
        _: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> AiSettingsResponse:
        """返回管理员可见的脱敏 AI 配置。"""

        return ai_settings_response()

    @application.get(
        "/api/v1/settings/ai/agents",
        response_model=AiAgentSettingsResponse,
    )
    def get_agent_settings(
        _: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> AiAgentSettingsResponse:
        """返回四个审查 Agent 的独立脱敏配置。"""

        try:
            return AiAgentSettingsResponse.from_view(
                get_agent_settings_service().get()
            )
        except (AiSettingsPersistenceError, AiSettingsConfigurationError) as exc:
            raise translate_ai_settings_error(exc) from exc

    @application.put(
        "/api/v1/settings/ai/agents/{agent}",
        response_model=AiAgentSettingsResponse,
    )
    def update_agent_settings(
        agent: ReviewAgent,
        request_body: AiAgentUpdateRequest,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AiAgentSettingsResponse:
        """保存单个 Agent 草稿；每个 Agent 的密钥和测试状态相互隔离。"""

        try:
            view = get_agent_settings_service().update(
                agent,
                request_body.to_draft(),
                expected_revision=request_body.expected_revision,
                actor=principal.username,
                api_key=request_body.api_key,
                clear_api_key=request_body.clear_api_key,
            )
        except (
            AiSettingsConfigurationError,
            AiSettingsConflictError,
            AiSettingsPersistenceError,
            AiSettingsValidationError,
        ) as exc:
            raise translate_ai_settings_error(exc) from exc
        return AiAgentSettingsResponse.from_view(view)

    @application.post(
        "/api/v1/settings/ai/agents/{agent}/test",
        response_model=AiAgentSettingsResponse,
    )
    def test_agent_settings(
        agent: ReviewAgent,
        request_body: AiRevisionRequest,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AiAgentSettingsResponse:
        """在事务外测试一个 Agent 的真实结构化连接。"""

        try:
            return AiAgentSettingsResponse.from_view(
                get_agent_settings_service().test(
                    agent,
                    expected_revision=request_body.expected_revision,
                    actor=principal.username,
                )
            )
        except AiConnectionTestError as exc:
            raise HTTPException(
                status_code=(
                    status.HTTP_503_SERVICE_UNAVAILABLE
                    if exc.retryable
                    else status.HTTP_422_UNPROCESSABLE_CONTENT
                ),
                detail=str(exc),
            ) from exc
        except (
            AiSettingsConfigurationError,
            AiSettingsConflictError,
            AiSettingsPersistenceError,
            AiSettingsValidationError,
        ) as exc:
            raise translate_ai_settings_error(exc) from exc

    @application.post(
        "/api/v1/settings/ai/agents/{agent}/enabled",
        response_model=AiAgentSettingsResponse,
    )
    def set_agent_enabled(
        agent: ReviewAgent,
        request_body: AiAgentEnabledRequest,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AiAgentSettingsResponse:
        """启用或停用一个 Agent，不影响其他 Agent 的模型配置。"""

        try:
            return AiAgentSettingsResponse.from_view(
                get_agent_settings_service().set_enabled(
                    agent,
                    request_body.enabled,
                    expected_revision=request_body.expected_revision,
                    actor=principal.username,
                )
            )
        except (
            AiSettingsConfigurationError,
            AiSettingsConflictError,
            AiSettingsPersistenceError,
            AiSettingsValidationError,
        ) as exc:
            raise translate_ai_settings_error(exc) from exc

    @application.put(
        "/api/v1/settings/ai/providers/{provider}",
        response_model=AiSettingsResponse,
    )
    def update_ai_provider(
        provider: ModelProvider,
        request_body: AiProviderUpdateRequest,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AiSettingsResponse:
        """保存一个供应商草稿；密钥只交给加密服务，不进入响应。"""

        try:
            view = get_ai_settings_service().update_provider(
                provider,
                request_body.to_draft(),
                expected_revision=request_body.expected_revision,
                actor=principal.username,
                api_key=request_body.api_key,
                clear_api_key=request_body.clear_api_key,
            )
        except (
            AiSettingsConfigurationError,
            AiSettingsConflictError,
            AiSettingsPersistenceError,
            AiSettingsValidationError,
        ) as exc:
            raise translate_ai_settings_error(exc) from exc
        return AiSettingsResponse.from_view(view)

    @application.post(
        "/api/v1/settings/ai/providers/{provider}/test",
        response_model=AiSettingsResponse,
    )
    def test_ai_provider(
        provider: ModelProvider,
        request_body: AiRevisionRequest,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AiSettingsResponse:
        """在数据库事务外发送一个最小结构化模型请求并保存测试状态。"""

        try:
            view = get_ai_settings_service().test_provider(
                provider,
                expected_revision=request_body.expected_revision,
                actor=principal.username,
            )
        except AiConnectionTestError as exc:
            raise HTTPException(
                status_code=(
                    status.HTTP_503_SERVICE_UNAVAILABLE
                    if exc.retryable
                    else status.HTTP_422_UNPROCESSABLE_CONTENT
                ),
                detail=str(exc),
            ) from exc
        except (
            AiProviderNotReadyError,
            AiSettingsConfigurationError,
            AiSettingsConflictError,
            AiSettingsPersistenceError,
            AiSettingsValidationError,
        ) as exc:
            raise translate_ai_settings_error(exc) from exc
        return AiSettingsResponse.from_view(view)

    @application.post(
        "/api/v1/settings/ai/providers/{provider}/activate",
        response_model=AiSettingsResponse,
    )
    def activate_ai_provider(
        provider: ModelProvider,
        request_body: AiRevisionRequest,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AiSettingsResponse:
        """仅激活已经通过当前配置指纹测试的供应商。"""

        try:
            view = get_ai_settings_service().activate_provider(
                provider,
                expected_revision=request_body.expected_revision,
                actor=principal.username,
            )
        except (
            AiProviderNotReadyError,
            AiSettingsConfigurationError,
            AiSettingsConflictError,
            AiSettingsPersistenceError,
        ) as exc:
            raise translate_ai_settings_error(exc) from exc
        return AiSettingsResponse.from_view(view)

    @application.put(
        "/api/v1/settings/ai/review-policy",
        response_model=AiSettingsResponse,
    )
    def update_review_policy(
        request_body: ReviewPolicyUpdateRequest,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AiSettingsResponse:
        """保存下一份 Review Plan 使用的动态输入预算。"""

        try:
            view = get_ai_settings_service().update_review_policy(
                ReviewPolicyDraft(
                    max_units=request_body.max_units,
                    max_scope_depth=request_body.max_scope_depth,
                    max_unit_input_bytes=request_body.max_unit_input_bytes,
                    max_total_input_bytes=request_body.max_total_input_bytes,
                ),
                expected_revision=request_body.expected_revision,
                actor=principal.username,
            )
        except (
            AiSettingsConflictError,
            AiSettingsPersistenceError,
            AiSettingsValidationError,
        ) as exc:
            raise translate_ai_settings_error(exc) from exc
        return AiSettingsResponse.from_view(view)

    @application.get(
        "/api/v1/settings/audits",
        response_model=ConfigurationAuditListResponse,
    )
    def list_configuration_audits(
        _: Annotated[SessionPrincipal, Depends(require_principal)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> ConfigurationAuditListResponse:
        """返回有界的配置变更审计；审计行只记录字段名。"""

        try:
            audits = get_ai_settings_service().audits(limit)
        except AiSettingsPersistenceError as exc:
            raise translate_ai_settings_error(exc) from exc
        return ConfigurationAuditListResponse(
            items=tuple(
                ConfigurationAuditResponse.from_view(item) for item in audits
            )
        )

    @application.get(
        "/api/v1/knowledge/search",
        response_model=KnowledgeSearchResponse,
    )
    def search_knowledge(
        q: Annotated[str, Query(min_length=1, max_length=500)],
        _: Annotated[SessionPrincipal, Depends(require_principal)],
        limit: Annotated[int, Query(ge=1, le=20)] = 5,
    ) -> KnowledgeSearchResponse:
        """检索版本化 Markdown 规则，返回可展示的引用来源。"""

        try:
            items = get_knowledge_base().search(q, limit=limit)
        except (
            KnowledgePersistenceError,
            KnowledgeValidationError,
        ) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeSearchResponse(
            query=q,
            items=tuple(
                KnowledgeCitationResponse(
                    **{
                        name: getattr(item, name)
                        for name in KnowledgeCitationResponse.model_fields
                    }
                )
                for item in items
            ),
        )

    @application.get(
        "/api/v1/knowledge/documents",
        response_model=KnowledgeDocumentListResponse,
    )
    def list_knowledge_documents(
        _: Annotated[SessionPrincipal, Depends(require_principal)],
        include_archived: bool = False,
        limit: Annotated[int, Query(ge=1, le=128)] = 128,
    ) -> KnowledgeDocumentListResponse:
        try:
            view = get_managed_knowledge_base().list_documents(
                include_archived=include_archived,
                limit=limit,
            )
        except (
            KnowledgeConflictError,
            KnowledgePersistenceError,
            KnowledgeValidationError,
        ) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeDocumentListResponse.from_view(view)

    @application.post(
        "/api/v1/knowledge/documents",
        response_model=KnowledgeMutationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_knowledge_document(
        request_body: KnowledgeDocumentCreateRequest,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> KnowledgeMutationResponse:
        try:
            view = get_managed_knowledge_base().create_document(
                source=request_body.source,
                content=request_body.content,
                enabled=request_body.enabled,
                expected_revision=request_body.expected_revision,
                actor=principal.username,
            )
        except (
            KnowledgeConflictError,
            KnowledgePersistenceError,
            KnowledgeValidationError,
        ) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeMutationResponse.from_view(view)

    @application.get(
        "/api/v1/knowledge/documents/{document_id}",
        response_model=KnowledgeDocumentResponse,
    )
    def get_knowledge_document(
        document_id: str,
        _: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> KnowledgeDocumentResponse:
        try:
            view = get_managed_knowledge_base().get_document(document_id)
        except (KnowledgeNotFoundError, KnowledgePersistenceError) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeDocumentResponse.from_view(view)

    @application.put(
        "/api/v1/knowledge/documents/{document_id}",
        response_model=KnowledgeMutationResponse,
    )
    def update_knowledge_document(
        document_id: str,
        request_body: KnowledgeDocumentUpdateRequest,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> KnowledgeMutationResponse:
        try:
            view = get_managed_knowledge_base().update_document(
                document_id,
                source=request_body.source,
                content=request_body.content,
                enabled=request_body.enabled,
                expected_revision=request_body.expected_revision,
                expected_document_version=request_body.expected_document_version,
                actor=principal.username,
            )
        except (
            KnowledgeConflictError,
            KnowledgeNotFoundError,
            KnowledgePersistenceError,
            KnowledgeValidationError,
        ) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeMutationResponse.from_view(view)

    @application.post(
        "/api/v1/knowledge/documents/{document_id}/archive",
        response_model=KnowledgeMutationResponse,
    )
    def archive_knowledge_document(
        document_id: str,
        request_body: KnowledgeDocumentStateRequest,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> KnowledgeMutationResponse:
        try:
            view = get_managed_knowledge_base().archive_document(
                document_id,
                archived=True,
                expected_revision=request_body.expected_revision,
                expected_document_version=request_body.expected_document_version,
                actor=principal.username,
            )
        except (
            KnowledgeConflictError,
            KnowledgeNotFoundError,
            KnowledgePersistenceError,
        ) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeMutationResponse.from_view(view)

    @application.post(
        "/api/v1/knowledge/documents/{document_id}/restore",
        response_model=KnowledgeMutationResponse,
    )
    def restore_knowledge_document(
        document_id: str,
        request_body: KnowledgeDocumentStateRequest,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> KnowledgeMutationResponse:
        try:
            view = get_managed_knowledge_base().archive_document(
                document_id,
                archived=False,
                expected_revision=request_body.expected_revision,
                expected_document_version=request_body.expected_document_version,
                actor=principal.username,
            )
        except (
            KnowledgeConflictError,
            KnowledgeNotFoundError,
            KnowledgePersistenceError,
        ) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeMutationResponse.from_view(view)

    @application.post(
        "/api/v1/knowledge/documents/{document_id}/versions/{version}/restore",
        response_model=KnowledgeMutationResponse,
    )
    def restore_knowledge_document_version(
        document_id: str,
        version: Annotated[int, Path(ge=1)],
        request_body: KnowledgeDocumentStateRequest,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> KnowledgeMutationResponse:
        try:
            view = get_managed_knowledge_base().restore_version(
                document_id,
                version,
                expected_revision=request_body.expected_revision,
                expected_document_version=request_body.expected_document_version,
                actor=principal.username,
            )
        except (
            KnowledgeConflictError,
            KnowledgeNotFoundError,
            KnowledgePersistenceError,
            KnowledgeValidationError,
        ) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeMutationResponse.from_view(view)

    @application.get(
        "/api/v1/dashboard",
        response_model=DashboardResponse,
    )
    def get_dashboard(
        _: Annotated[SessionPrincipal, Depends(require_principal)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> DashboardResponse:
        """返回认证后的任务统计、最近任务和 Worker 状态。

        参数：
            _: 仅用于触发管理员会话校验的依赖结果。
            limit: 最近任务数量，范围 1 到 100，默认 50。

        返回：
            当前数据库快照；Worker 没有心跳时会明确标记为未配置/离线，而不是
            猜测健康状态。

        异常：
            HTTPException(401): 会话无效。
            HTTPException(503): Dashboard 数据无法读取。
        """
        return dashboard_snapshot(limit)

    @application.get(
        "/api/v1/reviews",
        response_model=ReviewListResponse,
    )
    def list_reviews(
        _: Annotated[SessionPrincipal, Depends(require_principal)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> ReviewListResponse:
        """返回认证后的最近审查任务列表。

        参数：
            _: 管理员会话依赖。
            limit: 返回条数，范围 1 到 100，默认 50。

        返回：
            包含数据库中的总运行数和按创建时间倒序排列的最近任务；任务状态、
            尝试次数和最后错误来自同一次 Dashboard 读取。

        异常：
            HTTPException(401): 会话无效。
            HTTPException(503): 查询失败。
        """
        snapshot = dashboard_snapshot(limit)
        return ReviewListResponse(
            total=snapshot.total_reviews,
            items=snapshot.recent_reviews,
        )

    @application.get("/api/v1/reviews/stream")
    async def stream_reviews(
        request: Request,
        _: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> StreamingResponse:
        """建立认证后的 Server-Sent Events 实时 Dashboard 流。

        内部生成器每两秒读取一次完整快照并发送 ``dashboard`` 事件；客户端断开
        后循环自然结束。读取暂时失败只发送 ``unavailable`` 事件，不把错误数据
        伪装成正常快照；响应头关闭 Nginx 缓冲，确保前端及时收到更新。

        参数：
            request: 用于检测浏览器是否已断开连接。
            _: 建立流之前执行一次的管理员会话依赖。

        返回：
            ``text/event-stream`` 响应。每条正常事件包含哈希事件 ID、事件名和
            完整 Dashboard JSON；暂时读取失败时发送稳定的 ``unavailable`` 事件。

        注意：
            会话只在建立连接时验证一次；长连接已经建立后不会在 Cookie 到期瞬间
            被主动关闭。浏览器断线重连时 FastAPI 会重新执行认证依赖。
        """
        async def events():
            """每两秒生成一条 Dashboard SSE 事件，直到浏览器断开。

            生成器先检查 ``request.is_disconnected``，避免客户端离开后继续查询
            数据库；正常快照的 JSON 内容同时用于计算短事件 ID，便于浏览器识别
            重复数据。Dashboard 临时不可用时只发送不含内部异常的 ``unavailable``
            事件，随后等待下一轮恢复，不会结束整个连接。
            """
            while not await request.is_disconnected():
                try:
                    response_model = await run_in_threadpool(dashboard_snapshot, 50)
                    payload = response_model.model_dump_json()
                    event_id = sha256(payload.encode("utf-8")).hexdigest()[:16]
                    yield (
                        f"id: {event_id}\n"
                        "event: dashboard\n"
                        f"data: {payload}\n\n"
                    )
                except HTTPException:
                    yield (
                        "event: unavailable\n"
                        'data: {"detail":"dashboard temporarily unavailable"}\n\n'
                    )
                await asyncio.sleep(2)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    def review_details_response(review_run_id: str) -> ReviewDetailsResponse:
        """读取单条任务详情并统一转换存储异常。"""

        try:
            details = get_review_management_service().details(review_run_id)
        except ReviewNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="review task not found",
            ) from exc
        except ReviewManagementPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="review details are temporarily unavailable",
            ) from exc
        return ReviewDetailsResponse.from_details(details)

    @application.get(
        "/api/v1/reviews/{review_run_id}",
        response_model=ReviewDetailsResponse,
    )
    def get_review_details(
        review_run_id: str,
        _: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> ReviewDetailsResponse:
        """返回任务的阶段、模型结果、Finding、CI 和结构化事件日志。"""

        return review_details_response(review_run_id)

    @application.post(
        "/api/v1/reviews/{review_run_id}/identity/sync",
        response_model=ReviewDetailsResponse,
    )
    def sync_review_identity(
        review_run_id: str,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=200),
        ],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        __: Annotated[None, Depends(require_same_origin)],
    ) -> ReviewDetailsResponse:
        """从 GitHub 回查并补全历史任务的 PR 作者、链接和分支信息。"""

        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Idempotency-Key must not be blank",
            )
        try:
            details = get_review_management_service().sync_identity(
                review_run_id,
                actor=principal.username,
                request_id=normalized_key,
                loader=application.state.identity_loader,
            )
        except ReviewNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="review task not found",
            ) from exc
        except ReviewIdentitySyncUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GitHub PR identity sync is not configured",
            ) from exc
        except ReviewIdentitySyncConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except ReviewManagementPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="review identity sync is temporarily unavailable",
            ) from exc
        return ReviewDetailsResponse.from_details(details)

    @application.post(
        "/api/v1/reviews/{review_run_id}/actions",
        response_model=ReviewActionResponse,
    )
    def apply_review_action(
        review_run_id: str,
        request_body: ReviewActionRequest,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=200),
        ],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        __: Annotated[None, Depends(require_same_origin)],
    ) -> ReviewActionResponse:
        """执行可审计的加速、重试、取消或重新审查动作。"""

        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Idempotency-Key must not be blank",
            )
        try:
            management = get_review_management_service()
            new_run_id, task_id, execution_status = management.apply_action(
                review_run_id,
                request_body.action,
                actor=principal.username,
                request_id=normalized_key,
                target_stage=(
                    request_body.target_stage.value
                    if request_body.target_stage is not None
                    else None
                ),
            )
            # ``execution_status`` 是旧队列兼容字段；人工节点（尤其批准后）
            # 的真实状态只存在于固定 DAG 的 workflow_status 中。动作提交后
            # 重新读取一次已提交快照，避免用旧状态集合推断并返回 null。
            workflow_status = management.details(new_run_id).stored.workflow_status
        except ReviewNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="review task not found",
            ) from exc
        except ReviewActionConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except ReviewManagementPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="review action is temporarily unavailable",
            ) from exc
        except ReviewPublishUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GitHub publish is not configured or temporarily unavailable",
            ) from exc
        return ReviewActionResponse(
            action=request_body.action,
            review_run_id=new_run_id,
            review_task_id=task_id,
            execution_status=execution_status,
            workflow_status=workflow_status,
        )

    @application.post(
        "/api/v1/reviews/{review_run_id}/findings/{finding_id}",
        response_model=ReviewDetailsResponse,
    )
    def decide_review_finding(
        review_run_id: str,
        finding_id: str,
        request_body: ReviewFindingDecisionRequest,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=200),
        ],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        __: Annotated[None, Depends(require_same_origin)],
    ) -> ReviewDetailsResponse:
        """保存 Finding 的“确认问题/忽略”裁决并返回最新详情。"""

        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Idempotency-Key must not be blank",
            )
        try:
            details = get_review_management_service().review_finding(
                review_run_id,
                finding_id,
                request_body.decision,
                actor=principal.username,
                request_id=normalized_key,
            )
        except (ReviewNotFoundError, FindingNotFoundError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="review finding not found",
            ) from exc
        except ReviewManagementPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="finding decision is temporarily unavailable",
            ) from exc
        return ReviewDetailsResponse.from_details(details)

    @application.post(
        "/api/v1/reviews",
        response_model=ReviewAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_review(
        request_body: ReviewRequest,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=200,
            ),
        ],
        _: Annotated[SessionPrincipal, Depends(require_principal)],
        __: Annotated[None, Depends(require_same_origin)],
    ) -> ReviewAcceptedResponse:
        """校验幂等键并接受一个异步审查任务。

        请求必须先通过会话和同源检查；仓储层保证运行、任务和 Outbox 事件在同
        一个事务中持久化。相同键重复提交返回原任务，不同内容复用同一键则返回
        409，数据库暂时不可用则返回可安全重试的 503。

        参数：
            request_body: 已通过 Pydantic 严格字段校验的审查请求。
            idempotency_key: HTTP ``Idempotency-Key``，长度 1 到 200；函数会再去掉
                首尾空白，空白键返回 422。
            _: 管理员会话依赖结果，仅用于确认调用方已登录。
            __: 同源依赖结果，仅用于阻止带恶意 Origin 的浏览器副作用请求。

        返回：
            202 响应，包含运行/任务 ID、版本键、当前执行状态、首次接受时间和
            ``created`` 标志。重复请求的状态可能已经从 ``queued`` 推进到其他状态。

        异常：
            HTTPException(401/403/422/409/503): 分别对应会话无效、Origin 不匹配、
            请求或幂等键不合法、键指向不同内容、持久化不可用。
        """
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Idempotency-Key must not be blank",
            )
        try:
            result = get_review_service().submit(request_body, normalized_key)
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency-Key was already used for a different request",
            ) from exc
        except ReviewPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="review persistence is temporarily unavailable",
            ) from exc

        return ReviewAcceptedResponse(
            review_run_id=result.review_run_id,
            review_task_id=result.review_task_id,
            review_version_key=result.review_version_key,
            execution_status=result.execution_status,
            accepted_at=result.accepted_at,
            created=result.created,
        )

    return application


app = create_app()
