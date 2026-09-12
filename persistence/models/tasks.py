"""按职责集中维护的 tasks 数据记录。"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from domain.enums import CoverageStatus, ExecutionStatus, ReviewConclusion, WorkerStatus
from persistence.models.base import Base, enum_values, utc_now


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
        Index(
            "ix_review_runs_status_created_id", "execution_status", "created_at", "id"
        ),
        Index("ix_review_runs_pr_number", "pull_request_number"),
        Index(
            "ix_review_runs_repository_trgm",
            "repository",
            postgresql_using="gin",
            postgresql_ops={"repository": "gin_trgm_ops"},
            info={"postgresql_only": True},
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_review_runs_head_sha_trgm",
            "head_sha",
            postgresql_using="gin",
            postgresql_ops={"head_sha": "gin_trgm_ops"},
            info={"postgresql_only": True},
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_review_runs_approval_todo",
            "workflow_status",
            "approval_assignee",
            "created_at",
            "id",
        ),
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
    repository_policy: Mapped[dict[str, object] | None] = mapped_column(JSON)
    approval_assignee: Mapped[str | None] = mapped_column(String(100))
    approval_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    approval_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
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
        Index("ix_review_tasks_updated", "updated_at"),
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
    first_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
            "\"window\" IN ('hour', 'day')",
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
        Index(
            "ix_outbox_events_aggregate_occurred_id",
            "aggregate_type",
            "aggregate_id",
            "occurred_at",
            "id",
        ),
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
        Index("ix_outbox_events_type_occurred", "aggregate_type", "occurred_at", "id"),
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
