"""增加 Finding 真实评测样本。

Revision ID: 20260828_0025
Revises: 20260828_0024
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0025"
down_revision: str | None = "20260828_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存人工裁决样本，并为按仓库、风险域的滚动评测建立索引。"""

    op.create_table(
        "finding_evaluations",
        sa.Column("finding_id", sa.String(length=36), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("adjudicated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("adjudicated_by", sa.String(length=100), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category IN ('architecture', 'authorization', 'security', "
            "'database', 'business_contract', 'test_gap', 'reliability')",
            name=op.f("ck_finding_evaluations_category_value"),
        ),
        sa.CheckConstraint(
            "severity IN ('critical', 'high', 'medium', 'low')",
            name=op.f("ck_finding_evaluations_severity_value"),
        ),
        sa.CheckConstraint(
            "verdict IN ('valid', 'false_positive')",
            name=op.f("ck_finding_evaluations_verdict_value"),
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["review_findings.id"],
            name=op.f("fk_finding_evaluations_finding"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "finding_id", name=op.f("pk_finding_evaluations")
        ),
    )
    op.create_index(
        "ix_finding_evaluations_repository_category_time",
        "finding_evaluations",
        ["repository_id", "category", "adjudicated_at"],
    )
    op.create_index(
        "ix_finding_evaluations_repository_category_verdict",
        "finding_evaluations",
        ["repository_id", "category", "verdict"],
    )
    op.create_index(
        "ix_finding_evaluations_repository_time",
        "finding_evaluations",
        ["repository_id", "adjudicated_at", "finding_id"],
    )

    # 历史人工裁决由维护命令分批转换；迁移本身不扫描 review_findings。


def downgrade() -> None:
    """移除可再生成的评测样本，不改变 Finding 人工裁决。"""

    op.drop_index(
        "ix_finding_evaluations_repository_time",
        table_name="finding_evaluations",
    )
    op.drop_index(
        "ix_finding_evaluations_repository_category_verdict",
        table_name="finding_evaluations",
    )
    op.drop_index(
        "ix_finding_evaluations_repository_category_time",
        table_name="finding_evaluations",
    )
    op.drop_table("finding_evaluations")
