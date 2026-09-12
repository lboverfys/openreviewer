"""持久化审查任务提交所需的 SQLAlchemy 记录。"""

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from domain.enums import (
    ChangedFileStatus,
    CiCheckKind,
    CiState,
    CoverageStatus,
    EvidenceVerificationStatus,
    ExecutionStatus,
    ExternalActionState,
    FindingAdjudicationStatus,
    FindingCategory,
    FindingEvaluationVerdict,
    FindingLifecycleState,
    FindingOccurrenceStatus,
    LocationSide,
    ModelBatchStatus,
    ModelCallStatus,
    ModelProvider,
    ModelReasoningEffort,
    PatchState,
    PullRequestState,
    ReviewConclusion,
    ReviewFileDecision,
    Severity,
    VerificationStatus,
    WorkerStatus,
)

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    """生成 ORM 默认使用的当前 UTC 时间。

    返回：
        带 ``UTC`` 时区信息的 ``datetime``。SQLAlchemy 在创建没有显式时间值的
        记录时调用它，例如任务的 ``created_at`` 和心跳的 ``last_seen_at``。

    该函数不使用数据库服务器时间，因此测试可以通过显式传值或服务层注入时钟
    获得确定性结果；它也不会修改任何全局时钟配置。
    """
    return datetime.now(UTC)


def enum_values(enum_type: type[Enum]) -> str:
    """把 Python 枚举转换成迁移/约束所需的 SQL 字符串列表。

    数据库模型使用字符串列而不是原生数据库枚举，因此需要在 CHECK 约束中
    重复声明允许值。该辅助函数集中生成带引号的值，避免各模型手工拼接。

    参数：
        enum_type: 成员值为字符串的 Python ``Enum`` 类型。

    返回：
        以逗号分隔、每个值带单引号的 SQL 片段，例如 ``'queued', 'running'``。

    该片段只用于项目内固定枚举定义生成约束；它不是通用 SQL 转义器，不应接收
    来自用户请求的任意字符串。
    """
    return ", ".join(f"'{member.value}'" for member in enum_type)


