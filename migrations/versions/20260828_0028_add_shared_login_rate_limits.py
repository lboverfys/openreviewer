"""增加跨 API 副本共享的登录限流状态。

Revision ID: 20260828_0028
Revises: 20260828_0027
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0028"
down_revision: str | None = "20260828_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建只保存 SHA-256 键和有界固定窗口计数的表。"""

    op.create_table(
        "login_rate_limits",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_count > 0",
            name="attempt_count_positive",
        ),
        sa.PrimaryKeyConstraint("key_hash", name="pk_login_rate_limits"),
    )
    op.create_index(
        "ix_login_rate_limits_updated_at",
        "login_rate_limits",
        ["updated_at"],
    )


def downgrade() -> None:
    """移除可重建的登录限流状态。"""

    op.drop_index(
        "ix_login_rate_limits_updated_at",
        table_name="login_rate_limits",
    )
    op.drop_table("login_rate_limits")
