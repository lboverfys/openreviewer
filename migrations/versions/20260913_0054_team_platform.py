"""增加团队用量账本、人工工作项、审查方案和仓库调度。"""

import sqlalchemy as sa
from alembic import op

revision = "20260913_0054"
down_revision = "20260912_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'repository_usage_months',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('installation_id', sa.BigInteger, nullable=False),
        sa.Column('repository', sa.String(255), nullable=False),
        sa.Column('repository_key', sa.String(255), nullable=False),
        sa.Column('month', sa.DateTime(timezone=True), nullable=False),
        sa.Column('request_count', sa.BigInteger, nullable=False),
        sa.Column('input_tokens', sa.BigInteger, nullable=False),
        sa.Column('output_tokens', sa.BigInteger, nullable=False),
        sa.Column('estimated_cost_microusd', sa.BigInteger, nullable=False),
        sa.Column('reserved_cost_microusd', sa.BigInteger, nullable=False),
        sa.Column('unknown_count', sa.BigInteger, nullable=False),
        sa.Column('uncertain_count', sa.BigInteger, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('installation_id', 'repository_key', 'month'),
        sa.CheckConstraint('request_count >= 0 AND input_tokens >= 0 AND output_tokens >= 0', name='usage_nonnegative'),
        sa.CheckConstraint('estimated_cost_microusd >= 0 AND reserved_cost_microusd >= 0', name='cost_nonnegative'),
        sa.CheckConstraint('unknown_count >= 0 AND uncertain_count >= 0', name='unknown_nonnegative'),
    )
    op.create_index('ix_repository_usage_months_scope_month', 'repository_usage_months', ['repository_key', 'month', 'installation_id'])
    op.create_index('ix_repository_usage_months_created', 'repository_usage_months', ['created_at', 'id'])
    op.create_table(
        'model_usage_requests',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('month_id', sa.String(64), nullable=False),
        sa.Column('review_run_id', sa.String(36), nullable=False),
        sa.Column('installation_id', sa.BigInteger, nullable=False),
        sa.Column('repository', sa.String(255), nullable=False),
        sa.Column('repository_key', sa.String(255), nullable=False),
        sa.Column('agent', sa.String(32), nullable=False),
        sa.Column('purpose', sa.String(16), nullable=False),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('model', sa.String(200), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('reserved_cost_microusd', sa.BigInteger, nullable=False),
        sa.Column('estimated_cost_microusd', sa.BigInteger, nullable=True),
        sa.Column('input_tokens', sa.BigInteger, nullable=True),
        sa.Column('output_tokens', sa.BigInteger, nullable=True),
        sa.Column('duration_ms', sa.Integer, nullable=True),
        sa.Column('response_status', sa.Integer, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('reserved','settled','uncertain')", name='status_value'),
        sa.CheckConstraint("purpose IN ('review','embedding','rerank')", name='purpose_value'),
        sa.CheckConstraint('reserved_cost_microusd >= 0', name='reservation_nonnegative'),
        sa.CheckConstraint('estimated_cost_microusd IS NULL OR estimated_cost_microusd >= 0', name='cost_nonnegative'),
    )
    op.create_index('ix_model_usage_requests_month_created', 'model_usage_requests', ['month_id', 'created_at', 'id'])
    op.create_index('ix_model_usage_requests_run', 'model_usage_requests', ['review_run_id', 'created_at'])
    op.create_index('ix_model_usage_requests_scope_created', 'model_usage_requests', ['repository_key', 'created_at', 'id'])
    op.create_table(
        'finding_work_items',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('source_run_id', sa.String(36), nullable=False),
        sa.Column('source_finding_id', sa.String(36), nullable=False),
        sa.Column('installation_id', sa.BigInteger, nullable=False),
        sa.Column('repository', sa.String(255), nullable=False),
        sa.Column('repository_key', sa.String(255), nullable=False),
        sa.Column('pull_request_number', sa.Integer, nullable=False),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('severity', sa.String(20), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('assignee', sa.String(100), nullable=True),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('note', sa.Text, nullable=False),
        sa.Column('fix_pull_request_number', sa.Integer, nullable=True),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('source_finding_id'),
        sa.CheckConstraint("status IN ('open','in_progress','resolved','wont_fix')", name='status_value'),
        sa.CheckConstraint('revision > 0', name='revision_positive'),
    )
    op.create_index('ix_finding_work_items_scope_created', 'finding_work_items', ['repository_key', 'created_at', 'id'])
    op.create_index('ix_finding_work_items_assignee_status', 'finding_work_items', ['assignee', 'status', 'created_at', 'id'])
    op.create_index('ix_finding_work_items_status_due', 'finding_work_items', ['status', 'due_at'])
    op.create_table(
        'review_profiles',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('repository', sa.String(255), nullable=False),
        sa.Column('repository_key', sa.String(255), nullable=False),
        sa.Column('note', sa.String(1000), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('ai_revision', sa.Integer, nullable=False),
        sa.Column('snapshot', sa.JSON, nullable=False),
        sa.Column('summary', sa.JSON, nullable=False),
        sa.Column('ciphertext', sa.LargeBinary, nullable=False),
        sa.Column('nonce', sa.LargeBinary, nullable=False),
        sa.Column('key_version', sa.Integer, nullable=False),
        sa.Column('created_by', sa.String(100), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_review_profiles_repository_created', 'review_profiles', ['repository_key', 'created_at', 'id'])
    op.create_table(
        'repository_schedules',
        sa.Column('repository_key', sa.String(255), primary_key=True),
        sa.Column('last_claimed_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("review_runs", sa.Column("approval_assignee", sa.String(100), nullable=True))
    op.add_column("review_runs", sa.Column("approval_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("review_runs", sa.Column("approval_due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("review_tasks", sa.Column("first_claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_review_runs_approval_todo", "review_runs", ["workflow_status", "approval_assignee", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_review_runs_approval_todo", table_name="review_runs")
    op.drop_column("review_tasks", "first_claimed_at")
    op.drop_column("review_runs", "approval_due_at")
    op.drop_column("review_runs", "approval_requested_at")
    op.drop_column("review_runs", "approval_assignee")
    op.drop_table('repository_schedules')
    op.drop_table('review_profiles')
    op.drop_table('finding_work_items')
    op.drop_table('model_usage_requests')
    op.drop_table('repository_usage_months')
