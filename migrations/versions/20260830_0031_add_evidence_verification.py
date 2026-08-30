"""为 Finding 增加独立的源码证据核验结果。

Revision ID: 20260830_0031
Revises: 20260828_0030
Create Date: 2026-08-30
"""

from collections.abc import Sequence
from contextlib import nullcontext

from alembic import op
import sqlalchemy as sa


revision: str = "20260830_0031"
down_revision: str | None = "20260828_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """以保守默认值增加证据核验列，并建立查询索引。"""

    with op.batch_alter_table("review_findings") as batch:
        batch.add_column(
            sa.Column(
                "evidence_verification_status",
                sa.String(length=20),
                nullable=False,
                server_default="unverified",
            )
        )
        batch.add_column(
            sa.Column(
                "evidence_verification_reason",
                sa.String(length=120),
                nullable=False,
                server_default="legacy_not_reverified",
            )
        )
        batch.add_column(
            sa.Column("evidence_verified_at", sa.DateTime(timezone=True))
        )
        batch.create_check_constraint(
            "evidence_verification_status_value",
            "evidence_verification_status IN "
            "('unverified', 'verified', 'rejected', 'not_applicable')",
        )
        batch.alter_column(
            "evidence_verification_status",
            existing_type=sa.String(length=20),
            server_default=None,
        )
        batch.alter_column(
            "evidence_verification_reason",
            existing_type=sa.String(length=120),
            server_default=None,
        )

    postgresql = op.get_bind().dialect.name == "postgresql"
    options = {"postgresql_concurrently": True} if postgresql else {}
    context = op.get_context().autocommit_block() if postgresql else nullcontext()
    with context:
        op.create_index(
            "ix_review_findings_run_evidence_verification",
            "review_findings",
            ["review_run_id", "evidence_verification_status"],
            if_not_exists=postgresql,
            **options,
        )


def downgrade() -> None:
    """移除源码证据核验字段；发布前应先确认没有依赖这些列。"""

    postgresql = op.get_bind().dialect.name == "postgresql"
    options = {"postgresql_concurrently": True} if postgresql else {}
    context = op.get_context().autocommit_block() if postgresql else nullcontext()
    with context:
        op.drop_index(
            "ix_review_findings_run_evidence_verification",
            table_name="review_findings",
            if_exists=postgresql,
            **options,
        )
    with op.batch_alter_table("review_findings") as batch:
        batch.drop_constraint("evidence_verification_status_value", type_="check")
        batch.drop_column("evidence_verified_at")
        batch.drop_column("evidence_verification_reason")
        batch.drop_column("evidence_verification_status")
