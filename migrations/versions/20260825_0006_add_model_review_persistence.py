"""增加模型调用审计、模型阶段标记和未复核 Finding。

Revision ID: 20260825_0006
Revises: 20260825_0005
Create Date: 2026-08-25 16:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260825_0006"
down_revision: str | None = "20260825_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MODEL_PROVIDERS = ("openai", "anthropic")
MODEL_CALL_STATUSES = ("succeeded", "skipped")
SEVERITIES = ("critical", "high", "medium", "low")
FINDING_CATEGORIES = (
    "architecture",
    "authorization",
    "security",
    "database",
    "business_contract",
    "test_gap",
    "reliability",
)
LOCATION_SIDES = ("left", "right")
VERIFICATION_STATUSES = ("unverified", "verified", "rejected")


def quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    """创建模型阶段持久化结构，并为模型阶段提供独立尝试计数。"""

    with op.batch_alter_table("review_tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "model_attempt_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_review_tasks_model_attempt_count_nonnegative"),
            "model_attempt_count >= 0",
        )

    with op.batch_alter_table("review_plans") as batch_op:
        batch_op.add_column(
            sa.Column(
                "model_review_completed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )

    op.create_table(
        "model_calls",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_plan_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("prompt_version", sa.String(length=50), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("provider_response_id", sa.String(length=200)),
        sa.Column("provider_request_id", sa.String(length=200)),
        sa.Column("response_status", sa.Integer()),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_input_tokens", sa.Integer(), nullable=False),
        sa.Column("reasoning_output_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_microusd", sa.BigInteger()),
        sa.Column("finding_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"provider IN ({quoted(MODEL_PROVIDERS)})",
            name="provider_value",
        ),
        sa.CheckConstraint(
            f"status IN ({quoted(MODEL_CALL_STATUSES)})",
            name="status_value",
        ),
        sa.CheckConstraint("duration_ms >= 0", name="duration_ms_nonnegative"),
        sa.CheckConstraint("input_tokens >= 0", name="input_tokens_nonnegative"),
        sa.CheckConstraint("output_tokens >= 0", name="output_tokens_nonnegative"),
        sa.CheckConstraint(
            "cache_read_input_tokens >= 0",
            name="cache_read_input_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "cache_write_input_tokens >= 0",
            name="cache_write_input_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "reasoning_output_tokens >= 0",
            name="reasoning_output_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "estimated_cost_microusd IS NULL OR estimated_cost_microusd >= 0",
            name="estimated_cost_microusd_nonnegative",
        ),
        sa.CheckConstraint("finding_count >= 0", name="finding_count_nonnegative"),
        sa.CheckConstraint(
            "response_status IS NULL OR "
            "(response_status >= 100 AND response_status <= 599)",
            name="response_status_range",
        ),
        sa.ForeignKeyConstraint(
            ["review_plan_id"],
            ["review_plans.id"],
            name="fk_model_calls_review_plan",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_calls"),
        sa.UniqueConstraint(
            "review_plan_id",
            name="uq_model_calls_review_plan_id",
        ),
    )
    op.create_index(
        "ix_model_calls_provider_model_created",
        "model_calls",
        ["provider", "model", "created_at"],
        unique=False,
    )

    op.create_table(
        "review_findings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_run_id", sa.String(length=36), nullable=False),
        sa.Column("review_plan_id", sa.String(length=36), nullable=False),
        sa.Column("model_call_id", sa.String(length=36), nullable=False),
        sa.Column("source_unit_key", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("location_file", sa.String(length=1024)),
        sa.Column("location_blob_sha", sa.String(length=64)),
        sa.Column("location_start_line", sa.Integer()),
        sa.Column("location_end_line", sa.Integer()),
        sa.Column("location_side", sa.String(length=10)),
        sa.Column(
            "location_in_diff",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("location_symbol", sa.String(length=512)),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("impact", sa.Text(), nullable=False),
        sa.Column("suggestion", sa.Text(), nullable=False),
        sa.Column("required_test", sa.Text()),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("verification_status", sa.String(length=20), nullable=False),
        sa.Column("rule_reference", sa.String(length=1024)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"severity IN ({quoted(SEVERITIES)})",
            name="severity_value",
        ),
        sa.CheckConstraint(
            f"category IN ({quoted(FINDING_CATEGORIES)})",
            name="category_value",
        ),
        sa.CheckConstraint(
            "location_side IS NULL OR "
            f"location_side IN ({quoted(LOCATION_SIDES)})",
            name="location_side_value",
        ),
        sa.CheckConstraint(
            f"verification_status IN ({quoted(VERIFICATION_STATUSES)})",
            name="verification_status_value",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence_range",
        ),
        sa.CheckConstraint(
            "(location_file IS NULL AND location_blob_sha IS NULL AND "
            "location_start_line IS NULL AND location_end_line IS NULL AND "
            "location_side IS NULL AND location_symbol IS NULL) OR "
            "(location_file IS NOT NULL AND location_blob_sha IS NOT NULL AND "
            "location_start_line > 0 AND location_end_line >= location_start_line "
            "AND location_side IS NOT NULL)",
            name="location_shape",
        ),
        sa.ForeignKeyConstraint(
            ["review_run_id"],
            ["review_runs.id"],
            name="fk_review_findings_review_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["review_plan_id"],
            ["review_plans.id"],
            name="fk_review_findings_review_plan",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_call_id"],
            ["model_calls.id"],
            name="fk_review_findings_model_call",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_findings"),
        sa.UniqueConstraint(
            "review_run_id",
            "fingerprint",
            name="uq_review_findings_run_fingerprint",
        ),
    )
    op.create_index(
        "ix_review_findings_run_verification",
        "review_findings",
        ["review_run_id", "verification_status"],
        unique=False,
    )
    op.create_index(
        "ix_review_findings_head_fingerprint",
        "review_findings",
        ["head_sha", "fingerprint"],
        unique=False,
    )


def downgrade() -> None:
    """移除模型阶段及其结果，但保留原有 Review Plan。"""

    op.drop_index(
        "ix_review_findings_head_fingerprint",
        table_name="review_findings",
    )
    op.drop_index(
        "ix_review_findings_run_verification",
        table_name="review_findings",
    )
    op.drop_table("review_findings")
    op.drop_index(
        "ix_model_calls_provider_model_created",
        table_name="model_calls",
    )
    op.drop_table("model_calls")

    with op.batch_alter_table("review_plans") as batch_op:
        batch_op.drop_column("model_review_completed_at")

    with op.batch_alter_table("review_tasks") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_review_tasks_model_attempt_count_nonnegative"),
            type_="check",
        )
        batch_op.drop_column("model_attempt_count")
