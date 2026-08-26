"""增加安全、规范、逻辑和汇总 Agent 的独立配置。

Revision ID: 20260827_0015
Revises: 20260827_0014
Create Date: 2026-08-27 12:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0015"
down_revision: str | None = "20260827_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 Agent 配置和密钥隔离表。"""

    op.create_table(
        "ai_agent_configs",
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("api_protocol", sa.String(length=32), nullable=False),
        sa.Column("api_base_url", sa.String(length=500)),
        sa.Column("reasoning_effort", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("context_window_tokens", sa.Integer(), nullable=False, server_default=sa.text("128000")),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False, server_default=sa.text("8192")),
        sa.Column("max_batch_input_tokens", sa.Integer(), nullable=False, server_default=sa.text("64000")),
        sa.Column("connect_timeout_seconds", sa.Float(), nullable=False, server_default=sa.text("5")),
        sa.Column("read_timeout_seconds", sa.Float(), nullable=False, server_default=sa.text("180")),
        sa.Column("write_timeout_seconds", sa.Float(), nullable=False, server_default=sa.text("30")),
        sa.Column("pool_timeout_seconds", sa.Float(), nullable=False, server_default=sa.text("5")),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default=sa.text("2")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("test_status", sa.String(length=20)),
        sa.Column("tested_configuration_fingerprint", sa.String(length=64)),
        sa.Column("tested_at", sa.DateTime(timezone=True)),
        sa.Column("updated_by", sa.String(length=100), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "agent IN ('security', 'convention', 'logic', 'summary')",
            name="agent_value",
        ),
        sa.CheckConstraint(
            "provider IN ('openai', 'anthropic')",
            name="provider_value",
        ),
        sa.CheckConstraint(
            "(provider = 'openai' AND api_protocol IN ('responses', 'chat_completions')) "
            "OR (provider = 'anthropic' AND api_protocol = 'messages')",
            name="api_protocol_provider",
        ),
        sa.CheckConstraint(
            "reasoning_effort IN ('none', 'low', 'medium', 'high', 'max')",
            name="reasoning_effort_value",
        ),
        sa.CheckConstraint(
            "context_window_tokens BETWEEN 8192 AND 4000000",
            name="context_window_tokens_range",
        ),
        sa.CheckConstraint(
            "max_output_tokens BETWEEN 256 AND 131072",
            name="max_output_tokens_range",
        ),
        sa.CheckConstraint(
            "max_batch_input_tokens BETWEEN 4096 AND 4000000",
            name="max_batch_input_tokens_range",
        ),
        sa.CheckConstraint(
            "connect_timeout_seconds > 0 AND read_timeout_seconds > 0 "
            "AND write_timeout_seconds > 0 AND pool_timeout_seconds > 0",
            name="timeouts_positive",
        ),
        sa.CheckConstraint(
            "test_status IS NULL OR test_status IN ('succeeded', 'failed')",
            name="test_status_value",
        ),
        sa.CheckConstraint("max_retries BETWEEN 0 AND 10", name="max_retries_range"),
        sa.PrimaryKeyConstraint("agent", name="pk_ai_agent_configs"),
    )
    op.create_index(
        "ix_ai_agent_configs_enabled",
        "ai_agent_configs",
        ["enabled", "updated_at"],
    )
    op.create_table(
        "ai_agent_secrets",
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("key_version > 0", name="key_version_positive"),
        sa.ForeignKeyConstraint(
            ["agent"],
            ["ai_agent_configs.agent"],
            name="fk_ai_agent_secrets_agent",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("agent", name="pk_ai_agent_secrets"),
    )


def downgrade() -> None:
    """移除 Agent 配置和密钥表。"""

    op.drop_table("ai_agent_secrets")
    op.drop_index("ix_ai_agent_configs_enabled", table_name="ai_agent_configs")
    op.drop_table("ai_agent_configs")
