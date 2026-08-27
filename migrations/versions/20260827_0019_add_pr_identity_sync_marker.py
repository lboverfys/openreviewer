"""记录 Pull Request 身份信息最近一次同步时间。

Revision ID: 20260827_0019
Revises: 20260827_0018
Create Date: 2026-08-27
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0019"
down_revision: str | None = "20260827_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加可空同步标记；空值表示历史记录仍需要向 GitHub 补全。"""

    with op.batch_alter_table("pull_request_versions") as batch_op:
        batch_op.add_column(
            sa.Column("identity_fetched_at", sa.DateTime(timezone=True))
        )


def downgrade() -> None:
    """移除 Pull Request 身份信息同步标记。"""

    with op.batch_alter_table("pull_request_versions") as batch_op:
        batch_op.drop_column("identity_fetched_at")
