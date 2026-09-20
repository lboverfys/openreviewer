"""保留历史单人核对，新增创建后固定的双人验收模式。"""

import sqlalchemy as sa
from alembic import op

revision = "20260921_0064"
down_revision = "20260914_0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("evaluation_datasets") as batch:
        batch.add_column(sa.Column("review_mode", sa.String(16), nullable=False, server_default="single"))
        batch.create_check_constraint("review_mode_value", "review_mode IN ('single','dual')")


def downgrade() -> None:
    with op.batch_alter_table("evaluation_datasets") as batch:
        batch.drop_constraint("review_mode_value", type_="check")
        batch.drop_column("review_mode")
