"""增加安全错误字段和 GitHub 接入持久化模型。

Revision ID: 20260824_0003
Revises: 20260818_0002
Create Date: 2026-08-24 04:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260824_0003"
down_revision: str | None = "20260818_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


EXTERNAL_ACTION_STATES = ("pending", "running", "succeeded", "failed")
EXECUTION_STATUSES = (
    "queued",
    "waiting_for_ci",
    "running",
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "superseded",
)
REVIEW_CONCLUSIONS = (
    "no_confirmed_findings",
    "findings_present",
    "needs_human",
    "indeterminate",
    "not_applicable",
)
COVERAGE_STATUSES = ("complete", "partial", "unknown", "stale")
WORKER_STATUSES = ("starting", "idle", "busy", "stopping")


def quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


# 0001/0002 把已经带前缀的 CHECK 名称再次交给 ORM 命名约定处理，
# 因此已部署数据库中的约束名称包含重复的表名前缀。
LEGACY_CHECK_CONSTRAINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "review_runs": (
        (
            "ck_review_runs_execution_status_value",
            f"execution_status IN ({quoted(EXECUTION_STATUSES)})",
        ),
        (
            "ck_review_runs_review_conclusion_value",
            "review_conclusion IS NULL OR "
            f"review_conclusion IN ({quoted(REVIEW_CONCLUSIONS)})",
        ),
        (
            "ck_review_runs_coverage_status_value",
            f"coverage_status IN ({quoted(COVERAGE_STATUSES)})",
        ),
        ("ck_review_runs_repository_id_positive", "repository_id > 0"),
        (
            "ck_review_runs_pull_request_number_positive",
            "pull_request_number > 0",
        ),
        ("ck_review_runs_installation_id_positive", "installation_id > 0"),
    ),
    "review_tasks": (
        (
            "ck_review_tasks_execution_status_value",
            f"execution_status IN ({quoted(EXECUTION_STATUSES)})",
        ),
        (
            "ck_review_tasks_attempt_count_nonnegative",
            "attempt_count >= 0",
        ),
        ("ck_review_tasks_max_attempts_positive", "max_attempts > 0"),
    ),
    "outbox_events": (
        (
            "ck_outbox_events_publish_attempts_nonnegative",
            "publish_attempts >= 0",
        ),
    ),
    "worker_heartbeats": (
        (
            "ck_worker_heartbeats_status_value",
            f"status IN ({quoted(WORKER_STATUSES)})",
        ),
    ),
}


def _rename_legacy_check_constraints(*, to_canonical: bool) -> None:
    """重命名历史 CHECK 约束，但不改变约束规则。"""

    connection = op.get_bind()
    dialect_name = connection.dialect.name
    if dialect_name == "postgresql":
        preparer = connection.dialect.identifier_preparer
        for table_name, constraints in LEGACY_CHECK_CONSTRAINTS.items():
            for canonical_name, _ in constraints:
                legacy_name = f"ck_{table_name}_{canonical_name}"
                source_name, target_name = (
                    (legacy_name, canonical_name)
                    if to_canonical
                    else (canonical_name, legacy_name)
                )
                op.execute(
                    sa.text(
                        f"ALTER TABLE {preparer.quote(table_name)} "
                        "RENAME CONSTRAINT "
                        f"{preparer.quote(source_name)} TO "
                        f"{preparer.quote(target_name)}"
                    )
                )
        return

    if dialect_name == "sqlite":
        for table_name, constraints in LEGACY_CHECK_CONSTRAINTS.items():
            with op.batch_alter_table(
                table_name,
                recreate="always",
            ) as batch_op:
                for canonical_name, expression in constraints:
                    legacy_name = f"ck_{table_name}_{canonical_name}"
                    source_name, target_name = (
                        (legacy_name, canonical_name)
                        if to_canonical
                        else (canonical_name, legacy_name)
                    )
                    batch_op.drop_constraint(
                        op.f(source_name),
                        type_="check",
                    )
                    batch_op.create_check_constraint(
                        op.f(target_name),
                        expression,
                    )
        return

    raise RuntimeError(
        "check constraint normalization supports PostgreSQL and SQLite only"
    )


def upgrade() -> None:
    """增加有界任务错误、GitHub 持久化接入和动作审计数据。"""

    _rename_legacy_check_constraints(to_canonical=True)
    op.add_column(
        "review_tasks",
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "review_tasks",
        sa.Column("last_error_retryable", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "review_tasks",
        sa.Column("last_error_details", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_review_runs_created_at",
        "review_runs",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_review_runs_execution_status",
        "review_runs",
        ["execution_status"],
        unique=False,
    )
    op.create_index(
        "ix_review_tasks_expired_lease",
        "review_tasks",
        ["execution_status", "lease_expires_at"],
        unique=False,
    )

    op.create_table(
        "github_installations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "id > 0", name="id_positive"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_github_installations"),
    )
    op.create_index(
        "ix_github_installations_last_seen",
        "github_installations",
        ["last_seen_at"],
        unique=False,
    )

    op.create_table(
        "pull_request_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_version_key", sa.String(length=360), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("repository", sa.String(length=255), nullable=False),
        sa.Column("pull_request_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "installation_id > 0",
            name="installation_id_positive",
        ),
        sa.CheckConstraint(
            "repository_id > 0",
            name="repository_id_positive",
        ),
        sa.CheckConstraint(
            "pull_request_number > 0",
            name="pull_request_number_positive",
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["github_installations.id"],
            name="fk_pr_versions_installation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pull_request_versions"),
        sa.UniqueConstraint(
            "review_version_key",
            name="uq_pull_request_versions_review_version_key",
        ),
    )
    op.create_index(
        "ix_pull_request_versions_repository_pr_seen",
        "pull_request_versions",
        ["repository_id", "pull_request_number", "last_seen_at"],
        unique=False,
    )

    op.create_table(
        "github_webhook_deliveries",
        sa.Column("delivery_id", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("pull_request_version_id", sa.String(length=36), nullable=False),
        sa.Column("review_run_id", sa.String(length=36), nullable=False),
        sa.Column("review_task_id", sa.String(length=36), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["github_installations.id"],
            name="fk_webhook_installation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pull_request_version_id"],
            ["pull_request_versions.id"],
            name="fk_webhook_pr_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_run_id"],
            ["review_runs.id"],
            name="fk_webhook_review_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_task_id"],
            ["review_tasks.id"],
            name="fk_webhook_review_task",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "delivery_id", name="pk_github_webhook_deliveries"
        ),
    )
    op.create_index(
        "ix_github_webhook_deliveries_received",
        "github_webhook_deliveries",
        ["received_at"],
        unique=False,
    )

    op.create_table(
        "external_actions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("action_key", sa.String(length=300), nullable=False),
        sa.Column("review_run_id", sa.String(length=36), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("remote_id", sa.String(length=200), nullable=True),
        sa.Column(
            "attempt_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("request_method", sa.String(length=10), nullable=True),
        sa.Column("request_path", sa.String(length=1000), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("github_request_id", sa.String(length=200), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("rate_limit_remaining", sa.Integer(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_retryable", sa.Boolean(), nullable=True),
        sa.Column("last_error_details", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"state IN ({quoted(EXTERNAL_ACTION_STATES)})",
            name="state_value",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="duration_ms_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["review_run_id"],
            ["review_runs.id"],
            name="fk_external_actions_review_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_external_actions"),
        sa.UniqueConstraint(
            "action_key", name="uq_external_actions_action_key"
        ),
    )
    op.create_index(
        "ix_external_actions_run_state",
        "external_actions",
        ["review_run_id", "state"],
        unique=False,
    )


def downgrade() -> None:
    """移除 GitHub 接入状态和结构化任务错误字段。"""

    op.drop_index("ix_external_actions_run_state", table_name="external_actions")
    op.drop_table("external_actions")
    op.drop_index(
        "ix_github_webhook_deliveries_received",
        table_name="github_webhook_deliveries",
    )
    op.drop_table("github_webhook_deliveries")
    op.drop_index(
        "ix_pull_request_versions_repository_pr_seen",
        table_name="pull_request_versions",
    )
    op.drop_table("pull_request_versions")
    op.drop_index(
        "ix_github_installations_last_seen", table_name="github_installations"
    )
    op.drop_table("github_installations")
    op.drop_index(
        "ix_review_tasks_expired_lease", table_name="review_tasks"
    )
    op.drop_index(
        "ix_review_runs_execution_status", table_name="review_runs"
    )
    op.drop_index("ix_review_runs_created_at", table_name="review_runs")
    op.drop_column("review_tasks", "last_error_details")
    op.drop_column("review_tasks", "last_error_retryable")
    op.drop_column("review_tasks", "last_error_code")
    _rename_legacy_check_constraints(to_canonical=False)
