"""增加任务事件查询索引和 Finding 人工裁决信息。

Revision ID: 20260826_0010
Revises: 20260826_0009
Create Date: 2026-08-26 18:10:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260826_0010"
down_revision: str | None = "20260826_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为任务详情日志和人工 Finding 裁决补齐持久化字段。"""

    op.create_index(
        "ix_outbox_events_aggregate_occurred",
        "outbox_events",
        ["aggregate_type", "aggregate_id", "occurred_at"],
    )
    with op.batch_alter_table("review_findings") as batch_op:
        batch_op.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("reviewed_by", sa.String(length=100)))


def downgrade() -> None:
    """移除本次新增的裁决字段和事件查询索引。"""

    with op.batch_alter_table("review_findings") as batch_op:
        batch_op.drop_column("reviewed_by")
        batch_op.drop_column("reviewed_at")
    op.drop_index(
        "ix_outbox_events_aggregate_occurred",
        table_name="outbox_events",
    )
