"""区分历史代码复查与需要跟随 GitHub 状态的 PR 审查。"""

import sqlalchemy as sa
from alembic import op

revision = "20260914_0060"
down_revision = "20260913_0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("snapshot_review", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("review_runs", "snapshot_review")
