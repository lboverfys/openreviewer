"""分离基础索引与向量补全，增加共享调用治理和增量源码缓存。"""

import sqlalchemy as sa
from alembic import op

revision = "20260910_0049"
down_revision = "20260910_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("code_embeddings") as batch:
        batch.add_column(sa.Column("purpose", sa.String(12), nullable=False, server_default="code"))
    op.create_index("ix_code_embeddings_purpose_created", "code_embeddings", ["purpose", "created_at"])
    with op.batch_alter_table("code_indexes") as batch:
        batch.add_column(sa.Column("lexical_ready", sa.Boolean(), nullable=False, server_default="false"))
        batch.add_column(sa.Column("vector_status", sa.String(20), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("vector_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("vector_error", sa.String(500)))
    op.execute("UPDATE code_indexes SET lexical_ready = true, vector_status = 'ready', vector_count = chunk_count WHERE status = 'ready'")
    with op.batch_alter_table("code_index_chunks") as batch:
        batch.alter_column("embedding_id", existing_type=sa.String(64), nullable=True)
    op.create_table("code_source_cache",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_code_source_cache_created_at", "code_source_cache", ["created_at"])
    op.create_table("retrieval_provider_state",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True)))
    op.create_table("retrieval_rerank_cache",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("ranking", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_retrieval_rerank_cache_created_at", "retrieval_rerank_cache", ["created_at"])
    op.create_table("retrieval_request_budgets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_retrieval_request_budgets_created_at", "retrieval_request_budgets", ["created_at"])
    for table in ("code_indexes", "code_chunks", "code_parse_cache", "code_embeddings"):
        op.create_index(f"ix_{table}_created_at", table, ["created_at"])
    op.create_index("ix_code_index_chunks_chunk", "code_index_chunks", ["chunk_id"])
    op.create_index("ix_code_embeddings_configuration_input", "code_embeddings", ["configuration_key", "input_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_code_embeddings_purpose_created", table_name="code_embeddings")
    with op.batch_alter_table("code_embeddings") as batch:
        batch.drop_column("purpose")
    op.drop_index("ix_code_embeddings_configuration_input", table_name="code_embeddings")
    op.drop_index("ix_code_index_chunks_chunk", table_name="code_index_chunks")
    for table in ("code_indexes", "code_chunks", "code_parse_cache", "code_embeddings"):
        op.drop_index(f"ix_{table}_created_at", table_name=table)
    # 旧版本不支持无向量的关联，保留其代码块，移除尚未补全的关联。
    op.execute("DELETE FROM code_index_chunks WHERE embedding_id IS NULL")
    with op.batch_alter_table("code_index_chunks") as batch:
        batch.alter_column("embedding_id", existing_type=sa.String(64), nullable=False)
    for name in ("retrieval_request_budgets", "retrieval_rerank_cache", "retrieval_provider_state", "code_source_cache"):
        op.drop_table(name)
    with op.batch_alter_table("code_indexes") as batch:
        for name in ("vector_error", "vector_count", "vector_status", "lexical_ready"):
            batch.drop_column(name)
