"""为跨事务的人工发布增加并发尝试令牌。

Revision ID: 20260830_0033
Revises: 20260830_0032
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0033"
down_revision: str | None = "20260830_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为历史运行增加可空令牌；新发布开始时再写入具体值。"""

    op.add_column(
        "review_runs",
        sa.Column("publish_attempt_token", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """移除人工发布并发令牌。"""

    op.drop_column("review_runs", "publish_attempt_token")
