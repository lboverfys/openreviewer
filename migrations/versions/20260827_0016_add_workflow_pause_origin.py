"""保存固定 DAG 的暂停来源节点。

Revision ID: 20260827_0016
Revises: 20260827_0015
Create Date: 2026-08-27 13:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0016"
down_revision: str | None = "20260827_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RESUMABLE_STATUSES = (
    "queued",
    "ci",
    "planning",
    "agent_batches",
    "aggregating",
    "awaiting_approval",
    "awaiting_publish",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    """让暂停任务携带可验证的恢复目标，避免继续时回到错误阶段。"""

    expression = (
        "workflow_paused_from IS NULL OR workflow_paused_from IN "
        f"({_quoted(RESUMABLE_STATUSES)})"
    )
    for table in ("review_runs", "review_tasks"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column("workflow_paused_from", sa.String(32)))
            batch_op.create_check_constraint(
                op.f(f"ck_{table}_workflow_paused_from_value"),
                expression,
            )


def downgrade() -> None:
    """移除暂停来源字段。"""

    for table in ("review_runs", "review_tasks"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(
                op.f(f"ck_{table}_workflow_paused_from_value"),
                type_="check",
            )
            batch_op.drop_column("workflow_paused_from")
