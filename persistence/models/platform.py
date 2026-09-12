"""用量、人工工作项、不可变方案和调度记录。"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from persistence.models.base import Base, utc_now


class RepositoryUsageMonthRecord(Base):
    __tablename__ = "repository_usage_months"
    __table_args__ = (
        UniqueConstraint("installation_id", "repository_key", "month"),
        CheckConstraint(
            "request_count >= 0 AND input_tokens >= 0 AND output_tokens >= 0",
            name="usage_nonnegative",
        ),
        CheckConstraint(
            "estimated_cost_microusd >= 0 AND reserved_cost_microusd >= 0",
            name="cost_nonnegative",
        ),
        CheckConstraint(
            "unknown_count >= 0 AND uncertain_count >= 0", name="unknown_nonnegative"
        ),
        Index(
            "ix_repository_usage_months_scope_month",
            "repository_key",
            "month",
            "installation_id",
        ),
        Index("ix_repository_usage_months_created", "created_at", "id"),
        Index("ix_repository_usage_months_period", "month", "created_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    repository_key: Mapped[str] = mapped_column(String(255), nullable=False)
    month: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    estimated_cost_microusd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    reserved_cost_microusd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    unknown_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    uncertain_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ModelUsageRequestRecord(Base):
    __tablename__ = "model_usage_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('reserved','settled','uncertain')", name="status_value"
        ),
        CheckConstraint(
            "purpose IN ('review','embedding','rerank')", name="purpose_value"
        ),
        CheckConstraint("reserved_cost_microusd >= 0", name="reservation_nonnegative"),
        CheckConstraint(
            "estimated_cost_microusd IS NULL OR estimated_cost_microusd >= 0",
            name="cost_nonnegative",
        ),
        Index("ix_model_usage_requests_month_created", "month_id", "created_at", "id"),
        Index("ix_model_usage_requests_run", "review_run_id", "created_at"),
        Index(
            "ix_model_usage_requests_scope_created",
            "repository_key",
            "created_at",
            "id",
        ),
        Index(
            "ix_model_usage_requests_channel_active",
            "connection_key",
            "status",
            "permit_expires_at",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    month_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 独立财务观测；来源任务清理不级联删除。
    review_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    repository_key: Mapped[str] = mapped_column(String(255), nullable=False)
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reserved_cost_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    estimated_cost_microusd: Mapped[int | None] = mapped_column(BigInteger)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    response_status: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connection_key: Mapped[str | None] = mapped_column(String(64))
    permit_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderCircuitRecord(Base):
    __tablename__ = "provider_circuits"
    __table_args__ = (Index("ix_provider_circuits_updated_at", "updated_at"),)
    connection_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class FindingWorkItemRecord(Base):
    __tablename__ = "finding_work_items"
    __table_args__ = (
        UniqueConstraint("source_finding_id"),
        CheckConstraint(
            "status IN ('open','in_progress','resolved','wont_fix')",
            name="status_value",
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        Index(
            "ix_finding_work_items_scope_created", "repository_key", "created_at", "id"
        ),
        Index(
            "ix_finding_work_items_assignee_status",
            "assignee",
            "status",
            "created_at",
            "id",
        ),
        Index("ix_finding_work_items_status_due", "status", "due_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_finding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    repository_key: Mapped[str] = mapped_column(String(255), nullable=False)
    pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    assignee: Mapped[str | None] = mapped_column(String(100))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fix_pull_request_number: Mapped[int | None] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ReviewProfileRecord(Base):
    __tablename__ = "review_profiles"
    __table_args__ = (
        Index(
            "ix_review_profiles_repository_created",
            "repository_key",
            "created_at",
            "id",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    repository_key: Mapped[str] = mapped_column(String(255), nullable=False)
    note: Mapped[str] = mapped_column(String(1000), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    ai_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    summary: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    # 所有版本凭据仅存加密信封，不通过管理 API 返回。
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RepositoryScheduleRecord(Base):
    __tablename__ = "repository_schedules"
    repository_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
