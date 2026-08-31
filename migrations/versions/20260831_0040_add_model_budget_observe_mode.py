"""为模型预算计划增加可观测模式，兼容已有硬预算计划。

Revision ID: 20260831_0040
Revises: 20260830_0039
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_0040"
down_revision: str | None = "20260830_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新计划保存预算处理模式，并让历史计划进入观测模式。"""

    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("review_plans", recreate="always") as batch:
            batch.add_column(sa.Column("model_budget_mode", sa.String(length=16)))
            batch.create_check_constraint(
                "model_budget_mode_value",
                "model_budget_mode IS NULL OR model_budget_mode IN ('observe', 'enforce')",
            )
    else:
        with op.batch_alter_table("review_plans") as batch:
            batch.add_column(sa.Column("model_budget_mode", sa.String(length=16)))
            batch.create_check_constraint(
                "model_budget_mode_value",
                "model_budget_mode IS NULL OR model_budget_mode IN ('observe', 'enforce')",
            )

    # 预算阈值现在只用于观测，不能因为升级前创建的计划没有模式字段而
    # 继续触发旧版硬阻断。保留 NULL 兼容读取兜底，但正常升级后所有历史
    # 计划都明确标记为 observe，便于审计和管理界面正确展示。
    op.execute(
        sa.text(
            "UPDATE review_plans "
            "SET model_budget_mode = 'observe' "
            "WHERE model_budget_mode IS NULL"
        )
    )


def downgrade() -> None:
    """移除预算处理模式字段。"""

    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("review_plans", recreate="always") as batch:
            batch.drop_constraint("model_budget_mode_value", type_="check")
            batch.drop_column("model_budget_mode")
    else:
        with op.batch_alter_table("review_plans") as batch:
            batch.drop_constraint("model_budget_mode_value", type_="check")
            batch.drop_column("model_budget_mode")
