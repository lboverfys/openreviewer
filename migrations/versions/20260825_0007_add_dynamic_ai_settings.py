"""增加动态 AI 配置、加密密钥、配置审计和模型调用配置版本。

Revision ID: 20260825_0007
Revises: 20260825_0006
Create Date: 2026-08-25 21:30:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260825_0007"
down_revision: str | None = "20260825_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建动态配置结构；已有模型调用的配置版本保持为空。"""

    op.create_table(
        "ai_settings",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("active_provider", sa.String(length=20)),
        sa.Column("max_units", sa.Integer(), nullable=False),
        sa.Column("max_scope_depth", sa.Integer(), nullable=False),
        sa.Column("max_unit_input_bytes", sa.Integer(), nullable=False),
        sa.Column("max_total_input_bytes", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(length=100)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="singleton_id"),
        sa.CheckConstraint("revision >= 0", name="revision_nonnegative"),
        sa.CheckConstraint(
            "active_provider IS NULL OR active_provider IN ('openai', 'anthropic')",
            name="active_provider_value",
        ),
        sa.CheckConstraint(
            "max_units BETWEEN 1 AND 3000",
            name="max_units_range",
        ),
        sa.CheckConstraint(
            "max_scope_depth BETWEEN 1 AND 64",
            name="max_scope_depth_range",
        ),
        sa.CheckConstraint(
            "max_unit_input_bytes BETWEEN 4096 AND 10485760",
            name="max_unit_input_bytes_range",
        ),
        sa.CheckConstraint(
            "max_total_input_bytes >= max_unit_input_bytes "
            "AND max_total_input_bytes <= 104857600",
            name="max_total_input_bytes_range",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_settings"),
        sa.UniqueConstraint("revision", name="uq_ai_settings_revision"),
    )

    op.create_table(
        "ai_provider_configs",
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("connect_timeout_seconds", sa.Float(), nullable=False),
        sa.Column("read_timeout_seconds", sa.Float(), nullable=False),
        sa.Column("write_timeout_seconds", sa.Float(), nullable=False),
        sa.Column("pool_timeout_seconds", sa.Float(), nullable=False),
        sa.Column("max_request_bytes", sa.Integer(), nullable=False),
        sa.Column("max_response_bytes", sa.Integer(), nullable=False),
        sa.Column("input_usd_per_million", sa.Numeric(18, 6)),
        sa.Column("output_usd_per_million", sa.Numeric(18, 6)),
        sa.Column("cache_read_usd_per_million", sa.Numeric(18, 6)),
        sa.Column("cache_write_usd_per_million", sa.Numeric(18, 6)),
        sa.Column("tested_configuration_fingerprint", sa.String(length=64)),
        sa.Column("test_status", sa.String(length=20)),
        sa.Column("tested_at", sa.DateTime(timezone=True)),
        sa.Column("updated_by", sa.String(length=100), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('openai', 'anthropic')",
            name="provider_value",
        ),
        sa.CheckConstraint(
            "max_output_tokens BETWEEN 256 AND 131072",
            name="max_output_tokens_range",
        ),
        sa.CheckConstraint(
            "connect_timeout_seconds > 0 AND read_timeout_seconds > 0 "
            "AND write_timeout_seconds > 0 AND pool_timeout_seconds > 0",
            name="timeouts_positive",
        ),
        sa.CheckConstraint(
            "max_request_bytes BETWEEN 65536 AND 10485760",
            name="max_request_bytes_range",
        ),
        sa.CheckConstraint(
            "max_response_bytes BETWEEN 65536 AND 10485760",
            name="max_response_bytes_range",
        ),
        sa.CheckConstraint(
            "test_status IS NULL OR test_status IN ('succeeded', 'failed')",
            name="test_status_value",
        ),
        sa.CheckConstraint(
            "(input_usd_per_million IS NULL OR "
            "input_usd_per_million BETWEEN 0 AND 1000000) AND "
            "(output_usd_per_million IS NULL OR "
            "output_usd_per_million BETWEEN 0 AND 1000000) AND "
            "(cache_read_usd_per_million IS NULL OR "
            "cache_read_usd_per_million BETWEEN 0 AND 1000000) AND "
            "(cache_write_usd_per_million IS NULL OR "
            "cache_write_usd_per_million BETWEEN 0 AND 1000000)",
            name="prices_range",
        ),
        sa.PrimaryKeyConstraint("provider", name="pk_ai_provider_configs"),
    )
    op.create_index(
        "ix_ai_provider_configs_updated_at",
        "ai_provider_configs",
        ["updated_at"],
        unique=False,
    )

    op.create_table(
        "ai_provider_secrets",
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('openai', 'anthropic')",
            name="provider_value",
        ),
        sa.CheckConstraint("key_version > 0", name="key_version_positive"),
        sa.ForeignKeyConstraint(
            ["provider"],
            ["ai_provider_configs.provider"],
            name="fk_ai_provider_secrets_provider",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("provider", name="pk_ai_provider_secrets"),
    )

    op.create_table(
        "configuration_audits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("changed_fields", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_configuration_audits"),
        sa.UniqueConstraint(
            "revision",
            name="uq_configuration_audits_revision",
        ),
    )
    op.create_index(
        "ix_configuration_audits_created_at",
        "configuration_audits",
        ["created_at"],
        unique=False,
    )

    with op.batch_alter_table("model_calls") as batch_op:
        batch_op.add_column(
            sa.Column("configuration_revision", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    """删除动态配置结构，并恢复旧模型调用表。"""

    with op.batch_alter_table("model_calls") as batch_op:
        batch_op.drop_column("configuration_revision")
    op.drop_index(
        "ix_configuration_audits_created_at",
        table_name="configuration_audits",
    )
    op.drop_table("configuration_audits")
    op.drop_table("ai_provider_secrets")
    op.drop_index(
        "ix_ai_provider_configs_updated_at",
        table_name="ai_provider_configs",
    )
    op.drop_table("ai_provider_configs")
    op.drop_table("ai_settings")
