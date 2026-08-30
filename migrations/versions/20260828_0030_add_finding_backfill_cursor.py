"""增加 Finding 生命周期逐条回填游标。

Revision ID: 20260828_0030
Revises: 20260828_0029
Create Date: 2026-08-28
"""

from collections.abc import Sequence
from contextlib import nullcontext

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0030"
down_revision: str | None = "20260828_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """记录每条 Finding 的回填进度，并加速固定大小批次扫描。"""

    with op.batch_alter_table("review_findings") as batch:
        batch.add_column(
            sa.Column("lifecycle_backfilled_at", sa.DateTime(timezone=True))
        )

    postgresql = op.get_bind().dialect.name == "postgresql"
    options = {"postgresql_concurrently": True} if postgresql else {}
    context = op.get_context().autocommit_block() if postgresql else nullcontext()
    with context:
        op.create_index(
            "ix_review_findings_lifecycle_backfill",
            "review_findings",
            ["lifecycle_backfilled_at", "created_at", "id"],
            if_not_exists=postgresql,
            **options,
        )


def downgrade() -> None:
    """移除可重建的逐条回填游标。"""

    postgresql = op.get_bind().dialect.name == "postgresql"
    options = {"postgresql_concurrently": True} if postgresql else {}
    context = op.get_context().autocommit_block() if postgresql else nullcontext()
    with context:
        op.drop_index(
            "ix_review_findings_lifecycle_backfill",
            table_name="review_findings",
            if_exists=postgresql,
            **options,
        )
    with op.batch_alter_table("review_findings") as batch:
        batch.drop_column("lifecycle_backfilled_at")
