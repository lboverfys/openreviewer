"""增加 Worker 心跳表。

Revision ID: 20260818_0002
Revises: 20260818_0001
Create Date: 2026-08-18 20:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260818_0002"
down_revision: str | None = "20260818_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


WORKER_STATUSES = ("starting", "idle", "busy", "stopping")


def quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_task_id", sa.String(length=36), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"status IN ({quoted(WORKER_STATUSES)})",
            name="ck_worker_heartbeats_status_value",
        ),
        sa.ForeignKeyConstraint(
            ["current_task_id"],
            ["review_tasks.id"],
            name="fk_worker_heartbeats_current_task_id_review_tasks",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("worker_id", name="pk_worker_heartbeats"),
    )
    op.create_index(
        "ix_worker_heartbeats_last_seen",
        "worker_heartbeats",
        ["last_seen_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_worker_heartbeats_last_seen",
        table_name="worker_heartbeats",
    )
    op.drop_table("worker_heartbeats")