class ReviewRunRecord(Base):
    __tablename__ = "review_runs"
    __table_args__ = (
        CheckConstraint(
            f"execution_status IN ({enum_values(ExecutionStatus)})",
            name="execution_status_value",
        ),
        CheckConstraint(
            f"workflow_status IN ({enum_values(ExecutionStatus)})",
            name="workflow_status_value",
        ),
        CheckConstraint(
            "workflow_paused_from IS NULL OR workflow_paused_from IN "
            "('queued', 'ci', 'planning', 'agent_batches', 'aggregating', "
            "'awaiting_approval', 'awaiting_publish')",
            name="workflow_paused_from_value",
        ),
        CheckConstraint(
            "review_conclusion IS NULL OR "
            f"review_conclusion IN ({enum_values(ReviewConclusion)})",
            name="review_conclusion_value",
        ),
        CheckConstraint(
            f"coverage_status IN ({enum_values(CoverageStatus)})",
            name="coverage_status_value",
        ),
        CheckConstraint("repository_id > 0", name="repository_id_positive"),
        CheckConstraint(
            "pull_request_number > 0",
            name="pull_request_number_positive",
        ),
        CheckConstraint("installation_id > 0", name="installation_id_positive"),
        UniqueConstraint("idempotency_key"),
        Index(
            "ix_review_runs_version_created",
            "review_version_key",
            "created_at",
        ),
        Index("ix_review_runs_created_at", "created_at"),
        Index("ix_review_runs_created_id", "created_at", "id"),
        Index("ix_review_runs_execution_status", "execution_status"),
        Index(
            "ix_review_runs_status_created",
            "execution_status",
            "created_at",
        ),
        Index(
            "ix_review_runs_repository_pr_status",
            "repository_id",
            "pull_request_number",
            "execution_status",
        ),
        # 资源范围查询使用规范化键，避免对 repository 原字段套 lower() 破坏普通
        # 索引；repository 仍保留 GitHub 返回的展示大小写。
        Index(
            "ix_review_runs_installation_repository_key",
            "installation_id",
            "repository_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_version_key: Mapped[str] = mapped_column(String(360), nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    repository_key: Mapped[str] = mapped_column(String(255), nullable=False)
    pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_status: Mapped[str] = mapped_column(String(32), nullable=False)
    workflow_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ExecutionStatus.QUEUED.value
    )
    # 外部 GitHub 发布跨越事务边界；令牌用于阻止旧尝试覆盖后续状态。
    publish_attempt_token: Mapped[str | None] = mapped_column(String(64))
    # ``paused`` 是独立的人工门；保存暂停前节点后，继续操作不必猜测应回到哪里。
    workflow_paused_from: Mapped[str | None] = mapped_column(String(32))
    review_conclusion: Mapped[str | None] = mapped_column(String(32))
    coverage_status: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


@event.listens_for(ReviewRunRecord, "before_insert")
@event.listens_for(ReviewRunRecord, "before_update")
def _sync_review_run_repository_key(_mapper, _connection, target) -> None:
    """在 ORM 写入边界保持资源过滤键与展示字段同步。"""

    repository = getattr(target, "repository", None)
    if isinstance(repository, str):
        target.repository_key = repository.strip().casefold()


class ReviewTaskRecord(Base):
    __tablename__ = "review_tasks"
    __table_args__ = (
        CheckConstraint(
            f"execution_status IN ({enum_values(ExecutionStatus)})",
            name="execution_status_value",
        ),
        CheckConstraint(
            f"workflow_status IN ({enum_values(ExecutionStatus)})",
            name="workflow_status_value",
        ),
        CheckConstraint(
            "workflow_paused_from IS NULL OR workflow_paused_from IN "
            "('queued', 'ci', 'planning', 'agent_batches', 'aggregating', "
            "'awaiting_approval', 'awaiting_publish')",
            name="workflow_paused_from_value",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint(
            "model_attempt_count >= 0",
            name="model_attempt_count_nonnegative",
        ),
        CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        CheckConstraint("ci_poll_count >= 0", name="ci_poll_count_nonnegative"),
        CheckConstraint(
            "claimed_from_status IS NULL OR claimed_from_status IN "
            "('queued', 'waiting_for_ci', 'ready_for_review')",
            name="claimed_from_status_value",
        ),
        UniqueConstraint("review_run_id"),
        Index(
            "ix_review_tasks_claimable",
            "execution_status",
            "available_at",
            "priority",
        ),
        Index(
            "ix_review_tasks_expired_lease",
            "execution_status",
            "lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("review_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_status: Mapped[str] = mapped_column(String(32), nullable=False)
    workflow_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ExecutionStatus.QUEUED.value
    )
    workflow_paused_from: Mapped[str | None] = mapped_column(String(32))
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=100)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_from_status: Mapped[str | None] = mapped_column(String(32))
    ci_wait_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ci_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ci_poll_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_retryable: Mapped[bool | None] = mapped_column(Boolean)
    last_error_details: Mapped[dict[str, object] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class ReviewQuotaBucketRecord(Base):
    """按作用域和时间窗口原子累计的任务创建次数。"""

    __tablename__ = "review_quota_buckets"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('user', 'repository', 'global')",
            name="scope_value",
        ),
        CheckConstraint(
            '"window" IN (\'hour\', \'day\')',
            name="window_value",
        ),
        CheckConstraint("request_count >= 0", name="request_count_nonnegative"),
        UniqueConstraint(
            "scope",
            "scope_key",
            "window",
            "window_start",
            name="uq_review_quota_bucket_identity",
        ),
        Index(
            "ix_review_quota_buckets_cleanup",
            "window_start",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False)
    window: Mapped[str] = mapped_column(String(10), nullable=False)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class WorkerHeartbeatRecord(Base):
    """一个 Worker 进程最后一次被观察到的状态。

    心跳会被持久化保存，让 Dashboard 无需仅为在线状态跟踪引入 Redis，
    也能区分空闲 Worker 与崩溃或断开连接的 Worker。
    """

    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({enum_values(WorkerStatus)})",
            name="status_value",
        ),
        Index("ix_worker_heartbeats_last_seen", "last_seen_at"),
    )

    worker_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    # 同一稳定 worker_id 的重启由新的 instance_id 接管；旧进程只能更新自己
    # 的 token，避免其延迟心跳覆盖新进程状态。
    instance_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_task_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("review_tasks.id", ondelete="SET NULL"),
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class OutboxEventRecord(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "publish_attempts >= 0",
            name="publish_attempts_nonnegative",
        ),
        CheckConstraint(
            "(publish_lease_owner IS NULL AND publish_lease_expires_at IS NULL) "
            "OR (publish_lease_owner IS NOT NULL "
            "AND publish_lease_expires_at IS NOT NULL)",
            name="publish_lease_shape",
        ),
        UniqueConstraint("event_key"),
        Index(
            "ix_outbox_events_pending",
            "published_at",
            "next_publish_attempt_at",
            "publish_lease_expires_at",
            "occurred_at",
        ),
        Index(
            "ix_outbox_events_aggregate_occurred",
            "aggregate_type",
            "aggregate_id",
            "occurred_at",
        ),
        Index("ix_outbox_events_occurred_id", "occurred_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(200), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    publish_lease_owner: Mapped[str | None] = mapped_column(String(200))
    publish_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    next_publish_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    last_publish_error: Mapped[str | None] = mapped_column(Text)


class GitHubInstallationRecord(Base):
    """通过已验签投递观察到的 GitHub App 安装记录。"""

    __tablename__ = "github_installations"
    __table_args__ = (
        CheckConstraint("id > 0", name="id_positive"),
        Index("ix_github_installations_last_seen", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AdminSessionRecord(Base):
    """可吊销的管理员会话；只保存随机会话 ID 的 SHA-256。"""

    __tablename__ = "admin_sessions"
    __table_args__ = (
        Index("ix_admin_sessions_expires_at", "expires_at"),
        Index("ix_admin_sessions_active", "revoked_at", "expires_at"),
    )

    session_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(
        String(32), nullable=False, default="administrator"
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginRateLimitRecord(Base):
    """跨 API 副本共享的固定窗口登录尝试计数。"""

    __tablename__ = "login_rate_limits"
    __table_args__ = (
        CheckConstraint("attempt_count > 0", name="attempt_count_positive"),
        Index("ix_login_rate_limits_updated_at", "updated_at"),
    )

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AiSettingsRecord(Base):
    """管理界面维护的全局 AI 配置版本与 Review Planning 预算。"""

    __tablename__ = "ai_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton_id"),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
        UniqueConstraint("revision"),
        CheckConstraint(
            "active_provider IS NULL OR "
            f"active_provider IN ({enum_values(ModelProvider)})",
            name="active_provider_value",
        ),
        CheckConstraint("max_units BETWEEN 1 AND 3000", name="max_units_range"),
        CheckConstraint(
            "max_scope_depth BETWEEN 1 AND 64",
            name="max_scope_depth_range",
        ),
        CheckConstraint(
            "max_unit_input_bytes BETWEEN 4096 AND 10485760",
            name="max_unit_input_bytes_range",
        ),
        CheckConstraint(
            "max_total_input_bytes >= max_unit_input_bytes "
            "AND max_total_input_bytes <= 104857600",
            name="max_total_input_bytes_range",
        ),
        CheckConstraint(
            "max_model_http_calls BETWEEN 1 AND 10000",
            name="max_model_http_calls_range",
        ),
        CheckConstraint(
            "max_model_input_tokens BETWEEN 1000 AND 1000000000",
            name="max_model_input_tokens_range",
        ),
        CheckConstraint(
            "max_model_output_tokens BETWEEN 256 AND 100000000",
            name="max_model_output_tokens_range",
        ),
        CheckConstraint(
            "max_model_cost_microusd IS NULL OR "
            "max_model_cost_microusd BETWEEN 1 AND 1000000000000",
            name="max_model_cost_microusd_range",
        ),
        CheckConstraint(
            "max_model_duration_seconds BETWEEN 30 AND 86400",
            name="max_model_duration_seconds_range",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_provider: Mapped[str | None] = mapped_column(String(20))
    max_units: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    max_scope_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=32)
    max_unit_input_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=192 * 1024
    )
    max_total_input_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2 * 1024 * 1024
    )
    max_model_http_calls: Mapped[int] = mapped_column(
        Integer, nullable=False, default=64
    )
    max_model_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=2_000_000
    )
    max_model_output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=250_000
    )
    max_model_cost_microusd: Mapped[int | None] = mapped_column(BigInteger)
    max_model_duration_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3_600
    )
    updated_by: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AiProviderConfigRecord(Base):
    """一个供应商的非敏感模型参数与最近连接测试状态。"""

    __tablename__ = "ai_provider_configs"
    __table_args__ = (
        CheckConstraint(
            f"provider IN ({enum_values(ModelProvider)})",
            name="provider_value",
        ),
        CheckConstraint(
            "(provider = 'openai' AND api_protocol IN "
            "('responses', 'chat_completions')) OR "
            "(provider = 'anthropic' AND api_protocol = 'messages')",
            name="api_protocol_provider",
        ),
        CheckConstraint(
            "context_window_tokens BETWEEN 8192 AND 4000000",
            name="context_window_tokens_range",
        ),
        CheckConstraint(
            "max_output_tokens BETWEEN 256 AND 131072",
            name="max_output_tokens_range",
        ),
        CheckConstraint(
            "context_window_tokens - max_output_tokens >= 4096",
            name="context_reserves_input",
        ),
        CheckConstraint(
            "reasoning_effort IN ('none', 'low', 'medium', 'high', 'max')",
            name="reasoning_effort_value",
        ),
        CheckConstraint(
            "max_batch_input_tokens BETWEEN 4096 AND 4000000",
            name="max_batch_input_tokens_range",
        ),
        CheckConstraint(
            "connect_timeout_seconds > 0 AND read_timeout_seconds > 0 "
            "AND write_timeout_seconds > 0 AND pool_timeout_seconds > 0",
            name="timeouts_positive",
        ),
        CheckConstraint(
            "max_request_bytes BETWEEN 65536 AND 10485760",
            name="max_request_bytes_range",
        ),
        CheckConstraint(
            "max_response_bytes BETWEEN 65536 AND 16777216",
            name="max_response_bytes_range",
        ),
        CheckConstraint(
            "test_status IS NULL OR test_status IN ('succeeded', 'failed')",
            name="test_status_value",
        ),
        CheckConstraint(
            "(input_usd_per_million IS NULL OR "
            "input_usd_per_million BETWEEN 0 AND 1000000) AND "
            "(output_usd_per_million IS NULL OR "
            "output_usd_per_million BETWEEN 0 AND 1000000) AND "
            "(cache_read_usd_per_million IS NULL OR "
            "cache_read_usd_per_million BETWEEN 0 AND 1000000) AND "
            "(cache_write_usd_per_million IS NULL OR "
            "cache_write_usd_per_million BETWEEN 0 AND 1000000)",
            name="prices_range",
        ),
        Index("ix_ai_provider_configs_updated_at", "updated_at"),
    )

    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    api_protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    api_base_url: Mapped[str | None] = mapped_column(String(500))
    reasoning_effort: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ModelReasoningEffort.NONE.value
    )
    context_window_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=128_000
    )
    max_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8192
    )
    max_batch_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=64_000
    )
    connect_timeout_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=5.0
    )
    read_timeout_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=180.0
    )
    write_timeout_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=30.0
    )
    pool_timeout_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=5.0
    )
    max_request_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4 * 1024 * 1024
    )
    max_response_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=16 * 1024 * 1024
    )
    input_usd_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    output_usd_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    cache_read_usd_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    cache_write_usd_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    tested_configuration_fingerprint: Mapped[str | None] = mapped_column(String(64))
    test_status: Mapped[str | None] = mapped_column(String(20))
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AiProviderSecretRecord(Base):
    """使用应用主密钥加密的一份供应商 API Key。"""

    __tablename__ = "ai_provider_secrets"
    __table_args__ = (
        CheckConstraint(
            f"provider IN ({enum_values(ModelProvider)})",
            name="provider_value",
        ),
        CheckConstraint("key_version > 0", name="key_version_positive"),
    )

    provider: Mapped[str] = mapped_column(
        String(20),
        ForeignKey(
            "ai_provider_configs.provider",
            name="fk_ai_provider_secrets_provider",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AiAgentConfigRecord(Base):
    """固定 DAG 中每个审查 Agent 的独立模型配置。"""

    __tablename__ = "ai_agent_configs"
    __table_args__ = (
        CheckConstraint(
            "agent IN ('security', 'convention', 'logic', 'summary')",
            name="agent_value",
        ),
        CheckConstraint(
            f"provider IN ({enum_values(ModelProvider)})",
            name="provider_value",
        ),
        CheckConstraint(
            "(provider = 'openai' AND api_protocol IN "
            "('responses', 'chat_completions')) OR "
            "(provider = 'anthropic' AND api_protocol = 'messages')",
            name="api_protocol_provider",
        ),
        CheckConstraint(
            "reasoning_effort IN ('none', 'low', 'medium', 'high', 'max')",
            name="reasoning_effort_value",
        ),
        CheckConstraint(
            "context_window_tokens BETWEEN 8192 AND 4000000",
            name="context_window_tokens_range",
        ),
        CheckConstraint(
            "max_output_tokens BETWEEN 256 AND 131072",
            name="max_output_tokens_range",
        ),
        CheckConstraint(
            "max_batch_input_tokens BETWEEN 4096 AND 4000000",
            name="max_batch_input_tokens_range",
        ),
        CheckConstraint(
            "connect_timeout_seconds > 0 AND read_timeout_seconds > 0 "
            "AND write_timeout_seconds > 0 AND pool_timeout_seconds > 0",
            name="timeouts_positive",
        ),
        CheckConstraint("max_retries BETWEEN 0 AND 10", name="max_retries_range"),
        CheckConstraint(
            "test_status IS NULL OR test_status IN ('succeeded', 'failed')",
            name="test_status_value",
        ),
        Index("ix_ai_agent_configs_enabled", "enabled", "updated_at"),
    )

    agent: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 开启后连接参数来自 AiSettingsRecord 当前激活的公共供应商；保留本行
    # 的独立字段用于兼容旧客户端和切回独立模式时的草稿。
    use_shared_connection: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.false()
    )
    model_override: Mapped[str | None] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    api_protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    api_base_url: Mapped[str | None] = mapped_column(String(500))
    reasoning_effort: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ModelReasoningEffort.NONE.value
    )
    context_window_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=128_000)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=8192)
    max_batch_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=64_000)
    connect_timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    read_timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=180.0)
    write_timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=30.0)
    pool_timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    test_status: Mapped[str | None] = mapped_column(String(20))
    tested_configuration_fingerprint: Mapped[str | None] = mapped_column(String(64))
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class AiAgentSecretRecord(Base):
    """每个 Agent 独立保存的加密 API Key。"""

    __tablename__ = "ai_agent_secrets"
    __table_args__ = (
        CheckConstraint("key_version > 0", name="key_version_positive"),
    )

    agent: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("ai_agent_configs.agent", ondelete="CASCADE"),
        primary_key=True,
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ConfigurationAuditRecord(Base):
    """不含配置值和密钥内容的管理员配置变更审计。"""

    __tablename__ = "configuration_audits"
    __table_args__ = (
        CheckConstraint("revision > 0", name="revision_positive"),
        UniqueConstraint("revision"),
        Index("ix_configuration_audits_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    changed_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class KnowledgeLibraryRecord(Base):
    """知识库全局版本；每次可见内容变化都会递增。"""

    __tablename__ = "knowledge_library"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton_id"),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class KnowledgeDocumentRecord(Base):
    """一份可启停、归档且指向不可变当前版本的 Markdown 文档。"""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint("current_version > 0", name="current_version_positive"),
        UniqueConstraint("source"),
        Index(
            "ix_knowledge_documents_active_source",
            "enabled",
            "archived_at",
            "source",
        ),
        Index("ix_knowledge_documents_updated_at", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class KnowledgeDocumentVersionRecord(Base):
    """知识文档的一份不可变 Markdown 内容版本。"""

    __tablename__ = "knowledge_document_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "byte_size BETWEEN 1 AND 524288",
            name="byte_size_range",
        ),
        UniqueConstraint("document_id", "version"),
        Index(
            "ix_knowledge_document_versions_document_created",
            "document_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class PullRequestVersionRecord(Base):
    """平台观察到的一份不可变 Pull Request head SHA 版本。"""

    __tablename__ = "pull_request_versions"
    __table_args__ = (
        CheckConstraint("installation_id > 0", name="installation_id_positive"),
        CheckConstraint("repository_id > 0", name="repository_id_positive"),
        CheckConstraint(
            "pull_request_number > 0", name="pull_request_number_positive"
        ),
        UniqueConstraint("review_version_key"),
        Index(
            "ix_pull_request_versions_repository_pr_seen",
            "repository_id",
            "pull_request_number",
            "last_seen_at",
        ),
        CheckConstraint(
            "changed_files_count IS NULL OR changed_files_count >= 0",
            name="changed_files_count_nonnegative",
        ),
        CheckConstraint(
            "pr_state IS NULL OR "
            f"pr_state IN ({enum_values(PullRequestState)})",
            name="pr_state_value",
        ),
        CheckConstraint(
            "ci_state IS NULL OR "
            f"ci_state IN ({enum_values(CiState)})",
            name="ci_state_value",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_version_key: Mapped[str] = mapped_column(String(360), nullable=False)
    installation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "github_installations.id",
            name="fk_pr_versions_installation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    base_sha: Mapped[str | None] = mapped_column(String(64))
    author_login: Mapped[str | None] = mapped_column(String(100))
    html_url: Mapped[str | None] = mapped_column(String(2048))
    head_repository: Mapped[str | None] = mapped_column(String(255))
    head_ref: Mapped[str | None] = mapped_column(String(1024))
    base_repository: Mapped[str | None] = mapped_column(String(255))
    base_ref: Mapped[str | None] = mapped_column(String(1024))
    identity_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    pr_state: Mapped[str | None] = mapped_column(String(16))
    is_draft: Mapped[bool | None] = mapped_column(Boolean)
    title: Mapped[str | None] = mapped_column(String(1000))
    changed_files_count: Mapped[int | None] = mapped_column(Integer)
    files_complete: Mapped[bool | None] = mapped_column(Boolean)
    diff_complete: Mapped[bool | None] = mapped_column(Boolean)
    context_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pr_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ci_state: Mapped[str | None] = mapped_column(String(32))
    ci_checks_complete: Mapped[bool | None] = mapped_column(Boolean)
    ci_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class PullRequestFileRecord(Base):
    """某个不可变 PR head SHA 下的有界变更文件快照。"""

    __tablename__ = "pull_request_files"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({enum_values(ChangedFileStatus)})",
            name="status_value",
        ),
        CheckConstraint(
            f"patch_state IN ({enum_values(PatchState)})",
            name="patch_state_value",
        ),
        CheckConstraint("additions >= 0", name="additions_nonnegative"),
        CheckConstraint("deletions >= 0", name="deletions_nonnegative"),
        CheckConstraint("changes >= 0", name="changes_nonnegative"),
        UniqueConstraint("pull_request_version_id", "path"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pull_request_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "pull_request_versions.id",
            name="fk_pr_files_version",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    previous_path: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    blob_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    additions: Mapped[int] = mapped_column(Integer, nullable=False)
    deletions: Mapped[int] = mapped_column(Integer, nullable=False)
    changes: Mapped[int] = mapped_column(Integer, nullable=False)
    patch_state: Mapped[str] = mapped_column(String(32), nullable=False)
    patch: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class PullRequestCiCheckRecord(Base):
    """某个 PR 版本最近一次观察到的 CI 检查或提交状态。"""

    __tablename__ = "pull_request_ci_checks"
    __table_args__ = (
        CheckConstraint(
            f"kind IN ({enum_values(CiCheckKind)})",
            name="kind_value",
        ),
        UniqueConstraint("pull_request_version_id", "kind", "external_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pull_request_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "pull_request_versions.id",
            name="fk_pr_ci_checks_version",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    external_key: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    conclusion: Mapped[str | None] = mapped_column(String(50))
    app_id: Mapped[int | None] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ReviewPlanRecord(Base):
    """一个审查运行在精确 PR 版本上的不可变规划结果。"""

    __tablename__ = "review_plans"
    __table_args__ = (
        CheckConstraint("candidate_count >= 0", name="candidate_count_nonnegative"),
        CheckConstraint(
            "requested_candidate_count >= 0",
            name="requested_candidate_count_nonnegative",
        ),
        CheckConstraint("rule_count >= 0", name="rule_count_nonnegative"),
        CheckConstraint("unit_count >= 0", name="unit_count_nonnegative"),
        CheckConstraint("file_count >= 0", name="file_count_nonnegative"),
        CheckConstraint(
            "total_estimated_input_bytes >= 0",
            name="total_estimated_input_bytes_nonnegative",
        ),
        CheckConstraint("model_http_calls >= 0", name="model_http_calls_nonnegative"),
        CheckConstraint(
            "model_input_tokens >= 0",
            name="model_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "model_output_tokens >= 0",
            name="model_output_tokens_nonnegative",
        ),
        CheckConstraint(
            "model_estimated_cost_microusd >= 0",
            name="model_estimated_cost_microusd_nonnegative",
        ),
        CheckConstraint(
            "model_budget_resume_count >= 0",
            name="model_budget_resume_count_nonnegative",
        ),
        CheckConstraint(
            "model_budget_mode IS NULL OR model_budget_mode IN ('observe', 'enforce')",
            name="model_budget_mode_value",
        ),
        UniqueConstraint("review_run_id"),
        Index(
            "ix_review_plans_version_fingerprint",
            "review_version_key",
            "plan_fingerprint",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_runs.id",
            name="fk_review_plans_review_run",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    pull_request_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "pull_request_versions.id",
            name="fk_review_plans_pr_version",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    review_version_key: Mapped[str] = mapped_column(String(360), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    planner_version: Mapped[str] = mapped_column(String(50), nullable=False)
    rules_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    incomplete_files: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    rule_issues: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_estimated_input_bytes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    max_model_http_calls: Mapped[int] = mapped_column(
        Integer, nullable=False, default=64
    )
    max_model_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=2_000_000
    )
    max_model_output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=250_000
    )
    max_model_cost_microusd: Mapped[int | None] = mapped_column(BigInteger)
    max_model_duration_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3_600
    )
    model_http_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    model_output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    model_estimated_cost_microusd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    model_budget_resume_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    model_budget_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    model_budget_exhausted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    model_budget_exhausted_reason: Mapped[str | None] = mapped_column(String(50))
    # 0040 迁移会把历史 NULL 回填为 ``observe``；保留可空是为了兼容尚未
    # 完成迁移或人工导入的旧行，运行时同样按 observe 兜底。新计划显式写入
    # ``observe`` 或 ``enforce``。
    model_budget_mode: Mapped[str | None] = mapped_column(String(16))
    model_review_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ReviewPlanRuleRecord(Base):
    """Review Plan 保存时使用的一份 AGENTS.md 规则内容快照。"""

    __tablename__ = "review_plan_rules"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        UniqueConstraint(
            "review_plan_id",
            "path",
            name="uq_review_plan_rules_plan_path",
        ),
        UniqueConstraint(
            "review_plan_id",
            "ordinal",
            name="uq_review_plan_rules_plan_ordinal",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_plans.id",
            name="fk_review_plan_rules_plan",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    scope: Mapped[str | None] = mapped_column(String(1014))
    blob_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)


class ReviewUnitRecord(Base):
    """计划中一个确定性的文件输入及其关联文件组。"""

    __tablename__ = "review_units"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        CheckConstraint(
            "estimated_input_bytes > 0",
            name="estimated_input_bytes_positive",
        ),
        UniqueConstraint(
            "review_plan_id",
            "unit_key",
            name="uq_review_units_plan_unit_key",
        ),
        UniqueConstraint(
            "review_plan_id",
            "file",
            name="uq_review_units_plan_file",
        ),
        UniqueConstraint(
            "review_plan_id",
            "ordinal",
            name="uq_review_units_plan_ordinal",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_plans.id",
            name="fk_review_units_plan",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_key: Mapped[str] = mapped_column(String(64), nullable=False)
    group_key: Mapped[str] = mapped_column(String(64), nullable=False)
    file: Mapped[str] = mapped_column(String(1024), nullable=False)
    blob_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(50), nullable=False)
    patch: Mapped[str] = mapped_column(Text, nullable=False)
    patch_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_paths: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # 新版计划保存 Unit 的职责范围；历史迁移会回填全部三路。
    review_domains: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    estimated_input_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    planner_version: Mapped[str] = mapped_column(String(50), nullable=False)


class ReviewFilePlanRecord(Base):
    """每个 changed file 在计划中的唯一去向。"""

    __tablename__ = "review_file_plans"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        CheckConstraint(
            f"decision IN ({enum_values(ReviewFileDecision)})",
            name="decision_value",
        ),
        CheckConstraint(
            "(decision = 'planned' AND review_unit_id IS NOT NULL) OR "
            "(decision <> 'planned' AND review_unit_id IS NULL)",
            name="decision_unit_consistency",
        ),
        UniqueConstraint(
            "review_plan_id",
            "file",
            name="uq_review_file_plans_plan_file",
        ),
        UniqueConstraint(
            "review_plan_id",
            "ordinal",
            name="uq_review_file_plans_plan_ordinal",
        ),
        UniqueConstraint("review_unit_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_plans.id",
            name="fk_review_file_plans_plan",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    review_unit_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "review_units.id",
            name="fk_review_file_plans_unit",
            ondelete="CASCADE",
        ),
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    file: Mapped[str] = mapped_column(String(1024), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)


class ModelCallRecord(Base):
    """一份 Review Plan 已完成的模型阶段调用审计。"""

    __tablename__ = "model_calls"
    __table_args__ = (
        CheckConstraint(
            f"provider IN ({enum_values(ModelProvider)})",
            name="provider_value",
        ),
        CheckConstraint(
            "(provider = 'openai' AND api_protocol IN "
            "('responses', 'chat_completions')) OR "
            "(provider = 'anthropic' AND api_protocol = 'messages')",
            name="api_protocol_provider",
        ),
        CheckConstraint(
            f"status IN ({enum_values(ModelCallStatus)})",
            name="status_value",
        ),
        CheckConstraint("duration_ms >= 0", name="duration_ms_nonnegative"),
        CheckConstraint("input_tokens >= 0", name="input_tokens_nonnegative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_nonnegative"),
        CheckConstraint(
            "cache_read_input_tokens >= 0",
            name="cache_read_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "cache_write_input_tokens >= 0",
            name="cache_write_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "reasoning_output_tokens >= 0",
            name="reasoning_output_tokens_nonnegative",
        ),
        CheckConstraint(
            "estimated_cost_microusd IS NULL OR estimated_cost_microusd >= 0",
            name="estimated_cost_microusd_nonnegative",
        ),
        CheckConstraint("finding_count >= 0", name="finding_count_nonnegative"),
        CheckConstraint(
            "response_status IS NULL OR "
            "(response_status >= 100 AND response_status <= 599)",
            name="response_status_range",
        ),
        UniqueConstraint("review_plan_id"),
        Index("ix_model_calls_provider_model_created", "provider", "model", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_plans.id",
            name="fk_model_calls_review_plan",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    configuration_revision: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    api_protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_response_id: Mapped[str | None] = mapped_column(String(200))
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    response_status: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_write_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    reasoning_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_cost_microusd: Mapped[int | None] = mapped_column(BigInteger)
    finding_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ModelReviewBatchRecord(Base):
    """一个 Agent 批次的可恢复执行记录。

    ``result`` 保存经过严格 Pydantic 校验的供应商无关结果；它不包含 Prompt、
    API Key 或模型思维链。Worker 重启后只重新领取 ``pending``/过期 ``running``
    批次，已经成功的批次不会再次调用外部模型。
    """

    __tablename__ = "model_review_batches"
    __table_args__ = (
        CheckConstraint("batch_number > 0", name="batch_number_positive"),
        CheckConstraint("batch_count > 0", name="batch_count_positive"),
        CheckConstraint(
            "batch_number <= batch_count",
            name="batch_number_within_count",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint(
            "estimated_input_tokens >= 0",
            name="estimated_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "response_status IS NULL OR "
            "(response_status >= 100 AND response_status <= 599)",
            name="response_status_range",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="duration_ms_nonnegative",
        ),
        CheckConstraint(
            f"status IN ({enum_values(ModelBatchStatus)})",
            name="status_value",
        ),
        UniqueConstraint(
            "review_plan_id",
            "agent",
            "batch_number",
            name="uq_model_review_batches_plan_agent_number",
        ),
        Index(
            "ix_model_review_batches_claimable",
            "status",
            "available_at",
            "lease_expires_at",
        ),
        Index(
            "ix_model_review_batches_plan_agent",
            "review_plan_id",
            "agent",
            "batch_number",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_plans.id",
            name="fk_model_review_batches_plan",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    agent: Mapped[str] = mapped_column(String(32), nullable=False, default="default")
    batch_number: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    estimated_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ModelBatchStatus.PENDING.value
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    response_status: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    error_details: Mapped[dict[str, object] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ModelHttpCallRecord(Base):
    """单次真实模型 HTTP 请求的预算预留与结算审计。"""

    __tablename__ = "model_http_calls"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="sequence_positive"),
        CheckConstraint("request_bytes >= 0", name="request_bytes_nonnegative"),
        CheckConstraint(
            "reserved_input_tokens >= 0",
            name="reserved_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "reserved_output_tokens >= 0",
            name="reserved_output_tokens_nonnegative",
        ),
        CheckConstraint(
            "reserved_cost_microusd >= 0",
            name="reserved_cost_microusd_nonnegative",
        ),
        CheckConstraint(
            "actual_input_tokens IS NULL OR actual_input_tokens >= 0",
            name="actual_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "actual_output_tokens IS NULL OR actual_output_tokens >= 0",
            name="actual_output_tokens_nonnegative",
        ),
        CheckConstraint(
            "actual_cost_microusd IS NULL OR actual_cost_microusd >= 0",
            name="actual_cost_microusd_nonnegative",
        ),
        CheckConstraint(
            "status IN ('reserved', 'settled', 'uncertain')",
            name="status_value",
        ),
        CheckConstraint(
            "response_status IS NULL OR "
            "(response_status >= 100 AND response_status <= 599)",
            name="response_status_range",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="duration_ms_nonnegative",
        ),
        UniqueConstraint(
            "review_plan_id",
            "sequence",
            name="uq_model_http_calls_plan_sequence",
        ),
        Index(
            "ix_model_http_calls_plan_started",
            "review_plan_id",
            "started_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_plans.id",
            name="fk_model_http_calls_review_plan",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    agent: Mapped[str] = mapped_column(String(32), nullable=False, default="default")
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    api_protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    request_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_cost_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    actual_output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    actual_cost_microusd: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FindingLifecycleRecord(Base):
    """同一仓库 PR 内按稳定指纹维护的跨提交 Finding 状态。"""

    __tablename__ = "finding_lifecycles"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({enum_values(FindingLifecycleState)})",
            name="state_value",
        ),
        CheckConstraint(
            f"last_occurrence_status IN ({enum_values(FindingOccurrenceStatus)})",
            name="last_occurrence_status_value",
        ),
        CheckConstraint("occurrence_count > 0", name="occurrence_count_positive"),
        Index(
            "ix_finding_lifecycles_pr_state",
            "repository_id",
            "pull_request_number",
            "state",
        ),
        Index(
            "ix_finding_lifecycles_fixed_run",
            "fixed_by_review_run_id",
        ),
    )

    repository_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pull_request_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    first_seen_review_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    last_seen_review_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    previous_seen_review_run_id: Mapped[str | None] = mapped_column(String(36))
    fixed_by_review_run_id: Mapped[str | None] = mapped_column(String(36))
    first_seen_head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seen_head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    last_occurrence_status: Mapped[str] = mapped_column(String(20), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    fixed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    historical_backfilled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ReviewFindingRecord(Base):
    """模型候选经平台补齐身份后保存的、默认未复核 Finding。"""

    context_references: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list, server_default=sa.text("'[]'"))
    __tablename__ = "review_findings"
    __table_args__ = (
        CheckConstraint(
            f"severity IN ({enum_values(Severity)})",
            name="severity_value",
        ),
        CheckConstraint(
            f"category IN ({enum_values(FindingCategory)})",
            name="category_value",
        ),
        CheckConstraint(
            "location_side IS NULL OR "
            f"location_side IN ({enum_values(LocationSide)})",
            name="location_side_value",
        ),
        CheckConstraint(
            f"verification_status IN ({enum_values(VerificationStatus)})",
            name="verification_status_value",
        ),
        CheckConstraint(
            f"evidence_verification_status IN ({enum_values(EvidenceVerificationStatus)})",
            name="evidence_verification_status_value",
        ),
        CheckConstraint(
            f"adjudication_status IN ({enum_values(FindingAdjudicationStatus)})",
            name="adjudication_status_value",
        ),
        CheckConstraint(
            f"lifecycle_status IN ({enum_values(FindingOccurrenceStatus)})",
            name="lifecycle_status_value",
        ),
        CheckConstraint(
            "occurrence_count > 0",
            name="occurrence_count_positive",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence_range",
        ),
        CheckConstraint(
            "(location_file IS NULL AND location_blob_sha IS NULL AND "
            "location_start_line IS NULL AND location_end_line IS NULL AND "
            "location_side IS NULL AND location_symbol IS NULL) OR "
            "(location_file IS NOT NULL AND location_blob_sha IS NOT NULL AND "
            "location_start_line > 0 AND location_end_line >= location_start_line "
            "AND location_side IS NOT NULL)",
            name="location_shape",
        ),
        UniqueConstraint(
            "review_run_id",
            "fingerprint",
            name="uq_review_findings_run_fingerprint",
        ),
        Index(
            "ix_review_findings_run_verification",
            "review_run_id",
            "verification_status",
        ),
        Index(
            "ix_review_findings_run_evidence_verification",
            "review_run_id",
            "evidence_verification_status",
        ),
        Index(
            "ix_review_findings_run_adjudication",
            "review_run_id",
            "adjudication_status",
        ),
        Index(
            "ix_review_findings_run_created_id",
            "review_run_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_review_findings_lifecycle_backfill",
            "lifecycle_backfilled_at",
            "created_at",
            "id",
        ),
        Index("ix_review_findings_head_fingerprint", "head_sha", "fingerprint"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_runs.id",
            name="fk_review_findings_review_run",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    review_plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_plans.id",
            name="fk_review_findings_review_plan",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    model_call_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "model_calls.id",
            name="fk_review_findings_model_call",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    source_unit_key: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    location_file: Mapped[str | None] = mapped_column(String(1024))
    location_blob_sha: Mapped[str | None] = mapped_column(String(64))
    location_start_line: Mapped[int | None] = mapped_column(Integer)
    location_end_line: Mapped[int | None] = mapped_column(Integer)
    location_side: Mapped[str | None] = mapped_column(String(10))
    location_in_diff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    location_symbol: Mapped[str | None] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    impact: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str] = mapped_column(Text, nullable=False)
    required_test: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    verification_status: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence_verification_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EvidenceVerificationStatus.UNVERIFIED.value,
        server_default=EvidenceVerificationStatus.UNVERIFIED.value,
    )
    evidence_verification_reason: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        default="not_checked",
        server_default="not_checked",
    )
    evidence_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    adjudication_status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=FindingAdjudicationStatus.UNREVIEWED.value,
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=FindingOccurrenceStatus.NEW.value,
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    previous_review_run_id: Mapped[str | None] = mapped_column(String(36))
    lifecycle_backfilled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    rule_reference: Mapped[str | None] = mapped_column(String(1024))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class FindingEvaluationRecord(Base):
    """一条人工 Finding 裁决形成的可重复计算评测样本。

    ``finding_id`` 只保留原始 Finding 的历史引用，不再建立外键约束。评测样本
    的生命周期独立于按保留期清理的 Finding/ReviewRun；否则删除旧运行会通过
    ``ON DELETE CASCADE`` 静默抹掉评测门禁所依赖的历史数据。
    """

    __tablename__ = "finding_evaluations"
    __table_args__ = (
        CheckConstraint(
            f"category IN ({enum_values(FindingCategory)})",
            name="category_value",
        ),
        CheckConstraint(
            f"severity IN ({enum_values(Severity)})",
            name="severity_value",
        ),
        CheckConstraint(
            f"verdict IN ({enum_values(FindingEvaluationVerdict)})",
            name="verdict_value",
        ),
        Index(
            "ix_finding_evaluations_repository_category_time",
            "repository_id",
            "category",
            "adjudicated_at",
        ),
        Index(
            "ix_finding_evaluations_repository_time",
            "repository_id",
            "adjudicated_at",
            "finding_id",
        ),
        Index(
            "ix_finding_evaluations_repository_category_verdict",
            "repository_id",
            "category",
            "verdict",
        ),
        Index(
            "ix_finding_evaluations_adjudicated_cleanup",
            "adjudicated_at",
            "finding_id",
        ),
    )

    finding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    adjudicated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    adjudicated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class GitHubWebhookDeliveryRecord(Base):
    """一条已验签并被接受的 GitHub 投递审计记录。"""

    __tablename__ = "github_webhook_deliveries"
    __table_args__ = (
        Index("ix_github_webhook_deliveries_received", "received_at"),
    )

    delivery_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    installation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "github_installations.id",
            name="fk_webhook_installation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    pull_request_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "pull_request_versions.id",
            name="fk_webhook_pr_version",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    review_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_runs.id",
            name="fk_webhook_review_run",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    review_task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_tasks.id",
            name="fk_webhook_review_task",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ExternalActionRecord(Base):
    """GitHub 外部副作用使用的幂等与审计状态。"""

    __tablename__ = "external_actions"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({enum_values(ExternalActionState)})",
            name="state_value",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="duration_ms_nonnegative",
        ),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_shape",
        ),
        UniqueConstraint("action_key"),
        Index("ix_external_actions_run_state", "review_run_id", "state"),
        Index(
            "ix_external_actions_claimable",
            "state",
            "lease_expires_at",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    action_key: Mapped[str] = mapped_column(String(300), nullable=False)
    review_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_runs.id",
            name="fk_external_actions_review_run",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    remote_id: Mapped[str | None] = mapped_column(String(200))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_method: Mapped[str | None] = mapped_column(String(10))
    request_path: Mapped[str | None] = mapped_column(String(1000))
    response_status: Mapped[int | None] = mapped_column(Integer)
    github_request_id: Mapped[str | None] = mapped_column(String(200))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    rate_limit_remaining: Mapped[int | None] = mapped_column(Integer)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_retryable: Mapped[bool | None] = mapped_column(Boolean)
    last_error_details: Mapped[dict[str, object] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

# Versioned code retrieval is independent of mutable review task state.


class RetrievalSettingsRecord(Base):
    __tablename__ = "retrieval_settings"
    __table_args__ = (CheckConstraint("id = 1", name="singleton_id"),)
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    settings: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary(12))
    key_version: Mapped[int | None] = mapped_column(Integer)
    tested_fingerprint: Mapped[str | None] = mapped_column(String(64))
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class CodeIndexRecord(Base):
    __tablename__ = "code_indexes"
    __table_args__ = (
        CheckConstraint("status IN ('queued','building','ready','failed')", name="status_value"),
        Index("ix_code_indexes_repository_created", "repository_id", "created_at"),
        Index("ix_code_indexes_status_lease", "status", "lease_until"),
        Index("ix_code_indexes_created_at", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_key: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    lexical_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    vector_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    vector_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    vector_error: Mapped[str | None] = mapped_column(String(500))
    source_target: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parsed_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reused_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reused_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    parse_errors: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CodeChunkRecord(Base):
    __tablename__ = "code_chunks"
    __table_args__ = (Index("ix_code_chunks_created_at", "created_at"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    file: Mapped[str] = mapped_column(String(1024), nullable=False)
    blob_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    symbol: Mapped[str] = mapped_column(String(1024), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    aliases: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    references: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    tokens: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    fragment: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parse_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class CodeParseRecord(Base):
    __tablename__ = "code_parse_cache"
    __table_args__ = (Index("ix_code_parse_cache_created_at", "created_at"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parser_version: Mapped[str] = mapped_column(String(50), nullable=False)
    chunks: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class CodeEmbeddingRecord(Base):
    __tablename__ = "code_embeddings"
    __table_args__ = (
        Index("ix_code_embeddings_configuration", "configuration_key"),
        Index("ix_code_embeddings_configuration_input", "configuration_key", "input_hash", unique=True),
        Index("ix_code_embeddings_created_at", "created_at"),
        Index("ix_code_embeddings_purpose_created", "purpose", "created_at"),
        Index("ix_code_embeddings_hnsw", "embedding", postgresql_using="hnsw", postgresql_ops={"embedding": "vector_cosine_ops"}),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    configuration_key: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(12), nullable=False, default="code", server_default="code")
    embedding: Mapped[list[float]] = mapped_column(Vector(1024).with_variant(JSON, "sqlite"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class CodeIndexChunkRecord(Base):
    __tablename__ = "code_index_chunks"
    __table_args__ = (
        Index("ix_code_index_chunks_embedding", "embedding_id"),
        Index("ix_code_index_chunks_chunk", "chunk_id"),
        Index("ix_code_index_chunks_index_embedding", "index_id", "embedding_id", "chunk_id"),
    )
    index_id: Mapped[str] = mapped_column(String(64), ForeignKey("code_indexes.id", ondelete="CASCADE"), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(64), ForeignKey("code_chunks.id"), primary_key=True)
    embedding_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("code_embeddings.id"), nullable=True)


class CodeRelationRecord(Base):
    __tablename__ = "code_relations"
    index_id: Mapped[str] = mapped_column(String(64), ForeignKey("code_indexes.id", ondelete="CASCADE"), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), ForeignKey("code_chunks.id"), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(64), ForeignKey("code_chunks.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), primary_key=True)


class RetrievalTraceRecord(Base):
    __tablename__ = "retrieval_traces"
    __table_args__ = (
        Index("ix_retrieval_traces_review_created", "review_run_id", "created_at"),
        UniqueConstraint("review_run_id", "plan_fingerprint", "agent", name="uq_retrieval_trace_plan_agent"),
        Index("ix_retrieval_traces_index_created", "index_id", "created_at"),
        Index("ix_retrieval_traces_temporary_created", "created_at", "id",
            postgresql_where=sa.text("review_run_id IS NULL"), sqlite_where=sa.text("review_run_id IS NULL")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    index_id: Mapped[str] = mapped_column(String(64), ForeignKey("code_indexes.id", ondelete="CASCADE"), nullable=False)
    review_run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("review_runs.id", ondelete="CASCADE"))
    agent: Mapped[str | None] = mapped_column(String(32))
    plan_fingerprint: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class RetrievalEvaluationRecord(Base):
    __tablename__ = "retrieval_evaluations"
    __table_args__ = (Index("ix_retrieval_evaluations_index_created", "index_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    index_id: Mapped[str] = mapped_column(String(64), ForeignKey("code_indexes.id", ondelete="CASCADE"), nullable=False)
    report: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class CodeSourceCacheRecord(Base):
    __tablename__ = "code_source_cache"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)


class RetrievalProviderStateRecord(Base):
    __tablename__ = "retrieval_provider_state"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetrievalRerankCacheRecord(Base):
    __tablename__ = "retrieval_rerank_cache"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ranking: Mapped[list[list[float]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)


class RetrievalRequestBudgetRecord(Base):
    __tablename__ = "retrieval_request_budgets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)


# 管理列表按排序键翻页；全文片段搜索由各表独立的 trigram 索引召回。
Index("ix_outbox_events_aggregate_occurred_id", OutboxEventRecord.aggregate_type, OutboxEventRecord.aggregate_id, OutboxEventRecord.occurred_at, OutboxEventRecord.id)
Index("ix_code_indexes_created_id", CodeIndexRecord.created_at, CodeIndexRecord.id)
Index("ix_retrieval_evaluations_created_id", RetrievalEvaluationRecord.created_at, RetrievalEvaluationRecord.id)
Index("ix_review_runs_status_created_id", ReviewRunRecord.execution_status, ReviewRunRecord.created_at, ReviewRunRecord.id)
Index("ix_review_runs_pr_number", ReviewRunRecord.pull_request_number)
for _search_model, _search_fields in (
    (ReviewRunRecord, ("repository", "head_sha")),
    (PullRequestVersionRecord, ("title", "author_login", "head_ref", "base_ref", "head_repository", "base_repository")),
):
    for _search_field in _search_fields:
        Index(f"ix_{_search_model.__tablename__}_{_search_field}_trgm", getattr(_search_model, _search_field),
              postgresql_using="gin", postgresql_ops={_search_field: "gin_trgm_ops"}, info={"postgresql_only": True}).ddl_if(dialect="postgresql")
