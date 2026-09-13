"""为节点历史提供稳定游标索引，不修改已有心跳与保留期。"""

from alembic import op

revision = "20260913_0059"
down_revision = "20260913_0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_worker_heartbeats_started_page", "worker_heartbeats", ["started_at", "worker_id"])


def downgrade() -> None:
    op.drop_index("ix_worker_heartbeats_started_page", table_name="worker_heartbeats")
