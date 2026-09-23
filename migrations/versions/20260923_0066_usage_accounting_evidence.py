"""保存请求计价依据与费用缺失原因，历史账本保持原样。"""

import sqlalchemy as sa
from alembic import op

revision = "20260923_0066"
down_revision = "20260921_0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_usage_requests", sa.Column("cost_reason", sa.String(32), nullable=True))
    op.add_column("model_usage_requests", sa.Column("pricing_snapshot", sa.JSON(), nullable=True))
    op.add_column("model_usage_requests", sa.Column("usage_details", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_usage_requests", "usage_details")
    op.drop_column("model_usage_requests", "pricing_snapshot")
    op.drop_column("model_usage_requests", "cost_reason")
