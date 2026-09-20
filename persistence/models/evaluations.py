"""按职责集中维护的 evaluations 数据记录。"""

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
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from persistence.models.base import Base, utc_now


class EvaluationDatasetRecord(Base):
    """一个仓库的评测集；调参和验收样本在集内明确分开。"""

    __tablename__ = "evaluation_datasets"
    __table_args__ = (
        UniqueConstraint("request_key"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("review_mode IN ('single','dual')", name="review_mode_value"),
        CheckConstraint("case_count BETWEEN 0 AND 200", name="case_count_bounded"),
        Index(
            "ix_evaluation_datasets_archive_created", "archived_at", "created_at", "id"
        ),
        Index(
            "ix_evaluation_datasets_repository_created",
            "repository_key",
            "created_at",
            "id",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    review_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="single", server_default="single",
    )
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    repository_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class EvaluationCaseRecord(Base):
    __tablename__ = "evaluation_cases"
    __table_args__ = (
        # 同一个 PR 不得通过不同 SHA 混入调参集和验收集。
        UniqueConstraint("dataset_id", "pull_request_number"),
        CheckConstraint("split IN ('tuning','validation')", name="split_value"),
        CheckConstraint(
            "kind IN ('normal','known_defect','cross_file')", name="kind_value"
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        Index("ix_evaluation_cases_dataset_created", "dataset_id", "created_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("evaluation_datasets.id", ondelete="CASCADE"),
        nullable=False,
    )
    pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    split: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    reference_defects: Mapped[list[dict[str, object]] | None] = mapped_column(JSON)
    reference_reviews: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    reference_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    reference_count: Mapped[int | None] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class EvaluationObservationRecord(Base):
    """独立保留的审查结果快照；来源运行不设级联外键。"""

    __tablename__ = "evaluation_observations"
    __table_args__ = (
        UniqueConstraint("case_id", "variant"),
        CheckConstraint("variant IN ('baseline','candidate')", name="variant_value"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint(
            "finding_count BETWEEN 0 AND 500", name="finding_count_bounded"
        ),
        CheckConstraint(
            "assessment_status IN ('pending','partial','disputed','complete')",
            name="assessment_status_value",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("evaluation_cases.id", ondelete="CASCADE"),
        nullable=False,
    )
    variant: Mapped[str] = mapped_column(String(16), nullable=False)
    source_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    model_label: Mapped[str] = mapped_column(String(600), nullable=False)
    provenance_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    finding_count: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model_duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turnaround_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    estimated_cost_microusd: Mapped[int | None] = mapped_column(BigInteger)
    ballots: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    metrics: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    assessment_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    captured_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
