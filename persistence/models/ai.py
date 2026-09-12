"""按职责集中维护的 ai 数据记录。"""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from domain.enums import ModelProvider, ModelReasoningEffort
from persistence.models.base import Base, enum_values, utc_now


class AiSettingsRecord(Base):
    """管理界面维护的全局 AI 配置版本与 Review Planning 预算。"""

    __tablename__ = "ai_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton_id"),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
        UniqueConstraint("revision"),
        CheckConstraint(
            "active_provider IS NULL OR "
            f"active_provider IN ({enum_values(ModelProvider)})",
            name="active_provider_value",
        ),
        CheckConstraint("max_units BETWEEN 1 AND 3000", name="max_units_range"),
        CheckConstraint(
            "max_scope_depth BETWEEN 1 AND 64",
            name="max_scope_depth_range",
        ),
        CheckConstraint(
            "max_unit_input_bytes BETWEEN 4096 AND 10485760",
            name="max_unit_input_bytes_range",
        ),
        CheckConstraint(
            "max_total_input_bytes >= max_unit_input_bytes "
            "AND max_total_input_bytes <= 104857600",
            name="max_total_input_bytes_range",
        ),
        CheckConstraint(
            "max_model_http_calls BETWEEN 1 AND 10000",
            name="max_model_http_calls_range",
        ),
        CheckConstraint(
            "max_model_input_tokens BETWEEN 1000 AND 1000000000",
            name="max_model_input_tokens_range",
        ),
        CheckConstraint(
            "max_model_output_tokens BETWEEN 256 AND 100000000",
            name="max_model_output_tokens_range",
        ),
        CheckConstraint(
            "max_model_cost_microusd IS NULL OR "
            "max_model_cost_microusd BETWEEN 1 AND 1000000000000",
            name="max_model_cost_microusd_range",
        ),
        CheckConstraint(
            "max_model_duration_seconds BETWEEN 30 AND 86400",
            name="max_model_duration_seconds_range",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_provider: Mapped[str | None] = mapped_column(String(20))
    max_units: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    max_scope_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=32)
    max_unit_input_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=192 * 1024
    )
    max_total_input_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2 * 1024 * 1024
    )
    max_model_http_calls: Mapped[int] = mapped_column(
        Integer, nullable=False, default=64
    )
    max_model_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=2_000_000
    )
    max_model_output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=250_000
    )
    max_model_cost_microusd: Mapped[int | None] = mapped_column(BigInteger)
    max_model_duration_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3_600
    )
    updated_by: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AiProviderConfigRecord(Base):
    """一个供应商的非敏感模型参数与最近连接测试状态。"""

    __tablename__ = "ai_provider_configs"
    __table_args__ = (
        CheckConstraint(
            f"provider IN ({enum_values(ModelProvider)})",
            name="provider_value",
        ),
        CheckConstraint(
            "(provider = 'openai' AND api_protocol IN "
            "('responses', 'chat_completions')) OR "
            "(provider = 'anthropic' AND api_protocol = 'messages')",
            name="api_protocol_provider",
        ),
        CheckConstraint(
            "context_window_tokens BETWEEN 8192 AND 4000000",
            name="context_window_tokens_range",
        ),
        CheckConstraint(
            "max_output_tokens BETWEEN 256 AND 131072",
            name="max_output_tokens_range",
        ),
        CheckConstraint(
            "context_window_tokens - max_output_tokens >= 4096",
            name="context_reserves_input",
        ),
        CheckConstraint(
            "reasoning_effort IN ('none', 'low', 'medium', 'high', 'max')",
            name="reasoning_effort_value",
        ),
        CheckConstraint(
            "max_batch_input_tokens BETWEEN 4096 AND 4000000",
            name="max_batch_input_tokens_range",
        ),
        CheckConstraint(
            "connect_timeout_seconds > 0 AND read_timeout_seconds > 0 "
            "AND write_timeout_seconds > 0 AND pool_timeout_seconds > 0",
            name="timeouts_positive",
        ),
        CheckConstraint(
            "max_request_bytes BETWEEN 65536 AND 10485760",
            name="max_request_bytes_range",
        ),
        CheckConstraint(
            "max_response_bytes BETWEEN 65536 AND 16777216",
            name="max_response_bytes_range",
        ),
        CheckConstraint(
            "test_status IS NULL OR test_status IN ('succeeded', 'failed')",
            name="test_status_value",
        ),
        CheckConstraint(
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
        Index("ix_ai_provider_configs_updated_at", "updated_at"),
    )

    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    api_protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    api_base_url: Mapped[str | None] = mapped_column(String(500))
    reasoning_effort: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ModelReasoningEffort.NONE.value
    )
    context_window_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=128_000
    )
    max_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8192
    )
    max_batch_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=64_000
    )
    connect_timeout_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=5.0
    )
    read_timeout_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=180.0
    )
    write_timeout_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=30.0
    )
    pool_timeout_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=5.0
    )
    max_request_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4 * 1024 * 1024
    )
    max_response_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=16 * 1024 * 1024
    )
    input_usd_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    output_usd_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    cache_read_usd_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    cache_write_usd_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    tested_configuration_fingerprint: Mapped[str | None] = mapped_column(String(64))
    test_status: Mapped[str | None] = mapped_column(String(20))
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AiProviderSecretRecord(Base):
    """使用应用主密钥加密的一份供应商 API Key。"""

    __tablename__ = "ai_provider_secrets"
    __table_args__ = (
        CheckConstraint(
            f"provider IN ({enum_values(ModelProvider)})",
            name="provider_value",
        ),
        CheckConstraint("key_version > 0", name="key_version_positive"),
    )

    provider: Mapped[str] = mapped_column(
        String(20),
        ForeignKey(
            "ai_provider_configs.provider",
            name="fk_ai_provider_secrets_provider",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AiAgentConfigRecord(Base):
    """固定 DAG 中每个审查 Agent 的独立模型配置。"""

    __tablename__ = "ai_agent_configs"
    __table_args__ = (
        CheckConstraint(
            "agent IN ('security', 'convention', 'logic', 'summary')",
            name="agent_value",
        ),
        CheckConstraint(
            f"provider IN ({enum_values(ModelProvider)})",
            name="provider_value",
        ),
        CheckConstraint(
            "(provider = 'openai' AND api_protocol IN "
            "('responses', 'chat_completions')) OR "
            "(provider = 'anthropic' AND api_protocol = 'messages')",
            name="api_protocol_provider",
        ),
        CheckConstraint(
            "reasoning_effort IN ('none', 'low', 'medium', 'high', 'max')",
            name="reasoning_effort_value",
        ),
        CheckConstraint(
            "context_window_tokens BETWEEN 8192 AND 4000000",
            name="context_window_tokens_range",
        ),
        CheckConstraint(
            "max_output_tokens BETWEEN 256 AND 131072",
            name="max_output_tokens_range",
        ),
        CheckConstraint(
            "max_batch_input_tokens BETWEEN 4096 AND 4000000",
            name="max_batch_input_tokens_range",
        ),
        CheckConstraint(
            "connect_timeout_seconds > 0 AND read_timeout_seconds > 0 "
            "AND write_timeout_seconds > 0 AND pool_timeout_seconds > 0",
            name="timeouts_positive",
        ),
        CheckConstraint("max_retries BETWEEN 0 AND 10", name="max_retries_range"),
        CheckConstraint(
            "test_status IS NULL OR test_status IN ('succeeded', 'failed')",
            name="test_status_value",
        ),
        Index("ix_ai_agent_configs_enabled", "enabled", "updated_at"),
    )

    agent: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 开启后连接参数来自 AiSettingsRecord 当前激活的公共供应商；保留本行
    # 的独立字段用于兼容旧客户端和切回独立模式时的草稿。
    use_shared_connection: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.false()
    )
    model_override: Mapped[str | None] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    api_protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    api_base_url: Mapped[str | None] = mapped_column(String(500))
    reasoning_effort: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ModelReasoningEffort.NONE.value
    )
    context_window_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=128_000
    )
    max_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8192
    )
    max_batch_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=64_000
    )
    connect_timeout_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=5.0
    )
    read_timeout_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=180.0
    )
    write_timeout_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=30.0
    )
    pool_timeout_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=5.0
    )
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    test_status: Mapped[str | None] = mapped_column(String(20))
    tested_configuration_fingerprint: Mapped[str | None] = mapped_column(String(64))
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AiAgentSecretRecord(Base):
    """每个 Agent 独立保存的加密 API Key。"""

    __tablename__ = "ai_agent_secrets"
    __table_args__ = (CheckConstraint("key_version > 0", name="key_version_positive"),)

    agent: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("ai_agent_configs.agent", ondelete="CASCADE"),
        primary_key=True,
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ConfigurationAuditRecord(Base):
    """不含配置值和密钥内容的管理员配置变更审计。"""

    __tablename__ = "configuration_audits"
    __table_args__ = (
        CheckConstraint("revision > 0", name="revision_positive"),
        UniqueConstraint("revision"),
        Index("ix_configuration_audits_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    changed_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
