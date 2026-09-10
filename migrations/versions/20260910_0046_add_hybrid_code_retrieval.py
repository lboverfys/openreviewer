"""增加版本化代码索引、检索记录和评测数据。

Revision ID: 20260910_0046
Revises: 20260902_0045
Create Date: 2026-09-09 21:38:53.881938
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = '20260910_0046'
down_revision: str | Sequence[str] | None = '20260902_0045'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('code_chunks',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('file', sa.String(length=1024), nullable=False),
    sa.Column('blob_sha', sa.String(length=64), nullable=False),
    sa.Column('language', sa.String(length=20), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('symbol', sa.String(length=1024), nullable=False),
    sa.Column('start_line', sa.Integer(), nullable=False),
    sa.Column('end_line', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('embedding_hash', sa.String(length=64), nullable=False),
    sa.Column('aliases', sa.JSON(), nullable=False),
    sa.Column('references', sa.JSON(), nullable=False),
    sa.Column('tokens', sa.JSON(), nullable=False),
    sa.Column('fragment', sa.Integer(), nullable=False),
    sa.Column('parse_error', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_code_chunks'))
    )
    op.create_table('code_embeddings',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('configuration_key', sa.String(length=64), nullable=False),
    sa.Column('input_hash', sa.String(length=64), nullable=False),
    sa.Column('embedding', Vector(1024).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_code_embeddings'))
    )
    op.create_index('ix_code_embeddings_configuration', 'code_embeddings', ['configuration_key'], unique=False)
    op.create_index('ix_code_embeddings_hnsw', 'code_embeddings', ['embedding'], unique=False, postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.create_table('code_indexes',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('installation_id', sa.BigInteger(), nullable=False),
    sa.Column('repository_id', sa.BigInteger(), nullable=False),
    sa.Column('repository', sa.String(length=255), nullable=False),
    sa.Column('head_sha', sa.String(length=64), nullable=False),
    sa.Column('configuration_key', sa.String(length=64), nullable=False),
    sa.Column('embedding_model', sa.String(length=200), nullable=False),
    sa.Column('dimensions', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('source_target', sa.JSON(), nullable=False),
    sa.Column('lease_owner', sa.String(length=36), nullable=True),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('file_count', sa.Integer(), nullable=False),
    sa.Column('chunk_count', sa.Integer(), nullable=False),
    sa.Column('relation_count', sa.Integer(), nullable=False),
    sa.Column('embedded_count', sa.Integer(), nullable=False),
    sa.Column('reused_count', sa.Integer(), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('parse_errors', sa.JSON(), nullable=False),
    sa.Column('error', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('queued','building','ready','failed')", name=op.f('ck_code_indexes_status_value')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_code_indexes'))
    )
    op.create_index('ix_code_indexes_repository_created', 'code_indexes', ['repository_id', 'created_at'], unique=False)
    op.create_index('ix_code_indexes_status_lease', 'code_indexes', ['status', 'lease_until'], unique=False)
    op.create_table('retrieval_settings',
    sa.Column('id', sa.SmallInteger(), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('settings', sa.JSON(), nullable=False),
    sa.Column('ciphertext', sa.LargeBinary(), nullable=True),
    sa.Column('nonce', sa.LargeBinary(length=12), nullable=True),
    sa.Column('key_version', sa.Integer(), nullable=True),
    sa.Column('tested_fingerprint', sa.String(length=64), nullable=True),
    sa.Column('updated_by', sa.String(length=100), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('id = 1', name=op.f('ck_retrieval_settings_singleton_id')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_retrieval_settings'))
    )
    op.create_table('code_index_chunks',
    sa.Column('index_id', sa.String(length=64), nullable=False),
    sa.Column('chunk_id', sa.String(length=64), nullable=False),
    sa.Column('embedding_id', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['chunk_id'], ['code_chunks.id'], name=op.f('fk_code_index_chunks_chunk_id_code_chunks')),
    sa.ForeignKeyConstraint(['embedding_id'], ['code_embeddings.id'], name=op.f('fk_code_index_chunks_embedding_id_code_embeddings')),
    sa.ForeignKeyConstraint(['index_id'], ['code_indexes.id'], name=op.f('fk_code_index_chunks_index_id_code_indexes'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('index_id', 'chunk_id', name=op.f('pk_code_index_chunks'))
    )
    op.create_index('ix_code_index_chunks_embedding', 'code_index_chunks', ['embedding_id'], unique=False)
    op.create_table('code_relations',
    sa.Column('index_id', sa.String(length=64), nullable=False),
    sa.Column('source_id', sa.String(length=64), nullable=False),
    sa.Column('target_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.ForeignKeyConstraint(['index_id'], ['code_indexes.id'], name=op.f('fk_code_relations_index_id_code_indexes'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['code_chunks.id'], name=op.f('fk_code_relations_source_id_code_chunks')),
    sa.ForeignKeyConstraint(['target_id'], ['code_chunks.id'], name=op.f('fk_code_relations_target_id_code_chunks')),
    sa.PrimaryKeyConstraint('index_id', 'source_id', 'target_id', 'kind', name=op.f('pk_code_relations'))
    )
    op.create_table('retrieval_evaluations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('index_id', sa.String(length=64), nullable=False),
    sa.Column('report', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['index_id'], ['code_indexes.id'], name=op.f('fk_retrieval_evaluations_index_id_code_indexes'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_retrieval_evaluations'))
    )
    op.create_index('ix_retrieval_evaluations_index_created', 'retrieval_evaluations', ['index_id', 'created_at'], unique=False)
    op.create_table('retrieval_traces',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('index_id', sa.String(length=64), nullable=False),
    sa.Column('review_run_id', sa.String(length=36), nullable=True),
    sa.Column('agent', sa.String(length=32), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['index_id'], ['code_indexes.id'], name=op.f('fk_retrieval_traces_index_id_code_indexes'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['review_run_id'], ['review_runs.id'], name=op.f('fk_retrieval_traces_review_run_id_review_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_retrieval_traces'))
    )
    op.create_index('ix_retrieval_traces_index_created', 'retrieval_traces', ['index_id', 'created_at'], unique=False)
    op.create_index('ix_retrieval_traces_review_created', 'retrieval_traces', ['review_run_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_retrieval_traces_review_created', table_name='retrieval_traces')
    op.drop_index('ix_retrieval_traces_index_created', table_name='retrieval_traces')
    op.drop_table('retrieval_traces')
    op.drop_index('ix_retrieval_evaluations_index_created', table_name='retrieval_evaluations')
    op.drop_table('retrieval_evaluations')
    op.drop_table('code_relations')
    op.drop_index('ix_code_index_chunks_embedding', table_name='code_index_chunks')
    op.drop_table('code_index_chunks')
    op.drop_table('retrieval_settings')
    op.drop_index('ix_code_indexes_status_lease', table_name='code_indexes')
    op.drop_index('ix_code_indexes_repository_created', table_name='code_indexes')
    op.drop_table('code_indexes')
    op.drop_index('ix_code_embeddings_hnsw', table_name='code_embeddings', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.drop_index('ix_code_embeddings_configuration', table_name='code_embeddings')
    op.drop_table('code_embeddings')
    op.drop_table('code_chunks')
