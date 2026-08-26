"""持久化审查任务提交所需的 SQLAlchemy 记录。"""

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Float,
    Index,
    Integer,
    JSON,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from domain.enums import (
    ChangedFileStatus,
    CiCheckKind,
    CiState,
    CoverageStatus,
    ExecutionStatus,
    ExternalActionState,
    FindingCategory,
    LocationSide,
    ModelApiProtocol,
    ModelCallStatus,
    ModelProvider,
    PatchState,
    PullRequestState,
    ReviewFileDecision,
    ReviewConclusion,
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
        Index("ix_review_runs_execution_status", "execution_status"),
        Index(
            "ix_review_runs_repository_pr_status",
            "repository_id",
            "pull_request_number",
            "execution_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_version_key: Mapped[str] = mapped_column(String(360), nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_status: Mapped[str] = mapped_column(String(32), nullable=False)
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


class ReviewTaskRecord(Base):
    __tablename__ = "review_tasks"
    __table_args__ = (
        CheckConstraint(
            f"execution_status IN ({enum_values(ExecutionStatus)})",
            name="execution_status_value",
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
        UniqueConstraint("event_key"),
        Index("ix_outbox_events_pending", "published_at", "occurred_at"),
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
            "max_output_tokens BETWEEN 256 AND 131072",
            name="max_output_tokens_range",
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
            "max_response_bytes BETWEEN 65536 AND 10485760",
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
    max_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8192
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
        Integer, nullable=False, default=2 * 1024 * 1024
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
    """计划中一个确定性的单文件模型输入。"""

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
    file: Mapped[str] = mapped_column(String(1024), nullable=False)
    blob_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(50), nullable=False)
    patch: Mapped[str] = mapped_column(Text, nullable=False)
    patch_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_paths: Mapped[list[str]] = mapped_column(JSON, nullable=False)
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


class ReviewFindingRecord(Base):
    """模型候选经平台补齐身份后保存的、默认未复核 Finding。"""

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
    rule_reference: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
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
    """未来 GitHub 外部副作用使用的幂等与审计状态。"""

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
        UniqueConstraint("action_key"),
        Index("ix_external_actions_run_state", "review_run_id", "state"),
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
