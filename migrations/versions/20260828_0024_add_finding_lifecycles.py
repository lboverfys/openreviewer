"""增加跨提交 Finding 生命周期跟踪。

Revision ID: 20260828_0024
Revises: 20260828_0023
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0024"
down_revision: str | None = "20260828_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立 PR 级稳定指纹状态，并为每轮 Finding 记录出现方式。"""

    postgresql = op.get_bind().dialect.name == "postgresql"
    constraint_options = {"postgresql_not_valid": True} if postgresql else {}
    op.create_table(
        "finding_lifecycles",
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("pull_request_number", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("first_seen_review_run_id", sa.String(length=36), nullable=False),
        sa.Column("last_seen_review_run_id", sa.String(length=36), nullable=False),
        sa.Column("previous_seen_review_run_id", sa.String(length=36)),
        sa.Column("fixed_by_review_run_id", sa.String(length=36)),
        sa.Column("first_seen_head_sha", sa.String(length=64), nullable=False),
        sa.Column("last_seen_head_sha", sa.String(length=64), nullable=False),
        sa.Column("last_occurrence_status", sa.String(length=20), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fixed_at", sa.DateTime(timezone=True)),
        sa.Column("historical_backfilled_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('present', 'fixed')",
            name=op.f("ck_finding_lifecycles_state_value"),
        ),
        sa.CheckConstraint(
            "last_occurrence_status IN ('new', 'still_present', 'reintroduced')",
            name=op.f("ck_finding_lifecycles_last_occurrence_status_value"),
        ),
        sa.CheckConstraint(
            "occurrence_count > 0",
            name=op.f("ck_finding_lifecycles_occurrence_count_positive"),
        ),
        sa.PrimaryKeyConstraint(
            "repository_id",
            "pull_request_number",
            "fingerprint",
            name=op.f("pk_finding_lifecycles"),
        ),
    )
    op.create_index(
        "ix_finding_lifecycles_pr_state",
        "finding_lifecycles",
        ["repository_id", "pull_request_number", "state"],
    )
    op.create_index(
        "ix_finding_lifecycles_fixed_run",
        "finding_lifecycles",
        ["fixed_by_review_run_id"],
    )

    with op.batch_alter_table("review_findings") as batch:
        batch.add_column(
            sa.Column(
                "lifecycle_status",
                sa.String(length=20),
                nullable=False,
                server_default="new",
            )
        )
        batch.add_column(
            sa.Column(
                "occurrence_count",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )
        batch.add_column(sa.Column("previous_review_run_id", sa.String(length=36)))
        batch.create_check_constraint(
            "lifecycle_status_value",
            "lifecycle_status IN ('new', 'still_present', 'reintroduced')",
            **constraint_options,
        )
        batch.create_check_constraint(
            "occurrence_count_positive",
            "occurrence_count > 0",
            **constraint_options,
        )

    # 历史数据由显式维护命令按稳定指纹分批回填。生产迁移只做快速结构变更，
    # 不在 Alembic 事务中对 review_findings 执行无界窗口查询。

    with op.batch_alter_table("review_findings") as batch:
        batch.alter_column(
            "lifecycle_status",
            existing_type=sa.String(length=20),
            server_default=None,
        )
        batch.alter_column(
            "occurrence_count",
            existing_type=sa.Integer(),
            server_default=None,
        )


def downgrade() -> None:
    """移除生命周期派生数据，不改变原有 Finding 内容。"""

    with op.batch_alter_table("review_findings") as batch:
        batch.drop_constraint("occurrence_count_positive", type_="check")
        batch.drop_constraint("lifecycle_status_value", type_="check")
        batch.drop_column("previous_review_run_id")
        batch.drop_column("occurrence_count")
        batch.drop_column("lifecycle_status")
    op.drop_index(
        "ix_finding_lifecycles_fixed_run",
        table_name="finding_lifecycles",
    )
    op.drop_index(
        "ix_finding_lifecycles_pr_state",
        table_name="finding_lifecycles",
    )
    op.drop_table("finding_lifecycles")
