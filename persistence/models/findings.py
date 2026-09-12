"""按职责集中维护的 findings 数据记录。"""

from datetime import datetime

import sqlalchemy as sa
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from domain.enums import (
    EvidenceVerificationStatus,
    FindingAdjudicationStatus,
    FindingCategory,
    FindingEvaluationVerdict,
    FindingLifecycleState,
    FindingOccurrenceStatus,
    LocationSide,
    ModelBatchStatus,
    ModelCallStatus,
    ModelProvider,
    Severity,
    VerificationStatus,
)
from persistence.models.base import Base, enum_values, utc_now


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
        Index(
            "ix_model_calls_provider_model_created", "provider", "model", "created_at"
        ),
        Index("ix_model_calls_created", "created_at"),
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
    estimated_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
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
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
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

    context_references: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=sa.text("'[]'")
    )
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
            f"location_side IS NULL OR location_side IN ({enum_values(LocationSide)})",
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
    location_in_diff: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
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
