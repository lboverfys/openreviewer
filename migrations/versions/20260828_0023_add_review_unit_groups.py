"""为 Review Unit 增加确定性的关联文件组。

Revision ID: 20260828_0023
Revises: 20260828_0022
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0023"
down_revision: str | None = "20260828_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """先用旧 unit_key 回填历史行，再收紧为非空字段。"""

    with op.batch_alter_table("review_units") as batch:
        batch.add_column(sa.Column("group_key", sa.String(length=64)))
    op.execute("UPDATE review_units SET group_key = unit_key WHERE group_key IS NULL")
    with op.batch_alter_table("review_units") as batch:
        batch.alter_column(
            "group_key",
            existing_type=sa.String(length=64),
            nullable=False,
        )


def downgrade() -> None:
    """移除关联组；单文件 Unit 数据本身保持不变。"""

    with op.batch_alter_table("review_units") as batch:
        batch.drop_column("group_key")
