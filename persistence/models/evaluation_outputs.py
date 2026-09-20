"""当前请求账本对应的评测输出，来源任务清理不级联删除证据。"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from persistence.models.base import Base


class EvaluationModelOutputRecord(Base):
    __tablename__ = "evaluation_model_outputs"
    __table_args__ = (
        CheckConstraint("status IN ('pending','captured','parse_failed','transport_failed','oversized','run_limit','missing','expired')", name="status_value"),
        CheckConstraint("byte_size BETWEEN 0 AND 262144", name="byte_size_bounded"),
        Index("ix_evaluation_model_outputs_run_created", "review_run_id", "created_at", "id"),
        Index("ix_evaluation_model_outputs_expiry", "expires_at", "status", "id"),
    )
    # 与 model_usage_requests.id 共用实际请求身份；无来源任务/账本级联外键。
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    review_plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    repository_key: Mapped[str] = mapped_column(String(255), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_id: Mapped[str | None] = mapped_column(String(36))
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    batch_number: Mapped[int | None] = mapped_column(Integer)
    split_depth: Mapped[int] = mapped_column(Integer, nullable=False)
    request_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    api_protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    application_revision: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    output_format: Mapped[str] = mapped_column(String(40), nullable=False, default="provider_output_text")
    output_text: Mapped[str | None] = mapped_column(Text)
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
