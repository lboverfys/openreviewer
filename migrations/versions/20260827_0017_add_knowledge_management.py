"""add persistent knowledge document management

Revision ID: 20260827_0017
Revises: 20260827_0016
Create Date: 2026-08-27
"""

from typing import Sequence, Union
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0017"
down_revision: Union[str, None] = "20260827_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_library",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(length=100), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="singleton_id"),
        sa.CheckConstraint(
            "revision >= 0",
            name="revision_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_library"),
    )
    op.bulk_insert(
        sa.table(
            "knowledge_library",
            sa.column("id", sa.SmallInteger()),
            sa.column("revision", sa.Integer()),
            sa.column("updated_by", sa.String()),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        ),
        [{
            "id": 1,
            "revision": 0,
            "updated_by": "system:migration",
            "updated_at": datetime(2026, 8, 27, tzinfo=UTC),
        }],
    )
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("updated_by", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "current_version > 0",
            name="current_version_positive",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_documents"),
        sa.UniqueConstraint("source", name="uq_knowledge_documents_source"),
    )
    op.create_index(
        "ix_knowledge_documents_active_source",
        "knowledge_documents",
        ["enabled", "archived_at", "source"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_documents_updated_at",
        "knowledge_documents",
        ["updated_at"],
        unique=False,
    )
    op.create_table(
        "knowledge_document_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "byte_size BETWEEN 1 AND 524288",
            name="byte_size_range",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_documents.id"],
            name="fk_knowledge_document_versions_document_id_knowledge_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_document_versions"),
        sa.UniqueConstraint(
            "document_id",
            "version",
            name="uq_knowledge_document_versions_document_id",
        ),
    )
    op.create_index(
        "ix_knowledge_document_versions_document_created",
        "knowledge_document_versions",
        ["document_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_document_versions_document_created",
        table_name="knowledge_document_versions",
    )
    op.drop_table("knowledge_document_versions")
    op.drop_index(
        "ix_knowledge_documents_updated_at",
        table_name="knowledge_documents",
    )
    op.drop_index(
        "ix_knowledge_documents_active_source",
        table_name="knowledge_documents",
    )
    op.drop_table("knowledge_documents")
    op.drop_table("knowledge_library")
