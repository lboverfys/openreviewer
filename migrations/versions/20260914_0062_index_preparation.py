"""记录每次准备起点，使自动等待有明确期限。"""
import sqlalchemy as sa
from alembic import op

revision = "20260914_0062"
down_revision = "20260914_0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("code_indexes", sa.Column("preparation_started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))


def downgrade() -> None:
    op.drop_column("code_indexes", "preparation_started_at")
