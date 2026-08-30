"""拆分 Finding 的机器定位校验与人工裁决。

Revision ID: 20260828_0026
Revises: 20260828_0025
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0026"
down_revision: str | None = "20260828_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存独立人工标签，并恢复被旧人工操作覆盖的机器定位状态。"""

    postgresql = op.get_bind().dialect.name == "postgresql"
    constraint_options = {"postgresql_not_valid": True} if postgresql else {}
    with op.batch_alter_table("review_findings") as batch:
        batch.add_column(
            sa.Column(
                "adjudication_status",
                sa.String(length=24),
                nullable=False,
                server_default="unreviewed",
            )
        )
        batch.create_check_constraint(
            "adjudication_status_value",
            "adjudication_status IN ("
            "'unreviewed', 'valid', 'false_positive', 'duplicate', "
            "'out_of_scope', 'known_issue')",
            **constraint_options,
        )

    with op.batch_alter_table("review_findings") as batch:
        batch.alter_column(
            "adjudication_status",
            existing_type=sa.String(length=24),
            server_default=None,
        )

    if postgresql:
        with op.get_context().autocommit_block():
            op.create_index(
                "ix_review_findings_run_adjudication",
                "review_findings",
                ["review_run_id", "adjudication_status"],
                postgresql_concurrently=True,
                if_not_exists=True,
            )
    else:
        op.create_index(
            "ix_review_findings_run_adjudication",
            "review_findings",
            ["review_run_id", "adjudication_status"],
        )

    # 旧 verification_status 到人工裁决和机器定位状态的转换由维护命令分批完成。

    with op.batch_alter_table("finding_evaluations") as batch:
        batch.drop_constraint("verdict_value", type_="check")
        batch.create_check_constraint(
            "verdict_value",
            "verdict IN ("
            "'valid', 'false_positive', 'duplicate', 'out_of_scope', "
            "'known_issue')",
        )


def downgrade() -> None:
    """只在数据已分批准备完成后移除独立裁决字段。"""

    if op.get_context().as_sql:
        raise RuntimeError(
            "Finding 0026 不支持离线直接降级；先在线运行分批准备命令"
        )
    bind = op.get_bind()
    evaluations = sa.table(
        "finding_evaluations",
        sa.column("verdict", sa.String()),
    )
    findings = sa.table(
        "review_findings",
        sa.column("adjudication_status", sa.String()),
        sa.column("verification_status", sa.String()),
    )
    incompatible_evaluation = bind.scalar(
        sa.select(sa.literal(1))
        .select_from(evaluations)
        .where(
            evaluations.c.verdict.in_(
                ("duplicate", "out_of_scope", "known_issue")
            )
        )
        .limit(1)
    )
    incompatible_finding = bind.scalar(
        sa.select(sa.literal(1))
        .select_from(findings)
        .where(
            sa.or_(
                sa.and_(
                    findings.c.adjudication_status == "valid",
                    findings.c.verification_status != "verified",
                ),
                sa.and_(
                    findings.c.adjudication_status.in_(
                        (
                            "false_positive",
                            "duplicate",
                            "out_of_scope",
                            "known_issue",
                        )
                    ),
                    findings.c.verification_status != "rejected",
                ),
            )
        )
        .limit(1)
    )
    if incompatible_evaluation is not None or incompatible_finding is not None:
        raise RuntimeError(
            "Finding 裁决数据尚未准备完成；请在停机窗口重复运行 "
            "python -m apps.maintenance.prepare_finding_downgrade，"
            "直到 complete=true 后再降级"
        )

    with op.batch_alter_table("finding_evaluations") as batch:
        batch.drop_constraint("verdict_value", type_="check")
        batch.create_check_constraint(
            "verdict_value",
            "verdict IN ('valid', 'false_positive')",
        )

    postgresql = op.get_bind().dialect.name == "postgresql"
    if postgresql:
        with op.get_context().autocommit_block():
            op.drop_index(
                "ix_review_findings_run_adjudication",
                table_name="review_findings",
                postgresql_concurrently=True,
                if_exists=True,
            )
    else:
        op.drop_index(
            "ix_review_findings_run_adjudication",
            table_name="review_findings",
        )
    with op.batch_alter_table("review_findings") as batch:
        batch.drop_constraint("adjudication_status_value", type_="check")
        batch.drop_column("adjudication_status")
