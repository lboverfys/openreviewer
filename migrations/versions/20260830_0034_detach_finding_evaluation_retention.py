"""让 Finding 评测样本独立于 Finding 保留期。

Revision ID: 20260830_0034
Revises: 20260830_0033
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0034"
down_revision: str | None = "20260830_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "finding_evaluations"
_FINDING_FK = "fk_finding_evaluations_finding"


def upgrade() -> None:
    """移除会在 ReviewRun 清理时级联删除评测样本的外键。"""

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite 只能通过 batch 重建表来移除外键；其余列、索引和主键由
        # Alembic 反射后原样复制。
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.drop_constraint(_FINDING_FK, type_="foreignkey")
        return
    op.drop_constraint(_FINDING_FK, _TABLE, type_="foreignkey")


def downgrade() -> None:
    """仅在确认没有脱离 Finding 的历史样本后恢复旧外键。"""

    bind = op.get_bind()
    evaluations = sa.table(
        _TABLE,
        sa.column("finding_id", sa.String(length=36)),
    )
    findings = sa.table(
        "review_findings",
        sa.column("id", sa.String(length=36)),
    )
    dangling = bind.scalar(
        sa.select(sa.literal(1))
        .select_from(
            evaluations.outerjoin(
                findings,
                evaluations.c.finding_id == findings.c.id,
            )
        )
        .where(findings.c.id.is_(None))
        .limit(1)
    )
    if dangling is not None:
        raise RuntimeError(
            "finding_evaluations contains historical rows whose Finding was "
            "already removed; cannot restore the cascading foreign key"
        )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.create_foreign_key(
                _FINDING_FK,
                "review_findings",
                ["finding_id"],
                ["id"],
                ondelete="CASCADE",
            )
        return
    op.create_foreign_key(
        _FINDING_FK,
        _TABLE,
        "review_findings",
        ["finding_id"],
        ["id"],
        ondelete="CASCADE",
    )
