"""为资源范围查询增加大小写规范化键。

Revision ID: 20260830_0039
Revises: 20260830_0038
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0039"
down_revision: str | None = "20260830_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """回填并索引 GitHub 仓库的小写键，保留原字段的展示大小写。"""

    connection = op.get_bind()
    if connection.dialect.name not in {"postgresql", "sqlite"}:
        raise RuntimeError("repository scope key migration supports PostgreSQL and SQLite only")

    op.add_column(
        "review_runs",
        sa.Column("repository_key", sa.String(length=255), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE review_runs "
            "SET repository_key = lower(repository) "
            "WHERE repository_key IS NULL"
        )
    )
    if connection.dialect.name == "postgresql":
        op.alter_column(
            "review_runs",
            "repository_key",
            existing_type=sa.String(length=255),
            nullable=False,
        )
    else:
        with op.batch_alter_table("review_runs", recreate="always") as batch_op:
            batch_op.alter_column(
                "repository_key",
                existing_type=sa.String(length=255),
                nullable=False,
            )
    op.create_index(
        "ix_review_runs_installation_repository_key",
        "review_runs",
        ["installation_id", "repository_key"],
        unique=False,
    )


def downgrade() -> None:
    """移除规范化键和对应索引。"""

    connection = op.get_bind()
    if connection.dialect.name not in {"postgresql", "sqlite"}:
        raise RuntimeError("repository scope key migration supports PostgreSQL and SQLite only")
    op.drop_index(
        "ix_review_runs_installation_repository_key",
        table_name="review_runs",
    )
    if connection.dialect.name == "postgresql":
        op.drop_column("review_runs", "repository_key")
    else:
        with op.batch_alter_table("review_runs", recreate="always") as batch_op:
            batch_op.drop_column("repository_key")
