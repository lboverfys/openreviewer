"""增加可吊销管理员会话。

Revision ID: 20260828_0020
Revises: 20260827_0019
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0020"
down_revision: str | None = "20260827_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建只保存会话 ID 哈希和吊销状态的表。"""

    op.create_table(
        "admin_sessions",
        sa.Column("session_hash", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column(
            "role",
            sa.String(length=32),
            nullable=False,
            server_default="administrator",
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("session_hash", name="pk_admin_sessions"),
    )
    op.create_index(
        "ix_admin_sessions_expires_at",
        "admin_sessions",
        ["expires_at"],
    )
    op.create_index(
        "ix_admin_sessions_active",
        "admin_sessions",
        ["revoked_at", "expires_at"],
    )


def downgrade() -> None:
    """移除可吊销会话表。"""

    op.drop_index("ix_admin_sessions_active", table_name="admin_sessions")
    op.drop_index("ix_admin_sessions_expires_at", table_name="admin_sessions")
    op.drop_table("admin_sessions")
