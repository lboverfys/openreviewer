"""按职责集中维护的 retrieval 数据记录。"""

from datetime import datetime

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from persistence.models.base import Base, utc_now


class RetrievalSettingsRecord(Base):
    __tablename__ = "retrieval_settings"
    __table_args__ = (CheckConstraint("id = 1", name="singleton_id"),)
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    settings: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary(12))
    key_version: Mapped[int | None] = mapped_column(Integer)
    tested_fingerprint: Mapped[str | None] = mapped_column(String(64))
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class CodeIndexRecord(Base):
    __tablename__ = "code_indexes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','building','ready','failed')", name="status_value"
        ),
        Index("ix_code_indexes_repository_created", "repository_id", "created_at"),
        Index("ix_code_indexes_status_lease", "status", "lease_until"),
        Index("ix_code_indexes_created_at", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_key: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    lexical_ready: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    vector_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    vector_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    vector_error: Mapped[str | None] = mapped_column(String(500))
    source_target: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parsed_files: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reused_files: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reused_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    parse_errors: Mapped[list[object]] = mapped_column(
        JSON, nullable=False, default=list
    )
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CodeChunkRecord(Base):
    __tablename__ = "code_chunks"
    __table_args__ = (Index("ix_code_chunks_created_at", "created_at"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    file: Mapped[str] = mapped_column(String(1024), nullable=False)
    blob_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    symbol: Mapped[str] = mapped_column(String(1024), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    aliases: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    references: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    tokens: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    fragment: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parse_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class CodeParseRecord(Base):
    __tablename__ = "code_parse_cache"
    __table_args__ = (Index("ix_code_parse_cache_created_at", "created_at"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parser_version: Mapped[str] = mapped_column(String(50), nullable=False)
    chunks: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class CodeEmbeddingRecord(Base):
    __tablename__ = "code_embeddings"
    __table_args__ = (
        Index("ix_code_embeddings_configuration", "configuration_key"),
        Index(
            "ix_code_embeddings_configuration_input",
            "configuration_key",
            "input_hash",
            unique=True,
        ),
        Index("ix_code_embeddings_created_at", "created_at"),
        Index("ix_code_embeddings_purpose_created", "purpose", "created_at"),
        Index(
            "ix_code_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    configuration_key: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(12), nullable=False, default="code", server_default="code"
    )
    embedding: Mapped[list[float]] = mapped_column(
        Vector(1024).with_variant(JSON, "sqlite"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class CodeIndexChunkRecord(Base):
    __tablename__ = "code_index_chunks"
    __table_args__ = (
        Index("ix_code_index_chunks_embedding", "embedding_id"),
        Index("ix_code_index_chunks_chunk", "chunk_id"),
        Index(
            "ix_code_index_chunks_index_embedding",
            "index_id",
            "embedding_id",
            "chunk_id",
        ),
    )
    index_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("code_indexes.id", ondelete="CASCADE"), primary_key=True
    )
    chunk_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("code_chunks.id"), primary_key=True
    )
    embedding_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("code_embeddings.id"), nullable=True
    )


class CodeRelationRecord(Base):
    __tablename__ = "code_relations"
    index_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("code_indexes.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("code_chunks.id"), primary_key=True
    )
    target_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("code_chunks.id"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(24), primary_key=True)


class RetrievalTraceRecord(Base):
    __tablename__ = "retrieval_traces"
    __table_args__ = (
        Index("ix_retrieval_traces_review_created", "review_run_id", "created_at"),
        UniqueConstraint(
            "review_run_id",
            "plan_fingerprint",
            "agent",
            name="uq_retrieval_trace_plan_agent",
        ),
        Index("ix_retrieval_traces_index_created", "index_id", "created_at"),
        Index(
            "ix_retrieval_traces_temporary_created",
            "created_at",
            "id",
            postgresql_where=sa.text("review_run_id IS NULL"),
            sqlite_where=sa.text("review_run_id IS NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    index_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("code_indexes.id", ondelete="CASCADE"), nullable=False
    )
    review_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("review_runs.id", ondelete="CASCADE")
    )
    agent: Mapped[str | None] = mapped_column(String(32))
    plan_fingerprint: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class RetrievalEvaluationRecord(Base):
    __tablename__ = "retrieval_evaluations"
    __table_args__ = (
        Index("ix_retrieval_evaluations_index_created", "index_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    index_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("code_indexes.id", ondelete="CASCADE"), nullable=False
    )
    report: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class CodeSourceCacheRecord(Base):
    __tablename__ = "code_source_cache"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )


class RetrievalProviderStateRecord(Base):
    __tablename__ = "retrieval_provider_state"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetrievalRerankCacheRecord(Base):
    __tablename__ = "retrieval_rerank_cache"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ranking: Mapped[list[list[float]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )


class RetrievalRequestBudgetRecord(Base):
    __tablename__ = "retrieval_request_budgets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )
