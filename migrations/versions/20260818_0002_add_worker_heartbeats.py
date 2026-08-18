"""增加 Worker 心跳表。

Revision ID: 20260818_0002
Revises: 20260818_0001
Create Date: 2026-08-18 20:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260818_0002"
down_revision: str | None = "20260818_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


WORKER_STATUSES = ("starting", "idle", "busy", "stopping")


def quoted(values: tuple[str, ...]) -> str:
    """把固定 Worker 状态转换为 SQL CHECK 约束片段。

    参数：
        values: 源码中声明的 ``starting/idle/busy/stopping`` 状态元组。

    返回：
        可嵌入 ``status IN (...)`` 表达式的带引号、逗号分隔字符串。

    仅供迁移生成静态 SQL 使用，不接受来自请求或环境变量的任意文本。
    """
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    """创建持久化 Worker 心跳表和最近心跳索引。

    表以 ``worker_id`` 为主键，保存当前状态、可选任务、首次写入时间和最近
    心跳时间；任务外键使用 ``SET NULL``，任务删除后不会留下指向不存在任务的
    当前任务 ID。``last_seen_at`` 索引供 Dashboard/健康检查快速找到最新心跳。

    该迁移依赖上一版已经存在的 ``review_tasks`` 表，由 Alembic 按
    ``down_revision`` 顺序执行。
    """
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_task_id", sa.String(length=36), nullable=True),
        sa.Column(
            "started_at",
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
            f"status IN ({quoted(WORKER_STATUSES)})",
            name="ck_worker_heartbeats_status_value",
        ),
        sa.ForeignKeyConstraint(
            ["current_task_id"],
            ["review_tasks.id"],
            name="fk_worker_heartbeats_current_task_id_review_tasks",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("worker_id", name="pk_worker_heartbeats"),
    )
    op.create_index(
        "ix_worker_heartbeats_last_seen",
        "worker_heartbeats",
        ["last_seen_at"],
        unique=False,
    )


def downgrade() -> None:
    """删除 Worker 心跳索引和表，供明确的迁移回滚使用。

    先删除索引再删除表；这会丢失所有 Worker 的历史心跳，但不会删除审查运行
    或任务记录。生产部署通常不自动执行此函数。

    该函数由 Alembic 版本回退调用，不返回业务数据，也不会停止正在运行的 Worker。
    """
    op.drop_index(
        "ix_worker_heartbeats_last_seen",
        table_name="worker_heartbeats",
    )
    op.drop_table("worker_heartbeats")
