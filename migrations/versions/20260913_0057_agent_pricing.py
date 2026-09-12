"""为各 Agent 保存独立模型价目，已有配置保持未知价格。"""

import sqlalchemy as sa
from alembic import op

revision = "20260913_0057"
down_revision = "20260913_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_agent_configs", sa.Column("input_usd_per_million", sa.Numeric(18, 6))
    )
    op.add_column(
        "ai_agent_configs", sa.Column("output_usd_per_million", sa.Numeric(18, 6))
    )
    op.add_column(
        "ai_agent_configs", sa.Column("cache_read_usd_per_million", sa.Numeric(18, 6))
    )
    op.add_column(
        "ai_agent_configs", sa.Column("cache_write_usd_per_million", sa.Numeric(18, 6))
    )


def downgrade() -> None:
    op.drop_column("ai_agent_configs", "cache_write_usd_per_million")
    op.drop_column("ai_agent_configs", "cache_read_usd_per_million")
    op.drop_column("ai_agent_configs", "output_usd_per_million")
    op.drop_column("ai_agent_configs", "input_usd_per_million")
