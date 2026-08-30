"""为外部动作审计增加短租约，避免多副本重复副作用。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0036"
down_revision: str | None = "20260830_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加动作领取者和租约截止时间。"""

    with op.batch_alter_table("external_actions") as batch:
        batch.add_column(sa.Column("lease_owner", sa.String(length=200)))
        batch.add_column(
            sa.Column("lease_expires_at", sa.DateTime(timezone=True))
        )
        batch.create_check_constraint(
            "lease_shape",
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
        )
    op.create_index(
        "ix_external_actions_claimable",
        "external_actions",
        ["state", "lease_expires_at", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    """移除外部动作租约字段。"""

    op.drop_index("ix_external_actions_claimable", table_name="external_actions")
    with op.batch_alter_table("external_actions") as batch:
        batch.drop_constraint("lease_shape", type_="check")
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_owner")
