"""增加 PR 上下文快照和 CI 生命周期。

Revision ID: 20260824_0004
Revises: 20260824_0003
Create Date: 2026-08-24 12:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260824_0004"
down_revision: str | None = "20260824_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_EXECUTION_STATUSES = (
    "queued",
    "waiting_for_ci",
    "running",
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "superseded",
)
EXECUTION_STATUSES = (
    "queued",
    "waiting_for_ci",
    "running",
    "ready_for_review",
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "superseded",
)
PR_STATES = ("open", "closed")
CI_STATES = ("unknown", "pending", "success", "failure")
FILE_STATUSES = (
    "added",
    "removed",
    "modified",
    "renamed",
    "copied",
    "changed",
    "unchanged",
)
PATCH_STATES = ("available", "binary", "missing", "too_large")
CI_CHECK_KINDS = ("check_run", "commit_status")


def quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _replace_execution_status_constraints(
    statuses: tuple[str, ...],
) -> None:
    """替换运行和任务的状态约束，保持两张表允许值完全一致。"""

    expression = f"execution_status IN ({quoted(statuses)})"
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        for table_name in ("review_runs", "review_tasks"):
            op.drop_constraint(
                f"ck_{table_name}_execution_status_value",
                table_name,
                type_="check",
            )
            op.create_check_constraint(
                f"ck_{table_name}_execution_status_value",
                table_name,
                expression,
            )
        return
    if connection.dialect.name == "sqlite":
        for table_name in ("review_runs", "review_tasks"):
            with op.batch_alter_table(table_name, recreate="always") as batch_op:
                batch_op.drop_constraint(
                    op.f(f"ck_{table_name}_execution_status_value"),
                    type_="check",
                )
                batch_op.create_check_constraint(
                    op.f(f"ck_{table_name}_execution_status_value"),
                    expression,
                )
        return
    raise RuntimeError("PR context migration supports PostgreSQL and SQLite only")


def upgrade() -> None:
    """增加 GitHub 上下文、CI 快照、轮询期限和可审查状态。"""

    _replace_execution_status_constraints(EXECUTION_STATUSES)
    op.create_index(
        "ix_review_runs_repository_pr_status",
        "review_runs",
        ["repository_id", "pull_request_number", "execution_status"],
        unique=False,
    )
    with op.batch_alter_table("review_tasks") as batch_op:
        batch_op.add_column(
            sa.Column("ci_wait_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("ci_deadline_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "ci_poll_count",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_review_tasks_ci_poll_count_nonnegative"),
            "ci_poll_count >= 0",
        )

    with op.batch_alter_table("pull_request_versions") as batch_op:
        batch_op.add_column(sa.Column("base_sha", sa.String(length=64)))
        batch_op.add_column(sa.Column("pr_state", sa.String(length=16)))
        batch_op.add_column(sa.Column("is_draft", sa.Boolean()))
        batch_op.add_column(sa.Column("title", sa.String(length=1000)))
        batch_op.add_column(sa.Column("changed_files_count", sa.Integer()))
        batch_op.add_column(sa.Column("files_complete", sa.Boolean()))
        batch_op.add_column(sa.Column("diff_complete", sa.Boolean()))
        batch_op.add_column(
            sa.Column("context_fetched_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(sa.Column("pr_updated_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("ci_state", sa.String(length=32)))
        batch_op.add_column(sa.Column("ci_checks_complete", sa.Boolean()))
        batch_op.add_column(sa.Column("ci_checked_at", sa.DateTime(timezone=True)))
        batch_op.create_check_constraint(
            op.f("ck_pull_request_versions_changed_files_count_nonnegative"),
            "changed_files_count IS NULL OR changed_files_count >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_pull_request_versions_pr_state_value"),
            "pr_state IS NULL OR " f"pr_state IN ({quoted(PR_STATES)})",
        )
        batch_op.create_check_constraint(
            op.f("ck_pull_request_versions_ci_state_value"),
            "ci_state IS NULL OR " f"ci_state IN ({quoted(CI_STATES)})",
        )

    op.create_table(
        "pull_request_files",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("pull_request_version_id", sa.String(length=36), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("previous_path", sa.String(length=1024)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("blob_sha", sa.String(length=64), nullable=False),
        sa.Column("additions", sa.Integer(), nullable=False),
        sa.Column("deletions", sa.Integer(), nullable=False),
        sa.Column("changes", sa.Integer(), nullable=False),
        sa.Column("patch_state", sa.String(length=32), nullable=False),
        sa.Column("patch", sa.Text()),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"status IN ({quoted(FILE_STATUSES)})",
            name="status_value",
        ),
        sa.CheckConstraint(
            f"patch_state IN ({quoted(PATCH_STATES)})",
            name="patch_state_value",
        ),
        sa.CheckConstraint(
            "additions >= 0", name="additions_nonnegative"
        ),
        sa.CheckConstraint(
            "deletions >= 0", name="deletions_nonnegative"
        ),
        sa.CheckConstraint(
            "changes >= 0", name="changes_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["pull_request_version_id"],
            ["pull_request_versions.id"],
            name="fk_pr_files_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pull_request_files"),
        sa.UniqueConstraint(
            "pull_request_version_id",
            "path",
            name="uq_pull_request_files_pull_request_version_id",
        ),
    )
    op.create_table(
        "pull_request_ci_checks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("pull_request_version_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("external_key", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("conclusion", sa.String(length=50)),
        sa.Column("app_id", sa.BigInteger()),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"kind IN ({quoted(CI_CHECK_KINDS)})",
            name="kind_value",
        ),
        sa.ForeignKeyConstraint(
            ["pull_request_version_id"],
            ["pull_request_versions.id"],
            name="fk_pr_ci_checks_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pull_request_ci_checks"),
        sa.UniqueConstraint(
            "pull_request_version_id",
            "kind",
            "external_key",
            name="uq_pull_request_ci_checks_pull_request_version_id",
        ),
    )

def downgrade() -> None:
    """移除 PR 上下文与 CI 状态，并恢复上一版执行状态约束。"""

    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE review_tasks SET execution_status = 'waiting_for_ci' "
            "WHERE execution_status = 'ready_for_review'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE review_runs SET execution_status = 'waiting_for_ci' "
            "WHERE execution_status = 'ready_for_review'"
        )
    )
    op.drop_table("pull_request_ci_checks")
    op.drop_table("pull_request_files")

    with op.batch_alter_table("pull_request_versions") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_pull_request_versions_ci_state_value"), type_="check"
        )
        batch_op.drop_constraint(
            op.f("ck_pull_request_versions_pr_state_value"), type_="check"
        )
        batch_op.drop_constraint(
            op.f("ck_pull_request_versions_changed_files_count_nonnegative"),
            type_="check",
        )
        for column_name in (
            "ci_checked_at",
            "ci_checks_complete",
            "ci_state",
            "pr_updated_at",
            "context_fetched_at",
            "diff_complete",
            "files_complete",
            "changed_files_count",
            "title",
            "is_draft",
            "pr_state",
            "base_sha",
        ):
            batch_op.drop_column(column_name)

    with op.batch_alter_table("review_tasks") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_review_tasks_ci_poll_count_nonnegative"), type_="check"
        )
        batch_op.drop_column("ci_poll_count")
        batch_op.drop_column("ci_deadline_at")
        batch_op.drop_column("ci_wait_started_at")
    op.drop_index("ix_review_runs_repository_pr_status", table_name="review_runs")
    _replace_execution_status_constraints(OLD_EXECUTION_STATUSES)
