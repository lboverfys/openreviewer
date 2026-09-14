"""按职责集中维护的 knowledge 数据记录。"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from persistence.models.base import Base, utc_now


class KnowledgeDeletionRecord(Base):
    """只保留删除事实，阻止内置资料再次导入；不保留正文。"""

    __tablename__ = "knowledge_deletions"
    source: Mapped[str] = mapped_column(String(200), primary_key=True)
    deleted_by: Mapped[str] = mapped_column(String(100), nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeLibraryRecord(Base):
    """知识库全局版本；每次可见内容变化都会递增。"""

    __tablename__ = "knowledge_library"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton_id"),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class KnowledgeDocumentRecord(Base):
    """一份可启停、归档且指向不可变当前版本的 Markdown 文档。"""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint("current_version > 0", name="current_version_positive"),
        UniqueConstraint("source"),
        Index(
            "ix_knowledge_documents_active_source",
            "enabled",
            "archived_at",
            "source",
        ),
        Index("ix_knowledge_documents_updated_at", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    repository_scope: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class KnowledgeDocumentVersionRecord(Base):
    """知识文档的一份不可变 Markdown 内容版本。"""

    __tablename__ = "knowledge_document_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "byte_size BETWEEN 1 AND 524288",
            name="byte_size_range",
        ),
        UniqueConstraint("document_id", "version"),
        Index(
            "ix_knowledge_document_versions_document_created",
            "document_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
