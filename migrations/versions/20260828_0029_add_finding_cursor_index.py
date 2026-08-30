"""增加 Finding 稳定游标分页索引。

Revision ID: 20260828_0029
Revises: 20260828_0028
Create Date: 2026-08-28
"""

from collections.abc import Sequence
from contextlib import nullcontext

from alembic import op


revision: str = "20260828_0029"
down_revision: str | None = "20260828_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为按运行、创建时间和 ID 的升序分页建立复合 B-tree 索引。"""

    postgresql = op.get_bind().dialect.name == "postgresql"
    options = {"postgresql_concurrently": True} if postgresql else {}
    context = op.get_context().autocommit_block() if postgresql else nullcontext()
    with context:
        op.create_index(
            "ix_review_findings_run_created_id",
            "review_findings",
            ["review_run_id", "created_at", "id"],
            if_not_exists=postgresql,
            **options,
        )


def downgrade() -> None:
    """移除可重建的 Finding 游标分页索引。"""

    postgresql = op.get_bind().dialect.name == "postgresql"
    options = {"postgresql_concurrently": True} if postgresql else {}
    context = op.get_context().autocommit_block() if postgresql else nullcontext()
    with context:
        op.drop_index(
            "ix_review_findings_run_created_id",
            table_name="review_findings",
            if_exists=postgresql,
            **options,
        )
