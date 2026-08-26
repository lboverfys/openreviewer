"""增加 Review Plan 持久化与任务原阶段租约。

Revision ID: 20260825_0005
Revises: 20260824_0004
Create Date: 2026-08-25 10:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260825_0005"
down_revision: str | None = "20260824_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CLAIMABLE_STATUSES = ("queued", "waiting_for_ci", "ready_for_review")
FILE_DECISIONS = (
    "planned",
    "binary",
    "generated",
    "unsupported",
    "patch_missing",
    "patch_too_large",
    "rules_incomplete",
    "omitted_by_budget",
)


def quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    """创建计划快照四表，并让运行中任务记住领取前阶段。"""

    with op.batch_alter_table("review_tasks") as batch_op:
        batch_op.add_column(
            sa.Column("claimed_from_status", sa.String(length=32), nullable=True)
        )
        batch_op.create_check_constraint(
            op.f("ck_review_tasks_claimed_from_status_value"),
            "claimed_from_status IS NULL OR "
            f"claimed_from_status IN ({quoted(CLAIMABLE_STATUSES)})",
        )

    # 迁移时无法可靠推断旧 running 任务是否来自 CI 轮询；按历史行为回到 queued。
    op.execute(
        sa.text(
            "UPDATE review_tasks SET claimed_from_status = 'queued' "
            "WHERE execution_status = 'running' AND claimed_from_status IS NULL"
        )
    )

    op.create_table(
        "review_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_run_id", sa.String(length=36), nullable=False),
        sa.Column("pull_request_version_id", sa.String(length=36), nullable=False),
        sa.Column("review_version_key", sa.String(length=360), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("plan_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("planner_version", sa.String(length=50), nullable=False),
        sa.Column("rules_complete", sa.Boolean(), nullable=False),
        sa.Column("incomplete_files", sa.JSON(), nullable=False),
        sa.Column("rule_issues", sa.JSON(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("requested_candidate_count", sa.Integer(), nullable=False),
        sa.Column("rule_count", sa.Integer(), nullable=False),
        sa.Column("unit_count", sa.Integer(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("total_estimated_input_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "candidate_count >= 0", name="candidate_count_nonnegative"
        ),
        sa.CheckConstraint(
            "requested_candidate_count >= 0",
            name="requested_candidate_count_nonnegative",
        ),
        sa.CheckConstraint("rule_count >= 0", name="rule_count_nonnegative"),
        sa.CheckConstraint("unit_count >= 0", name="unit_count_nonnegative"),
        sa.CheckConstraint("file_count >= 0", name="file_count_nonnegative"),
        sa.CheckConstraint(
            "total_estimated_input_bytes >= 0",
            name="total_estimated_input_bytes_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["review_run_id"],
            ["review_runs.id"],
            name="fk_review_plans_review_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pull_request_version_id"],
            ["pull_request_versions.id"],
            name="fk_review_plans_pr_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_plans"),
        sa.UniqueConstraint(
            "review_run_id", name="uq_review_plans_review_run_id"
        ),
    )
    op.create_index(
        "ix_review_plans_version_fingerprint",
        "review_plans",
        ["review_version_key", "plan_fingerprint"],
        unique=False,
    )

    op.create_table(
        "review_plan_rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_plan_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("scope", sa.String(length=1014)),
        sa.Column("blob_sha", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        sa.CheckConstraint("byte_size > 0", name="byte_size_positive"),
        sa.ForeignKeyConstraint(
            ["review_plan_id"],
            ["review_plans.id"],
            name="fk_review_plan_rules_plan",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_plan_rules"),
        sa.UniqueConstraint(
            "review_plan_id",
            "path",
            name="uq_review_plan_rules_plan_path",
        ),
        sa.UniqueConstraint(
            "review_plan_id",
            "ordinal",
            name="uq_review_plan_rules_plan_ordinal",
        ),
    )

    op.create_table(
        "review_units",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_plan_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("unit_key", sa.String(length=64), nullable=False),
        sa.Column("file", sa.String(length=1024), nullable=False),
        sa.Column("blob_sha", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=50), nullable=False),
        sa.Column("patch", sa.Text(), nullable=False),
        sa.Column("patch_sha256", sa.String(length=64), nullable=False),
        sa.Column("rule_paths", sa.JSON(), nullable=False),
        sa.Column("estimated_input_bytes", sa.Integer(), nullable=False),
        sa.Column("planner_version", sa.String(length=50), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        sa.CheckConstraint(
            "estimated_input_bytes > 0", name="estimated_input_bytes_positive"
        ),
        sa.ForeignKeyConstraint(
            ["review_plan_id"],
            ["review_plans.id"],
            name="fk_review_units_plan",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_units"),
        sa.UniqueConstraint(
            "review_plan_id",
            "unit_key",
            name="uq_review_units_plan_unit_key",
        ),
        sa.UniqueConstraint(
            "review_plan_id",
            "file",
            name="uq_review_units_plan_file",
        ),
        sa.UniqueConstraint(
            "review_plan_id",
            "ordinal",
            name="uq_review_units_plan_ordinal",
        ),
    )

    op.create_table(
        "review_file_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_plan_id", sa.String(length=36), nullable=False),
        sa.Column("review_unit_id", sa.String(length=36)),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("file", sa.String(length=1024), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        sa.CheckConstraint(
            f"decision IN ({quoted(FILE_DECISIONS)})",
            name="decision_value",
        ),
        sa.CheckConstraint(
            "(decision = 'planned' AND review_unit_id IS NOT NULL) OR "
            "(decision <> 'planned' AND review_unit_id IS NULL)",
            name="decision_unit_consistency",
        ),
        sa.ForeignKeyConstraint(
            ["review_plan_id"],
            ["review_plans.id"],
            name="fk_review_file_plans_plan",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["review_unit_id"],
            ["review_units.id"],
            name="fk_review_file_plans_unit",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_file_plans"),
        sa.UniqueConstraint(
            "review_plan_id",
            "file",
            name="uq_review_file_plans_plan_file",
        ),
        sa.UniqueConstraint(
            "review_plan_id",
            "ordinal",
            name="uq_review_file_plans_plan_ordinal",
        ),
        sa.UniqueConstraint(
            "review_unit_id", name="uq_review_file_plans_review_unit_id"
        ),
    )


def downgrade() -> None:
    """移除计划快照及领取前阶段字段。"""

    op.drop_table("review_file_plans")
    op.drop_table("review_units")
    op.drop_table("review_plan_rules")
    op.drop_index("ix_review_plans_version_fingerprint", table_name="review_plans")
    op.drop_table("review_plans")

    with op.batch_alter_table("review_tasks") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_review_tasks_claimed_from_status_value"), type_="check"
        )
        batch_op.drop_column("claimed_from_status")
