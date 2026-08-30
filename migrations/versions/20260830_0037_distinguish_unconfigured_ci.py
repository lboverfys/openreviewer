"""区分未配置 CI 与无法确认完整的 CI 快照。

Revision ID: 20260830_0037
Revises: 20260830_0036
Create Date: 2026-08-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260830_0037"
down_revision: str | None = "20260830_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_CI_STATES = ("not_configured", "unknown", "pending", "success", "failure")
_OLD_CI_STATES = ("unknown", "pending", "success", "failure")


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _replace_ci_state_constraint(states: tuple[str, ...]) -> None:
    """在 PostgreSQL/SQLite 上替换现有 CI 状态约束。"""

    connection = op.get_bind()
    constraint_name = op.f("ck_pull_request_versions_ci_state_value")
    expression = "ci_state IS NULL OR " f"ci_state IN ({_quoted(states)})"
    if connection.dialect.name == "postgresql":
        op.drop_constraint(
            constraint_name,
            "pull_request_versions",
            type_="check",
        )
        op.create_check_constraint(
            constraint_name,
            "pull_request_versions",
            expression,
        )
        return
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(
            "pull_request_versions",
            recreate="always",
        ) as batch_op:
            batch_op.drop_constraint(constraint_name, type_="check")
            batch_op.create_check_constraint(constraint_name, expression)
        return
    raise RuntimeError("CI state migration supports PostgreSQL and SQLite only")


def upgrade() -> None:
    """允许持久化没有配置 CI 门禁的完整快照。"""

    _replace_ci_state_constraint(_NEW_CI_STATES)


def downgrade() -> None:
    """恢复旧 CI 状态约束；调用方需先清理新状态记录。"""

    connection = op.get_bind()
    if connection.dialect.name in {"postgresql", "sqlite"}:
        # 旧版本无法解释新状态，先把它归一化为 unknown，避免收紧约束失败。
        op.execute(
            "UPDATE pull_request_versions "
            "SET ci_state = 'unknown' "
            "WHERE ci_state = 'not_configured'"
        )
    _replace_ci_state_constraint(_OLD_CI_STATES)
