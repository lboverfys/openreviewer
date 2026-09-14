"""记录文档删除事实，阻止初始化和维护任务重新导入。"""

import sqlalchemy as sa
from alembic import op

revision = "20260914_0061"
down_revision = "20260914_0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("knowledge_deletions",
        sa.Column("source", sa.String(200), primary_key=True),
        sa.Column("deleted_by", sa.String(100), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    op.drop_table("knowledge_deletions")
