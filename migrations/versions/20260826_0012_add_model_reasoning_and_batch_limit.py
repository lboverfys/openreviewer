"""增加模型推理强度与单批输入上限配置。

Revision ID: 20260826_0012
Revises: 20260826_0011
Create Date: 2026-08-26 22:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260826_0012"
down_revision: str | None = "20260826_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为已有供应商采用兼容性优先的默认推理和分批策略。"""

    with op.batch_alter_table("ai_provider_configs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "reasoning_effort",
                sa.String(length=16),
                server_default="none",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "max_batch_input_tokens",
                sa.Integer(),
                server_default=sa.text("64000"),
                nullable=False,
            )
        )
    with op.batch_alter_table("ai_provider_configs") as batch_op:
        batch_op.alter_column("reasoning_effort", server_default=None)
        batch_op.alter_column("max_batch_input_tokens", server_default=None)
        batch_op.create_check_constraint(
            "reasoning_effort_value",
            "reasoning_effort IN ('none', 'low', 'medium', 'high', 'max')",
        )
        batch_op.create_check_constraint(
            "max_batch_input_tokens_range",
            "max_batch_input_tokens BETWEEN 4096 AND 4000000",
        )


def downgrade() -> None:
    """移除推理强度与单批输入上限字段。"""

    with op.batch_alter_table("ai_provider_configs") as batch_op:
        batch_op.drop_constraint("max_batch_input_tokens_range", type_="check")
        batch_op.drop_constraint("reasoning_effort_value", type_="check")
        batch_op.drop_column("max_batch_input_tokens")
        batch_op.drop_column("reasoning_effort")
