"""为固定 Worker ID 增加进程级心跳所有权。

Revision ID: 20260830_0038
Revises: 20260830_0037
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0038"
down_revision: str | None = "20260830_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加可空 token；现有心跳由新 Worker 启动时原子接管。"""

    op.add_column(
        "worker_heartbeats",
        sa.Column("instance_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """移除进程级心跳所有权字段。"""

    op.drop_column("worker_heartbeats", "instance_id")
