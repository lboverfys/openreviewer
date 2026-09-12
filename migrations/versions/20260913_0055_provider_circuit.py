"""增加共享供应商通道、并发请求租约和故障熔断状态。"""

import sqlalchemy as sa
from alembic import op

revision = "20260913_0055"
down_revision = "20260913_0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("provider_circuits",
        sa.Column("connection_key", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("open_until", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("model_usage_requests", sa.Column("connection_key", sa.String(64)))
    op.add_column("model_usage_requests", sa.Column("permit_expires_at", sa.DateTime(timezone=True)))
    op.create_index("ix_model_usage_requests_channel_active", "model_usage_requests", ["connection_key", "status", "permit_expires_at"])
    op.create_index("ix_provider_circuits_updated_at", "provider_circuits", ["updated_at"])
    op.create_index("ix_repository_usage_months_period", "repository_usage_months", ["month", "created_at", "id"])
    op.create_index("ix_model_calls_created", "model_calls", ["created_at"])
    op.create_index("ix_review_tasks_updated", "review_tasks", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_review_tasks_updated", table_name="review_tasks")
    op.drop_index("ix_model_calls_created", table_name="model_calls")
    op.drop_index("ix_repository_usage_months_period", table_name="repository_usage_months")
    op.drop_index("ix_provider_circuits_updated_at", table_name="provider_circuits")
    op.drop_index("ix_model_usage_requests_channel_active", table_name="model_usage_requests")
    op.drop_column("model_usage_requests", "permit_expires_at")
    op.drop_column("model_usage_requests", "connection_key")
    op.drop_table("provider_circuits")
