"""增加 Dashboard 游标和 SSE 变化令牌索引。

Revision ID: 20260828_0027
Revises: 20260828_0026
Create Date: 2026-08-28
"""

from collections.abc import Sequence
from contextlib import nullcontext

from alembic import op


revision: str = "20260828_0027"
down_revision: str | None = "20260828_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """让稳定翻页和最新事件探测都使用复合 B-tree 索引。"""

    postgresql = op.get_bind().dialect.name == "postgresql"
    options = {"postgresql_concurrently": True} if postgresql else {}
    context = op.get_context().autocommit_block() if postgresql else nullcontext()
    with context:
        op.create_index(
            "ix_review_runs_created_id",
            "review_runs",
            ["created_at", "id"],
            if_not_exists=postgresql,
            **options,
        )
        op.create_index(
            "ix_outbox_events_occurred_id",
            "outbox_events",
            ["occurred_at", "id"],
            if_not_exists=postgresql,
            **options,
        )


def downgrade() -> None:
    """移除可重建的 Dashboard 查询索引。"""

    postgresql = op.get_bind().dialect.name == "postgresql"
    options = {"postgresql_concurrently": True} if postgresql else {}
    context = op.get_context().autocommit_block() if postgresql else nullcontext()
    with context:
        op.drop_index(
            "ix_outbox_events_occurred_id",
            table_name="outbox_events",
            if_exists=postgresql,
            **options,
        )
        op.drop_index(
            "ix_review_runs_created_id",
            table_name="review_runs",
            if_exists=postgresql,
            **options,
        )
