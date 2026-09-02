"""彻底停用模型资源预算，并恢复被旧预算暂停的任务。

Revision ID: 20260902_0045
Revises: 20260902_0044
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260902_0045"
down_revision: str | None = "20260902_0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """让旧字段仅作为兼容数据存在，任何任务都不再因其暂停。"""

    connection = op.get_bind()

    # 先恢复运行记录。使用一条 UPDATE，避免按任务逐条查询；只有明确由
    # 旧预算错误暂停的任务会被唤醒，用户主动暂停的任务保持不变。
    if connection.dialect.name == "sqlite":
        connection.execute(
            sa.text(
                "UPDATE review_runs SET "
                "workflow_status = CASE "
                "  WHEN COALESCE((SELECT t.workflow_paused_from FROM review_tasks t "
                "                 WHERE t.review_run_id = review_runs.id), "
                "                 workflow_paused_from) = 'queued' "
                "    THEN 'queued' "
                "  WHEN COALESCE((SELECT t.workflow_paused_from FROM review_tasks t "
                "                 WHERE t.review_run_id = review_runs.id), "
                "                 workflow_paused_from) = 'ci' "
                "    THEN 'ci' "
                "  WHEN COALESCE((SELECT t.workflow_paused_from FROM review_tasks t "
                "                 WHERE t.review_run_id = review_runs.id), "
                "                 workflow_paused_from) IN "
                "       ('planning', 'agent_batches', 'aggregating') "
                "    THEN COALESCE((SELECT t.workflow_paused_from FROM review_tasks t "
                "                   WHERE t.review_run_id = review_runs.id), "
                "                   workflow_paused_from) "
                "  ELSE 'agent_batches' END, "
                "execution_status = CASE "
                "  WHEN COALESCE((SELECT t.workflow_paused_from FROM review_tasks t "
                "                 WHERE t.review_run_id = review_runs.id), "
                "                 workflow_paused_from) IN ('queued', 'ci') "
                "    THEN 'queued' ELSE 'ready_for_review' END, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE id IN (SELECT review_run_id FROM review_tasks "
                "              WHERE last_error_code = 'model_budget_exceeded')"
            )
        )
    else:
        connection.execute(
            sa.text(
                "UPDATE review_runs AS r "
                "SET workflow_status = CASE "
                "  WHEN COALESCE(t.workflow_paused_from, r.workflow_paused_from) "
                "       = 'queued' THEN 'queued' "
                "  WHEN COALESCE(t.workflow_paused_from, r.workflow_paused_from) "
                "       = 'ci' THEN 'ci' "
                "  WHEN COALESCE(t.workflow_paused_from, r.workflow_paused_from) "
                "       IN ('planning', 'agent_batches', 'aggregating') "
                "    THEN COALESCE(t.workflow_paused_from, r.workflow_paused_from) "
                "  ELSE 'agent_batches' END, "
                "execution_status = CASE "
                "  WHEN COALESCE(t.workflow_paused_from, r.workflow_paused_from) "
                "       IN ('queued', 'ci') THEN 'queued' "
                "  ELSE 'ready_for_review' END, "
                "updated_at = CURRENT_TIMESTAMP "
                "FROM review_tasks AS t "
                "WHERE t.review_run_id = r.id "
                "  AND t.last_error_code = 'model_budget_exceeded'"
            )
        )
    connection.execute(
        sa.text(
            "UPDATE review_tasks "
            "SET workflow_status = CASE "
            "  WHEN workflow_paused_from = 'queued' THEN 'queued' "
            "  WHEN workflow_paused_from = 'ci' THEN 'ci' "
            "  WHEN workflow_paused_from IN "
            "       ('planning', 'agent_batches', 'aggregating') "
            "    THEN workflow_paused_from "
            "  ELSE 'agent_batches' END, "
            "execution_status = CASE "
            "  WHEN workflow_paused_from IN ('queued', 'ci') THEN 'queued' "
            "  ELSE 'ready_for_review' END, "
            "workflow_paused_from = NULL, "
            "lease_owner = NULL, "
            "lease_expires_at = NULL, "
            "claimed_from_status = NULL, "
            "available_at = CURRENT_TIMESTAMP, "
            "last_error = NULL, "
            "last_error_code = NULL, "
            "last_error_retryable = NULL, "
            "last_error_details = NULL, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE last_error_code = 'model_budget_exceeded'"
        )
    )

    # 旧版本仍可能读取这些列。统一改为非阻断模式并清空已耗尽标记，
    # 防止回滚/延迟启动的 Worker 再把历史计划暂停一次。
    connection.execute(
        sa.text(
            "UPDATE review_plans "
            "SET model_budget_mode = 'observe', "
            "    model_budget_exhausted_reason = NULL, "
            "    model_budget_exhausted_at = NULL, "
            "    model_budget_started_at = NULL"
        )
    )


def downgrade() -> None:
    """不恢复资源预算阻断；旧字段保留以支持数据库兼容。"""

    # 这是行为停用迁移，无法安全地把已经恢复的任务重新暂停；保持空操作
    # 也能让发布系统在需要回退代码时不重新引入预算错误状态。
    return None
