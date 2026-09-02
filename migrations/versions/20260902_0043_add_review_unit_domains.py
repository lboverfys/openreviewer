"""为 Review Unit 保存确定性的 Agent 职责范围。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0043"
down_revision: str | None = "20260831_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增职责 JSON 列，并为历史计划回填完整三路范围。"""

    connection = op.get_bind()
    recreate = "always" if connection.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table("review_units", recreate=recreate) as batch:
        batch.add_column(sa.Column("review_domains", sa.JSON(), nullable=True))

    # 历史 Unit 没有职责信息，必须按完整三路回填，不能因为升级而静默
    # 漏掉原本可能由任一路发现的问题。
    connection.execute(
        sa.text(
            "UPDATE review_units "
            "SET review_domains = '[\"security\",\"convention\",\"logic\"]' "
            "WHERE review_domains IS NULL"
        )
    )
    with op.batch_alter_table("review_units", recreate=recreate) as batch:
        batch.alter_column(
            "review_domains",
            existing_type=sa.JSON(),
            existing_nullable=True,
            nullable=False,
        )


def downgrade() -> None:
    """移除职责范围列；历史 Unit 的其他身份保持不变。"""

    connection = op.get_bind()
    recreate = "always" if connection.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table("review_units", recreate=recreate) as batch:
        batch.drop_column("review_domains")
