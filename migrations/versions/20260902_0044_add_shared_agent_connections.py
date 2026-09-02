"""让固定 Agent 可复用当前激活的公共模型连接配置。

Revision ID: 20260902_0044
Revises: 20260902_0043
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0044"
down_revision: str | None = "20260902_0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增共享连接开关和可选模型覆盖，旧 Agent 保持独立模式。"""

    connection = op.get_bind()
    recreate = "always" if connection.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table("ai_agent_configs", recreate=recreate) as batch:
        batch.add_column(
            sa.Column(
                "use_shared_connection",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("model_override", sa.String(length=200)))
def downgrade() -> None:
    """移除共享连接字段；旧的独立连接字段不受影响。"""

    connection = op.get_bind()
    recreate = "always" if connection.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table("ai_agent_configs", recreate=recreate) as batch:
        batch.drop_column("model_override")
        batch.drop_column("use_shared_connection")
