"""仓库的 GitHub 访问确认与平台许可共用一条配置记录。"""
import sqlalchemy as sa
from alembic import op

revision = "20260914_0063"
down_revision = "20260914_0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("repository_policies", sa.Column("connection_installation_id", sa.BigInteger()))
    op.add_column("repository_policies", sa.Column("connection_repository_id", sa.BigInteger()))
    op.add_column("repository_policies", sa.Column("connected_at", sa.DateTime(timezone=True)))
    op.add_column("repository_policies", sa.Column("connection_error", sa.String(500)))
    op.create_index("ix_repository_policies_connection_installation_id", "repository_policies", ["connection_installation_id"])


def downgrade() -> None:
    op.drop_index("ix_repository_policies_connection_installation_id", "repository_policies")
    for column in ("connection_error", "connected_at", "connection_repository_id", "connection_installation_id"):
        op.drop_column("repository_policies", column)
