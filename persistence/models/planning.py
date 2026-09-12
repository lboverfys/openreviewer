"""按职责集中维护的 planning 数据记录。"""

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
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from domain.enums import ReviewFileDecision
from persistence.models.base import Base, enum_values, utc_now


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
    model_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    model_output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
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
