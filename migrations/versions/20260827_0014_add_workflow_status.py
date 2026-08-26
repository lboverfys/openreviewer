"""增加固定多 Agent DAG 的独立工作流状态。

Revision ID: 20260827_0014
Revises: 20260827_0013
Create Date: 2026-08-27 11:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0014"
down_revision: str | None = "20260827_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORKFLOW_STATUSES = (
    "queued",
    "ci",
    "planning",
    "agent_batches",
    "aggregating",
    "awaiting_approval",
    "approved",
    "rejected",
    "awaiting_publish",
    "publishing",
    "completed",
    "failed",
    "paused",
    "waiting_for_ci",
    "running",
    "ready_for_review",
    "timed_out",
    "cancelled",
    "superseded",
)

LEGACY_EXECUTION_STATUSES = (
    "queued",
    "waiting_for_ci",
    "running",
    "ready_for_review",
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "superseded",
)


def quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _replace_check(table: str, name: str, expression: str) -> None:
    connection = op.get_bind()
    constraint_name = op.f(name)
    if connection.dialect.name == "postgresql":
        op.drop_constraint(constraint_name, table, type_="check")
        op.create_check_constraint(constraint_name, table, expression)
        return
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(table, recreate="always") as batch_op:
            batch_op.drop_constraint(constraint_name, type_="check")
            batch_op.create_check_constraint(constraint_name, expression)
        return
    raise RuntimeError("workflow status migration supports PostgreSQL and SQLite only")


def upgrade() -> None:
    """为运行和任务保存可暂停、批准、发布的 DAG 节点。"""

    for table in ("review_runs", "review_tasks"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "workflow_status",
                    sa.String(length=32),
                    server_default="queued",
                    nullable=False,
                )
            )
            batch_op.create_check_constraint(
                op.f(f"ck_{table}_workflow_status_value"),
                f"workflow_status IN ({quoted(WORKFLOW_STATUSES)})",
            )

    op.execute(
        sa.text(
            "UPDATE review_runs SET workflow_status = CASE "
            "WHEN execution_status = 'waiting_for_ci' THEN 'ci' "
            "WHEN execution_status = 'ready_for_review' THEN 'planning' "
            "WHEN execution_status = 'running' AND EXISTS ("
            "SELECT 1 FROM review_plans WHERE review_plans.review_run_id = review_runs.id"
            ") THEN 'agent_batches' "
            "WHEN execution_status = 'completed' AND EXISTS ("
            "SELECT 1 FROM review_plans WHERE review_plans.review_run_id = review_runs.id "
            "AND review_plans.model_review_completed_at IS NOT NULL"
            ") THEN 'awaiting_approval' "
            "ELSE execution_status END"
        )
    )
    op.execute(
        sa.text(
            "UPDATE review_tasks SET workflow_status = ("
            "SELECT workflow_status FROM review_runs "
            "WHERE review_runs.id = review_tasks.review_run_id"
            ")"
        )
    )
    for table in ("review_runs", "review_tasks"):
        _replace_check(
            table,
            f"ck_{table}_execution_status_value",
            f"execution_status IN ({quoted(WORKFLOW_STATUSES)})",
        )
        _replace_check(
            table,
            f"ck_{table}_workflow_status_value",
            f"workflow_status IN ({quoted(WORKFLOW_STATUSES)})",
        )
    with op.batch_alter_table("review_runs") as batch_op:
        batch_op.alter_column("workflow_status", server_default=None)
    with op.batch_alter_table("review_tasks") as batch_op:
        batch_op.alter_column("workflow_status", server_default=None)


def downgrade() -> None:
    """移除独立工作流状态并恢复旧执行状态约束。"""

    # 正常应用只把新节点写入 workflow_status；这里仍防御性归一化可能由
    # 手工 SQL 写入 execution_status 的新值，避免收紧 CHECK 时迁移失败。
    for table in ("review_runs", "review_tasks"):
        op.execute(
            sa.text(
                f"UPDATE {table} SET execution_status = CASE "
                "WHEN execution_status = 'ci' THEN 'waiting_for_ci' "
                "WHEN execution_status IN ('planning', 'agent_batches', "
                "'aggregating', 'paused') THEN 'ready_for_review' "
                "WHEN execution_status IN ('awaiting_approval', 'approved', "
                "'rejected', 'awaiting_publish', 'publishing') THEN 'completed' "
                "ELSE execution_status END"
            )
        )

    for table in ("review_runs", "review_tasks"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(
                op.f(f"ck_{table}_workflow_status_value"),
                type_="check",
            )
            batch_op.drop_column("workflow_status")
        _replace_check(
            table,
            f"ck_{table}_execution_status_value",
            f"execution_status IN ({quoted(LEGACY_EXECUTION_STATUSES)})",
        )
