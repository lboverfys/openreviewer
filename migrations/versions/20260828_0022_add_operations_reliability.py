"""增加 Outbox 发布租约与有界运维查询索引。

Revision ID: 20260828_0022
Revises: 20260828_0021
Create Date: 2026-08-28
"""

from collections.abc import Sequence
from contextlib import nullcontext

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0022"
down_revision: str | None = "20260828_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """让 Outbox 可并发领取，并为保留期清理提供索引。"""

    postgresql = op.get_bind().dialect.name == "postgresql"
    options = {"postgresql_concurrently": True} if postgresql else {}
    context = op.get_context().autocommit_block() if postgresql else nullcontext()
    with context:
        op.drop_index(
            "ix_outbox_events_pending",
            table_name="outbox_events",
            if_exists=postgresql,
            **options,
        )
    with op.batch_alter_table("outbox_events") as batch:
        batch.add_column(sa.Column("publish_lease_owner", sa.String(length=200)))
        batch.add_column(
            sa.Column("publish_lease_expires_at", sa.DateTime(timezone=True))
        )
        batch.add_column(
            sa.Column(
                "next_publish_attempt_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        batch.add_column(sa.Column("last_publish_error", sa.Text()))
        batch.create_check_constraint(
            "publish_lease_shape",
            "(publish_lease_owner IS NULL AND publish_lease_expires_at IS NULL) "
            "OR (publish_lease_owner IS NOT NULL "
            "AND publish_lease_expires_at IS NOT NULL)",
        )
    context = op.get_context().autocommit_block() if postgresql else nullcontext()
    with context:
        op.create_index(
            "ix_outbox_events_pending",
            "outbox_events",
            [
                "published_at",
                "next_publish_attempt_at",
                "publish_lease_expires_at",
                "occurred_at",
            ],
            if_not_exists=postgresql,
            **options,
        )
        op.create_index(
            "ix_review_runs_status_created",
            "review_runs",
            ["execution_status", "created_at"],
            if_not_exists=postgresql,
            **options,
        )


def downgrade() -> None:
    """移除 Outbox 发布状态和运维清理索引。"""

    postgresql = op.get_bind().dialect.name == "postgresql"
    options = {"postgresql_concurrently": True} if postgresql else {}
    context = op.get_context().autocommit_block() if postgresql else nullcontext()
    with context:
        op.drop_index(
            "ix_review_runs_status_created",
            table_name="review_runs",
            if_exists=postgresql,
            **options,
        )
        op.drop_index(
            "ix_outbox_events_pending",
            table_name="outbox_events",
            if_exists=postgresql,
            **options,
        )
    with op.batch_alter_table("outbox_events") as batch:
        batch.drop_constraint("publish_lease_shape", type_="check")
        batch.drop_column("last_publish_error")
        batch.drop_column("next_publish_attempt_at")
        batch.drop_column("publish_lease_expires_at")
        batch.drop_column("publish_lease_owner")
    context = op.get_context().autocommit_block() if postgresql else nullcontext()
    with context:
        op.create_index(
            "ix_outbox_events_pending",
            "outbox_events",
            ["published_at", "occurred_at"],
            if_not_exists=postgresql,
            **options,
        )
