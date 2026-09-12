"""给人工经验知识增加仓库范围，既有通用知识保持原语义。"""

import sqlalchemy as sa
from alembic import op

revision = "20260913_0056"
down_revision = "20260913_0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_documents", sa.Column("repository_scope", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_documents", "repository_scope")
