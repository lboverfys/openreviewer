"""按源码 Blob 与解析器版本复用静态解析结果。"""

import sqlalchemy as sa
from alembic import op

revision = "20260910_0048"
down_revision = "20260910_0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "code_parse_cache",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("parser_version", sa.String(50), nullable=False),
        sa.Column("chunks", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table("code_indexes") as batch:
        batch.add_column(sa.Column("parsed_files", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("reused_files", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("code_indexes") as batch:
        batch.drop_column("reused_files")
        batch.drop_column("parsed_files")
    op.drop_table("code_parse_cache")
