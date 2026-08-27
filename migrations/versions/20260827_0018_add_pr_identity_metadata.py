"""保存 Pull Request 作者与分支身份信息。

Revision ID: 20260827_0018
Revises: 20260827_0017
Create Date: 2026-08-27
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0018"
down_revision: str | None = "20260827_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """给不可变 PR 版本快照补充可直接展示的身份字段。"""

    with op.batch_alter_table("pull_request_versions") as batch_op:
        batch_op.add_column(sa.Column("author_login", sa.String(100)))
        batch_op.add_column(sa.Column("html_url", sa.String(2048)))
        batch_op.add_column(sa.Column("head_repository", sa.String(255)))
        batch_op.add_column(sa.Column("head_ref", sa.String(1024)))
        batch_op.add_column(sa.Column("base_repository", sa.String(255)))
        batch_op.add_column(sa.Column("base_ref", sa.String(1024)))


def downgrade() -> None:
    """移除 PR 身份展示字段。"""

    with op.batch_alter_table("pull_request_versions") as batch_op:
        batch_op.drop_column("base_ref")
        batch_op.drop_column("base_repository")
        batch_op.drop_column("head_ref")
        batch_op.drop_column("head_repository")
        batch_op.drop_column("html_url")
        batch_op.drop_column("author_login")
