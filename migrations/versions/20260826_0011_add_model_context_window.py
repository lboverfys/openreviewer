"""增加模型上下文窗口配置。

Revision ID: 20260826_0011
Revises: 20260826_0010
Create Date: 2026-08-26 20:20:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260826_0011"
down_revision: str | None = "20260826_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存上下文窗口，并迁移已知的 DeepSeek V4 Flash 配置。"""

    with op.batch_alter_table("ai_provider_configs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "context_window_tokens",
                sa.Integer(),
                server_default=sa.text("128000"),
                nullable=False,
            )
        )
    op.execute(
        sa.text(
            "UPDATE ai_provider_configs "
            "SET context_window_tokens = 1000000 "
            "WHERE lower(model) = 'deepseek-v4-flash' "
            "OR lower(model) LIKE '%/deepseek-v4-flash'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE ai_provider_configs "
            "SET context_window_tokens = max_output_tokens + 4096 "
            "WHERE context_window_tokens - max_output_tokens < 4096"
        )
    )
    with op.batch_alter_table("ai_provider_configs") as batch_op:
        batch_op.create_check_constraint(
            "context_window_tokens_range",
            "context_window_tokens BETWEEN 8192 AND 4000000",
        )
        batch_op.create_check_constraint(
            "context_reserves_input",
            "context_window_tokens - max_output_tokens >= 4096",
        )
    op.execute(
        sa.text(
            "UPDATE review_runs SET "
            "execution_status = 'completed', "
            "review_conclusion = CASE WHEN EXISTS ("
            "SELECT 1 FROM review_findings rf "
            "WHERE rf.review_run_id = review_runs.id"
            ") THEN 'findings_present' ELSE 'no_confirmed_findings' END, "
            "coverage_status = CASE WHEN EXISTS ("
            "SELECT 1 FROM review_plans rp "
            "WHERE rp.review_run_id = review_runs.id "
            "AND rp.rules_complete = false"
            ") OR EXISTS ("
            "SELECT 1 FROM review_plans rp "
            "JOIN review_file_plans rfp ON rfp.review_plan_id = rp.id "
            "WHERE rp.review_run_id = review_runs.id "
            "AND rfp.decision <> 'planned'"
            ") THEN 'partial' ELSE 'complete' END "
            "WHERE id IN (SELECT review_run_id FROM review_plans "
            "WHERE model_review_completed_at IS NOT NULL)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE review_tasks SET execution_status = 'completed', "
            "claimed_from_status = NULL, lease_owner = NULL, "
            "lease_expires_at = NULL, last_error = NULL, "
            "last_error_code = NULL, last_error_retryable = NULL, "
            "last_error_details = NULL "
            "WHERE review_run_id IN (SELECT review_run_id FROM review_plans "
            "WHERE model_review_completed_at IS NOT NULL)"
        )
    )


def downgrade() -> None:
    """移除模型上下文窗口配置。"""

    with op.batch_alter_table("ai_provider_configs") as batch_op:
        batch_op.drop_constraint("context_reserves_input", type_="check")
        batch_op.drop_constraint("context_window_tokens_range", type_="check")
        batch_op.drop_column("context_window_tokens")
