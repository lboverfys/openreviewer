"""将模型任务运行保护默认值从 15 分钟提高到 1 小时。

Revision ID: 20260831_0042
Revises: 20260831_0041
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_0042"
down_revision: str | None = "20260831_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """提高新旧默认运行保护，保留管理员设置的其他时限。"""

    with op.batch_alter_table("ai_settings") as batch:
        batch.alter_column(
            "max_model_duration_seconds",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=sa.text("3600"),
        )
    with op.batch_alter_table("review_plans") as batch:
        batch.alter_column(
            "max_model_duration_seconds",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=sa.text("3600"),
        )

    # 900 秒是 0021 引入的历史默认。全局设置是单例，若不回填，部署后
    # 应用仍会继续给新任务固化 15 分钟；历史 observe 计划若不回填，人工
    # 重试也仍会沿用 15 分钟。只提升恰好使用旧默认的记录，其他管理员
    # 自定义时限保持不变。
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE ai_settings "
            "SET max_model_duration_seconds = 3600 "
            "WHERE max_model_duration_seconds = 900"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE review_plans "
            "SET max_model_duration_seconds = 3600 "
            "WHERE max_model_duration_seconds = 900 "
            "AND model_budget_mode = 'observe'"
        )
    )


def downgrade() -> None:
    """恢复新记录的 900 秒默认值，不覆盖升级后的运行数据。"""

    with op.batch_alter_table("ai_settings") as batch:
        batch.alter_column(
            "max_model_duration_seconds",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=sa.text("900"),
        )
    with op.batch_alter_table("review_plans") as batch:
        batch.alter_column(
            "max_model_duration_seconds",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=sa.text("900"),
        )

    # 数据回填不可可靠逆转：升级后管理员可能主动保存 3600 秒。降级只恢复
    # 数据库默认值，保留现有记录，避免把真实配置误改回 900 秒。
