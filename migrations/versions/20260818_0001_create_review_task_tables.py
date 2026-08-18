"""创建审查运行、任务和 Outbox 表。

Revision ID: 20260818_0001
Revises:
Create Date: 2026-08-18 18:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260818_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "review_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_version_key", sa.String(length=360), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("repository", sa.String(length=255), nullable=False),
        sa.Column("pull_request_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column(
            "execution_status",
            sa.String(length=32),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("review_conclusion", sa.String(length=32), nullable=True),
        sa.Column(
            "coverage_status",
            sa.String(length=32),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
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
        sa.CheckConstraint(
            f"execution_status IN ({quoted(EXECUTION_STATUSES)})",
            name="ck_review_runs_execution_status_value",
        ),
        sa.CheckConstraint(
            "review_conclusion IS NULL OR "
            f"review_conclusion IN ({quoted(REVIEW_CONCLUSIONS)})",
            name="ck_review_runs_review_conclusion_value",
        ),
        sa.CheckConstraint(
            f"coverage_status IN ({quoted(COVERAGE_STATUSES)})",
            name="ck_review_runs_coverage_status_value",
        ),
        sa.CheckConstraint(
            "repository_id > 0",
            name="ck_review_runs_repository_id_positive",
        ),
        sa.CheckConstraint(
            "pull_request_number > 0",
            name="ck_review_runs_pull_request_number_positive",
        ),
        sa.CheckConstraint(
            "installation_id > 0",
            name="ck_review_runs_installation_id_positive",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_runs"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_review_runs_idempotency_key",
        ),
    )
    op.create_index(
        "ix_review_runs_version_created",
        "review_runs",
        ["review_version_key", "created_at"],
        unique=False,
    )

    op.create_table(
        "review_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_run_id", sa.String(length=36), nullable=False),
        sa.Column(
            "execution_status",
            sa.String(length=32),
            server_default="queued",
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.SmallInteger(),
            server_default="100",
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default="3",
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            f"execution_status IN ({quoted(EXECUTION_STATUSES)})",
            name="ck_review_tasks_execution_status_value",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_review_tasks_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name="ck_review_tasks_max_attempts_positive",
        ),
        sa.ForeignKeyConstraint(
            ["review_run_id"],
            ["review_runs.id"],
            name="fk_review_tasks_review_run_id_review_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_tasks"),
        sa.UniqueConstraint(
            "review_run_id",
            name="uq_review_tasks_review_run_id",
        ),
    )
    op.create_index(
        "ix_review_tasks_claimable",
        "review_tasks",
        ["execution_status", "available_at", "priority"],
        unique=False,
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_key", sa.String(length=200), nullable=False),
        sa.Column("aggregate_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "publish_attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.CheckConstraint(
            "publish_attempts >= 0",
            name="ck_outbox_events_publish_attempts_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.UniqueConstraint(
            "event_key",
            name="uq_outbox_events_event_key",
        ),
    )
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["published_at", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_pending", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_review_tasks_claimable", table_name="review_tasks")
    op.drop_table("review_tasks")
    op.drop_index("ix_review_runs_version_created", table_name="review_runs")
    op.drop_table("review_runs")
