"""保存检索上下文快照身份与 Finding 的关联证据引用。"""

import sqlalchemy as sa
from alembic import op

revision = "20260910_0047"
down_revision = "20260910_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("retrieval_traces") as batch:
        batch.add_column(sa.Column("plan_fingerprint", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_retrieval_trace_plan_agent", ["review_run_id", "plan_fingerprint", "agent"])
    with op.batch_alter_table("review_findings") as batch:
        batch.add_column(sa.Column("context_references", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("review_findings") as batch:
        batch.drop_column("context_references")
    with op.batch_alter_table("retrieval_traces") as batch:
        batch.drop_constraint("uq_retrieval_trace_plan_agent", type_="unique")
        batch.drop_column("plan_fingerprint")
