"""增加可由管理界面配置的模型 API 中转地址。

Revision ID: 20260826_0009
Revises: 20260825_0008
Create Date: 2026-08-26 14:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260826_0009"
down_revision: str | None = "20260825_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为已有供应商配置增加可空地址，空值继续使用官方 API。"""

    with op.batch_alter_table("ai_provider_configs") as batch_op:
        batch_op.add_column(sa.Column("api_base_url", sa.String(length=500)))


def downgrade() -> None:
    """移除自定义模型 API 地址。"""

    with op.batch_alter_table("ai_provider_configs") as batch_op:
        batch_op.drop_column("api_base_url")
