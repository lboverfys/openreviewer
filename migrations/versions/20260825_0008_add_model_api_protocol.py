"""增加模型 API 协议选择与调用审计。

Revision ID: 20260825_0008
Revises: 20260825_0007
Create Date: 2026-08-25 22:30:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260825_0008"
down_revision: str | None = "20260825_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PROTOCOL_CHECK = (
    "(provider = 'openai' AND api_protocol IN "
    "('responses', 'chat_completions')) OR "
    "(provider = 'anthropic' AND api_protocol = 'messages')"
)


def upgrade() -> None:
    """保留旧调用语义，并允许 OpenAI 配置切换到 Chat Completions。"""

    for table_name in ("ai_provider_configs", "model_calls"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "api_protocol",
                    sa.String(length=32),
                    nullable=False,
                    server_default="responses",
                )
            )
        op.execute(
            sa.text(
                f"UPDATE {table_name} SET api_protocol = 'messages' "
                "WHERE provider = 'anthropic'"
            )
        )
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column("api_protocol", server_default=None)
            batch_op.create_check_constraint(
                "api_protocol_provider",
                _PROTOCOL_CHECK,
            )


def downgrade() -> None:
    """删除协议字段；OpenAI 调用恢复为固定 Responses 语义。"""

    for table_name in ("model_calls", "ai_provider_configs"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(
                "api_protocol_provider",
                type_="check",
            )
            batch_op.drop_column("api_protocol")
