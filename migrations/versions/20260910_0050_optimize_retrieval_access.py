"""支持向量候选按快照关联，以及历史临时检索记录的有界清理。"""

import sqlalchemy as sa
from alembic import op

revision = "20260910_0050"
down_revision = "20260910_0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_code_index_chunks_index_embedding", "code_index_chunks", ["index_id", "embedding_id", "chunk_id"])
    op.create_index("ix_retrieval_traces_temporary_created", "retrieval_traces", ["created_at", "id"],
        postgresql_where=sa.text("review_run_id IS NULL"), sqlite_where=sa.text("review_run_id IS NULL"))


def downgrade() -> None:
    op.drop_index("ix_retrieval_traces_temporary_created", table_name="retrieval_traces")
    op.drop_index("ix_code_index_chunks_index_embedding", table_name="code_index_chunks")
