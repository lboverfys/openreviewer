"""增加每个审查计划的模型硬预算与 HTTP 调用账本。

Revision ID: 20260828_0021
Revises: 20260828_0020
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0021"
down_revision: str | None = "20260828_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """固化预算配置，并记录每一次真实模型 HTTP 请求。"""

    with op.batch_alter_table("ai_settings") as batch:
        batch.add_column(
            sa.Column(
                "max_model_http_calls",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("64"),
            )
        )
        batch.add_column(
            sa.Column(
                "max_model_input_tokens",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("2000000"),
            )
        )
        batch.add_column(
            sa.Column(
                "max_model_output_tokens",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("250000"),
            )
        )
        batch.add_column(sa.Column("max_model_cost_microusd", sa.BigInteger()))
        batch.add_column(
            sa.Column(
                "max_model_duration_seconds",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("900"),
            )
        )
        batch.create_check_constraint(
            "max_model_http_calls_range",
            "max_model_http_calls BETWEEN 1 AND 10000",
        )
        batch.create_check_constraint(
            "max_model_input_tokens_range",
            "max_model_input_tokens BETWEEN 1000 AND 1000000000",
        )
        batch.create_check_constraint(
            "max_model_output_tokens_range",
            "max_model_output_tokens BETWEEN 256 AND 100000000",
        )
        batch.create_check_constraint(
            "max_model_cost_microusd_range",
            "max_model_cost_microusd IS NULL OR "
            "max_model_cost_microusd BETWEEN 1 AND 1000000000000",
        )
        batch.create_check_constraint(
            "max_model_duration_seconds_range",
            "max_model_duration_seconds BETWEEN 30 AND 86400",
        )

    with op.batch_alter_table("review_plans") as batch:
        batch.add_column(
            sa.Column(
                "max_model_http_calls",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("64"),
            )
        )
        batch.add_column(
            sa.Column(
                "max_model_input_tokens",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("2000000"),
            )
        )
        batch.add_column(
            sa.Column(
                "max_model_output_tokens",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("250000"),
            )
        )
        batch.add_column(sa.Column("max_model_cost_microusd", sa.BigInteger()))
        batch.add_column(
            sa.Column(
                "max_model_duration_seconds",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("900"),
            )
        )
        batch.add_column(
            sa.Column(
                "model_http_calls",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column(
                "model_input_tokens",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column(
                "model_output_tokens",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column(
                "model_estimated_cost_microusd",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column(
                "model_budget_resume_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(sa.Column("model_budget_started_at", sa.DateTime(timezone=True)))
        batch.add_column(
            sa.Column("model_budget_exhausted_at", sa.DateTime(timezone=True))
        )
        batch.add_column(
            sa.Column("model_budget_exhausted_reason", sa.String(length=50))
        )
        batch.create_check_constraint(
            "model_http_calls_nonnegative",
            "model_http_calls >= 0",
        )
        batch.create_check_constraint(
            "model_input_tokens_nonnegative",
            "model_input_tokens >= 0",
        )
        batch.create_check_constraint(
            "model_output_tokens_nonnegative",
            "model_output_tokens >= 0",
        )
        batch.create_check_constraint(
            "model_estimated_cost_microusd_nonnegative",
            "model_estimated_cost_microusd >= 0",
        )
        batch.create_check_constraint(
            "model_budget_resume_count_nonnegative",
            "model_budget_resume_count >= 0",
        )

    op.create_table(
        "model_http_calls",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_plan_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("api_protocol", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("request_bytes", sa.Integer(), nullable=False),
        sa.Column("reserved_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reserved_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reserved_cost_microusd", sa.BigInteger(), nullable=False),
        sa.Column("actual_input_tokens", sa.BigInteger()),
        sa.Column("actual_output_tokens", sa.BigInteger()),
        sa.Column("actual_cost_microusd", sa.BigInteger()),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("response_status", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("sequence > 0", name="sequence_positive"),
        sa.CheckConstraint("request_bytes >= 0", name="request_bytes_nonnegative"),
        sa.CheckConstraint(
            "reserved_input_tokens >= 0",
            name="reserved_input_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "reserved_output_tokens >= 0",
            name="reserved_output_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "reserved_cost_microusd >= 0",
            name="reserved_cost_microusd_nonnegative",
        ),
        sa.CheckConstraint(
            "actual_input_tokens IS NULL OR actual_input_tokens >= 0",
            name="actual_input_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "actual_output_tokens IS NULL OR actual_output_tokens >= 0",
            name="actual_output_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "actual_cost_microusd IS NULL OR actual_cost_microusd >= 0",
            name="actual_cost_microusd_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'settled', 'uncertain')",
            name="status_value",
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
        sa.ForeignKeyConstraint(
            ["review_plan_id"],
            ["review_plans.id"],
            name="fk_model_http_calls_review_plan",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_http_calls"),
        sa.UniqueConstraint(
            "review_plan_id",
            "sequence",
            name="uq_model_http_calls_plan_sequence",
        ),
    )
    op.create_index(
        "ix_model_http_calls_plan_started",
        "model_http_calls",
        ["review_plan_id", "started_at"],
    )


def downgrade() -> None:
    """移除模型硬预算和调用账本。"""

    op.drop_index("ix_model_http_calls_plan_started", table_name="model_http_calls")
    op.drop_table("model_http_calls")

    with op.batch_alter_table("review_plans") as batch:
        batch.drop_constraint("model_budget_resume_count_nonnegative", type_="check")
        batch.drop_constraint(
            "model_estimated_cost_microusd_nonnegative",
            type_="check",
        )
        batch.drop_constraint("model_output_tokens_nonnegative", type_="check")
        batch.drop_constraint("model_input_tokens_nonnegative", type_="check")
        batch.drop_constraint("model_http_calls_nonnegative", type_="check")
        for name in (
            "model_budget_exhausted_reason",
            "model_budget_exhausted_at",
            "model_budget_started_at",
            "model_budget_resume_count",
            "model_estimated_cost_microusd",
            "model_output_tokens",
            "model_input_tokens",
            "model_http_calls",
            "max_model_duration_seconds",
            "max_model_cost_microusd",
            "max_model_output_tokens",
            "max_model_input_tokens",
            "max_model_http_calls",
        ):
            batch.drop_column(name)

    with op.batch_alter_table("ai_settings") as batch:
        batch.drop_constraint("max_model_duration_seconds_range", type_="check")
        batch.drop_constraint("max_model_cost_microusd_range", type_="check")
        batch.drop_constraint("max_model_output_tokens_range", type_="check")
        batch.drop_constraint("max_model_input_tokens_range", type_="check")
        batch.drop_constraint("max_model_http_calls_range", type_="check")
        for name in (
            "max_model_duration_seconds",
            "max_model_cost_microusd",
            "max_model_output_tokens",
            "max_model_input_tokens",
            "max_model_http_calls",
        ):
            batch.drop_column(name)
