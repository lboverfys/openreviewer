"""为评测样本保留期清理增加全局时间索引。

Revision ID: 20260830_0035
Revises: 20260830_0034
Create Date: 2026-08-30
"""

from collections.abc import Sequence
from contextlib import nullcontext

from alembic import op

revision: str = "20260830_0035"
down_revision: str | None = "20260830_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """按 ``adjudicated_at`` 有界删除过期评测样本。"""

    postgresql = op.get_bind().dialect.name == "postgresql"
    if postgresql:
        with op.get_context().autocommit_block():
            op.create_index(
                "ix_finding_evaluations_adjudicated_cleanup",
                "finding_evaluations",
                ["adjudicated_at", "finding_id"],
                if_not_exists=True,
                postgresql_concurrently=True,
            )
    else:
        with nullcontext():
            op.create_index(
                "ix_finding_evaluations_adjudicated_cleanup",
                "finding_evaluations",
                ["adjudicated_at", "finding_id"],
            )


def downgrade() -> None:
    """移除评测样本清理索引；数据本身不受影响。"""

    postgresql = op.get_bind().dialect.name == "postgresql"
    if postgresql:
        with op.get_context().autocommit_block():
            op.drop_index(
                "ix_finding_evaluations_adjudicated_cleanup",
                table_name="finding_evaluations",
                if_exists=True,
                postgresql_concurrently=True,
            )
    else:
        with nullcontext():
            op.drop_index(
                "ix_finding_evaluations_adjudicated_cleanup",
                table_name="finding_evaluations",
            )
