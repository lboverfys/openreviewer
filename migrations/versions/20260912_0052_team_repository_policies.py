"""增加团队成员与仓库策略，并保存任务使用的策略版本。"""

import sqlalchemy as sa
from alembic import op

revision = "20260912_0052"
down_revision = "20260912_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_members",
        sa.Column("username", sa.String(100), primary_key=True),
        sa.Column("username_key", sa.String(100), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("resource_scope", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(100), nullable=False),
        sa.CheckConstraint("revision >= 0", name="revision_nonnegative"),
        sa.CheckConstraint(
            "role IN ('viewer', 'adjudicator', 'publisher', 'administrator')",
            name="role_value",
        ),
    )
    op.create_index("ix_team_members_created", "team_members", ["created_at", "username"])
    op.create_table(
        "repository_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("repository_key", sa.String(255), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(100), nullable=False),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
        sa.UniqueConstraint("repository_key"),
    )
    op.create_index(
        "ix_repository_policies_created", "repository_policies", ["created_at", "id"]
    )
    op.create_index("ix_admin_sessions_username", "admin_sessions", ["username"])
    op.create_index(
        "ix_outbox_events_type_occurred", "outbox_events",
        ["aggregate_type", "occurred_at", "id"],
    )
    op.add_column("review_runs", sa.Column("repository_policy", sa.JSON(), nullable=True))
    op.add_column(
        "review_runs",
        sa.Column("model_request_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("review_runs", "model_request_count")
    op.drop_column("review_runs", "repository_policy")
    op.drop_index("ix_admin_sessions_username", table_name="admin_sessions")
    op.drop_index("ix_outbox_events_type_occurred", table_name="outbox_events")
    op.drop_table("repository_policies")
    op.drop_table("team_members")
