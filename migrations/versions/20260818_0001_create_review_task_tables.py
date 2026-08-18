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
    """把固定的允许值转换为 CHECK 约束所需的 SQL 片段。

    参数：
        values: 迁移脚本内定义的状态/结论元组，不来自用户输入。

    返回：
        每个值单引号包裹、以逗号分隔的字符串，供 f-string 拼接到约束表达式。

    这里没有实现通用 SQL 转义，因为调用方只传入源码中固定的枚举常量；如果
    允许值改成外部输入，必须改用 SQLAlchemy 的参数化构造方式。
    """
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    """创建审查运行、任务和 Outbox 三张基础表及其约束索引。

    这些表共同保证任务接受的原子性：一个审查请求要么同时拥有运行记录、任务
    记录和事件，要么一条都没有。唯一键和 CHECK 约束把幂等、状态值及正数 ID
    等关键规则下沉到数据库，避免只依赖 API 进程校验。

    创建顺序：
        先建 ``review_runs`` 作为运行主表，再建通过外键关联它的
        ``review_tasks``，最后建保存状态事件的 ``outbox_events``。同时创建查询
        所需索引和唯一约束，保证领取任务、幂等查找和事件去重不会依赖全表扫描。

    该函数由 Alembic 调用，不应在应用启动时手工导入执行；重复执行由 Alembic
    版本表阻止，回滚由 :func:`downgrade` 按依赖逆序处理。
    """
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
    """按外键依赖逆序删除本次迁移创建的索引和表。

    删除顺序是 Outbox、任务、运行：先去掉没有外部引用的事件，再删除引用运行
    的任务，最后删除被任务引用的运行。该操作会删除这次迁移创建的结构和其中
    数据，因此生产发布流程默认只向前迁移，不应把它当作日常“撤销代码”工具。
    """
    op.drop_index("ix_outbox_events_pending", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_review_tasks_claimable", table_name="review_tasks")
    op.drop_table("review_tasks")
    op.drop_index("ix_review_runs_version_created", table_name="review_runs")
    op.drop_table("review_runs")
