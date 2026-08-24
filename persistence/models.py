"""持久化审查任务提交所需的 SQLAlchemy 记录。"""

from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    MetaData,
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
    PatchState,
    PullRequestState,
    ReviewConclusion,
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
        CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        CheckConstraint("ci_poll_count >= 0", name="ci_poll_count_nonnegative"),
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
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
