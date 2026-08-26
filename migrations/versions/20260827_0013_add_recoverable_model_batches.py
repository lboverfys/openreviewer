"""增加可恢复的模型批次状态。

Revision ID: 20260827_0013
Revises: 20260826_0012
Create Date: 2026-08-27 10:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0013"
down_revision: str | None = "20260826_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存每个 Agent 批次的状态、租约和严格结构化结果。"""

    op.create_table(
        "model_review_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_plan_id", sa.String(length=36), nullable=False),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("batch_number", sa.Integer(), nullable=False),
        sa.Column("batch_count", sa.Integer(), nullable=False),
        sa.Column("unit_keys", sa.JSON(), nullable=False),
        sa.Column(
            "estimated_input_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=200)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("request_fingerprint", sa.String(length=64)),
        sa.Column("provider_request_id", sa.String(length=200)),
        sa.Column("response_status", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("result", sa.JSON()),
        sa.Column("error_code", sa.String(length=64)),
        sa.Column("error_message", sa.String(length=1000)),
        sa.Column("error_details", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("batch_number > 0", name="batch_number_positive"),
        sa.CheckConstraint("batch_count > 0", name="batch_count_positive"),
        sa.CheckConstraint(
            "batch_number <= batch_count",
            name="batch_number_within_count",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        sa.CheckConstraint(
            "estimated_input_tokens >= 0",
            name="estimated_input_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "response_status IS NULL OR "
            "(response_status >= 100 AND response_status <= 599)",
            name="response_status_range",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="duration_ms_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="status_value",
        ),
        sa.ForeignKeyConstraint(
            ["review_plan_id"],
            ["review_plans.id"],
            name="fk_model_review_batches_plan",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_review_batches"),
        sa.UniqueConstraint(
            "review_plan_id",
            "agent",
            "batch_number",
            name="uq_model_review_batches_plan_agent_number",
        ),
    )
    op.create_index(
        "ix_model_review_batches_claimable",
        "model_review_batches",
        ["status", "available_at", "lease_expires_at"],
    )
    op.create_index(
        "ix_model_review_batches_plan_agent",
        "model_review_batches",
        ["review_plan_id", "agent", "batch_number"],
    )


def downgrade() -> None:
    """移除批次恢复表。"""

    op.drop_index(
        "ix_model_review_batches_plan_agent",
        table_name="model_review_batches",
    )
    op.drop_index(
        "ix_model_review_batches_claimable",
        table_name="model_review_batches",
    )
    op.drop_table("model_review_batches")
