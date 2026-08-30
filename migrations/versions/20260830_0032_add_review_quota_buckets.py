"""增加账号、仓库和全局任务创建配额计数。

Revision ID: 20260830_0032
Revises: 20260830_0031
Create Date: 2026-08-30
"""

from collections.abc import Sequence
from contextlib import nullcontext

from alembic import op
import sqlalchemy as sa


revision: str = "20260830_0032"
down_revision: str | None = "20260830_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建可按窗口原子 UPSERT 的配额桶。"""

    op.create_table(
        "review_quota_buckets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("scope_key", sa.String(length=255), nullable=False),
        sa.Column("window", sa.String(length=10), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope IN ('user', 'repository', 'global')",
            name="scope_value",
        ),
        sa.CheckConstraint(
            '"window" IN (\'hour\', \'day\')',
            name="window_value",
        ),
        sa.CheckConstraint(
            "request_count >= 0",
            name="request_count_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope",
            "scope_key",
            "window",
            "window_start",
            name="uq_review_quota_bucket_identity",
        ),
    )
    postgresql = op.get_bind().dialect.name == "postgresql"
    options = {"postgresql_concurrently": True} if postgresql else {}
    context = op.get_context().autocommit_block() if postgresql else nullcontext()
    with context:
        op.create_index(
            "ix_review_quota_buckets_cleanup",
            "review_quota_buckets",
            ["window_start", "updated_at"],
            if_not_exists=postgresql,
            **options,
        )
    with op.batch_alter_table("review_quota_buckets") as batch:
        batch.alter_column(
            "request_count",
            existing_type=sa.Integer(),
            server_default=None,
        )


def downgrade() -> None:
    """移除配额计数表。"""

    postgresql = op.get_bind().dialect.name == "postgresql"
    options = {"postgresql_concurrently": True} if postgresql else {}
    context = op.get_context().autocommit_block() if postgresql else nullcontext()
    with context:
        op.drop_index(
            "ix_review_quota_buckets_cleanup",
            table_name="review_quota_buckets",
            if_exists=postgresql,
            **options,
        )
    op.drop_table("review_quota_buckets")
