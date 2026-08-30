"""数据库驱动的 AI 配置、密钥加密、连接测试与 Worker 运行时。"""

from __future__ import annotations

import json
import os
from base64 import b64decode
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import (
    ModelApiProtocol,
    ModelProvider,
    ModelReasoningEffort,
    ReviewAgent,
)
from domain.identifiers import build_review_version_key
from domain.model_budget import ModelBudgetPolicy
from domain.model_review import ModelReviewInput
from domain.review_planning import ReviewUnit
from domain.security import SafeApplicationError
from persistence.models import (
    AiProviderConfigRecord,
    AiProviderSecretRecord,
    AiSettingsRecord,
    ConfigurationAuditRecord,
)
from services.model_providers import create_model_reviewer
from services.model_review import (
    DEFAULT_MAX_BATCH_INPUT_TOKENS,
    ModelPricing,
    ModelReviewer,
    ModelServiceSettings,
    normalize_api_base_url,
)
from services.review_planning import (
    DeterministicReviewPlanner,
    ReviewPlanningSettings,
)

if TYPE_CHECKING:
    from services.agent_settings import AgentSettingsService
    from services.agent_workflow import FixedAgentWorkflow


AI_SETTINGS_ID = 1
SUPPORTED_PROVIDERS = (ModelProvider.OPENAI, ModelProvider.ANTHROPIC)
DEFAULT_MAX_UNITS = 100
DEFAULT_MAX_SCOPE_DEPTH = 32
DEFAULT_MAX_UNIT_INPUT_BYTES = 192 * 1024
DEFAULT_MAX_TOTAL_INPUT_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_MODEL_HTTP_CALLS = 64
DEFAULT_MAX_MODEL_INPUT_TOKENS = 2_000_000
DEFAULT_MAX_MODEL_OUTPUT_TOKENS = 250_000
DEFAULT_MAX_MODEL_COST_MICROUSD: int | None = None
DEFAULT_MAX_MODEL_DURATION_SECONDS = 900
_MAX_KEY_FILE_BYTES = 4096


class AiSettingsError(RuntimeError):
    """AI 配置操作无法安全完成。"""


class AiSettingsConfigurationError(AiSettingsError):
    """加密主密钥等进程级安全配置缺失或无效。"""


class AiSettingsPersistenceError(AiSettingsError):
    """AI 配置暂时无法从数据库读取或写入。"""


class AiSettingsConflictError(AiSettingsError):
    """调用方提交的 revision 已经过期。"""


class AiSettingsValidationError(AiSettingsError):
    """管理员提交的模型或预算参数不满足边界。"""


class AiProviderNotReadyError(AiSettingsError):
    """供应商尚未保存、没有密钥或没有通过当前配置测试。"""


class AiConnectionTestError(AiSettingsError):
    """真实供应商连接测试失败，消息已经过安全边界处理。"""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class EncryptedAiSecret:
    ciphertext: bytes
    nonce: bytes
    key_version: int


@dataclass(frozen=True, slots=True)
class AiSecretCipher:
    """使用 AES-256-GCM 和供应商绑定的附加认证数据保护 API Key。"""

    key: bytes = field(repr=False)
    key_version: int = 1
    previous_keys: tuple[tuple[int, bytes], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if len(self.key) != 32:
            raise AiSettingsConfigurationError(
                "AI 配置加密主密钥解码后必须正好为 32 字节"
            )
        if self.key_version <= 0:
            raise AiSettingsConfigurationError("AI 配置密钥版本必须为正整数")
        if len(self.previous_keys) > 4:
            raise AiSettingsConfigurationError("最多支持四个旧版 AI 配置密钥")
        versions = [self.key_version]
        for version, key in self.previous_keys:
            if not isinstance(version, int) or version <= 0:
                raise AiSettingsConfigurationError("旧版 AI 配置密钥版本必须为正整数")
            if len(key) != 32:
                raise AiSettingsConfigurationError(
                    "旧版 AI 配置密钥解码后必须正好为 32 字节"
                )
            versions.append(version)
        if len(versions) != len(set(versions)):
            raise AiSettingsConfigurationError("AI 配置密钥版本不能重复")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> AiSecretCipher:
        values = os.environ if environment is None else environment
        direct = values.get("OPENREVIEWER_AI_CONFIG_KEY", "").strip()
        key_file = values.get("OPENREVIEWER_AI_CONFIG_KEY_FILE", "").strip()
        if direct and key_file:
            raise AiSettingsConfigurationError(
                "AI 配置加密主密钥只能选择直接值或文件中的一种"
            )
        if key_file:
            path = Path(key_file)
            try:
                if path.stat().st_size > _MAX_KEY_FILE_BYTES:
                    raise AiSettingsConfigurationError("AI 配置密钥文件过大")
                encoded = path.read_text(encoding="ascii").strip()
            except AiSettingsConfigurationError:
                raise
            except (OSError, UnicodeError) as exc:
                raise AiSettingsConfigurationError(
                    "AI 配置密钥文件无法读取"
                ) from exc
        else:
            encoded = direct
        if not encoded:
            raise AiSettingsConfigurationError(
                "必须配置 OPENREVIEWER_AI_CONFIG_KEY 或其文件路径"
            )
        key = _decode_ai_config_key(encoded, "AI 配置加密主密钥")
        raw_version = values.get("OPENREVIEWER_AI_CONFIG_KEY_VERSION", "1")
        try:
            key_version = int(raw_version)
        except ValueError as exc:
            raise AiSettingsConfigurationError(
                "OPENREVIEWER_AI_CONFIG_KEY_VERSION 必须为正整数"
            ) from exc
        previous_keys = _read_previous_ai_config_keys(values)
        return cls(
            key=key,
            key_version=key_version,
            previous_keys=previous_keys,
        )

    def encrypt(self, provider: ModelProvider, api_key: str) -> EncryptedAiSecret:
        normalized = api_key.strip()
        if not normalized or normalized != api_key or any(
            character.isspace() for character in normalized
        ):
            raise AiSettingsValidationError("API Key 不能为空或包含空白字符")
        if len(normalized.encode("utf-8")) > 64 * 1024:
            raise AiSettingsValidationError("API Key 超过允许大小")
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.key).encrypt(
            nonce,
            normalized.encode("utf-8"),
            self._associated_data(provider, self.key_version),
        )
        return EncryptedAiSecret(ciphertext, nonce, self.key_version)

    def decrypt(
        self,
        provider: ModelProvider,
        ciphertext: bytes,
        nonce: bytes,
        key_version: int,
    ) -> str:
        decryption_key = self.key if key_version == self.key_version else next(
            (
                key
                for previous_version, key in self.previous_keys
                if previous_version == key_version
            ),
            None,
        )
        if decryption_key is None:
            raise AiSettingsConfigurationError(
                "数据库中的 AI 密钥版本不在当前解密密钥环中"
            )
        try:
            plaintext = AESGCM(decryption_key).decrypt(
                nonce,
                ciphertext,
                self._associated_data(provider, key_version),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
            raise AiSettingsConfigurationError(
                "数据库中的 AI API Key 无法解密"
            ) from exc

    @staticmethod
    def _associated_data(provider: ModelProvider, key_version: int) -> bytes:
        return f"openreviewer:ai-provider:{provider.value}:v{key_version}".encode(
            "ascii"
        )


def _decode_ai_config_key(encoded: str, label: str) -> bytes:
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        key = b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise AiSettingsConfigurationError(
            f"{label}必须是 URL-safe Base64"
        ) from exc
    if len(key) != 32:
        raise AiSettingsConfigurationError(f"{label}解码后必须正好为 32 字节")
    return key


def _read_previous_ai_config_keys(
    values: Mapping[str, str],
) -> tuple[tuple[int, bytes], ...]:
    direct = values.get("OPENREVIEWER_AI_CONFIG_PREVIOUS_KEYS_JSON", "").strip()
    file_name = values.get(
        "OPENREVIEWER_AI_CONFIG_PREVIOUS_KEYS_FILE", ""
    ).strip()
    if direct and file_name:
        raise AiSettingsConfigurationError(
            "旧版 AI 配置密钥只能选择直接值或文件中的一种"
        )
    if file_name:
        path = Path(file_name)
        try:
            if path.stat().st_size > 16 * 1024:
                raise AiSettingsConfigurationError("旧版 AI 配置密钥文件过大")
            raw = path.read_text(encoding="utf-8").strip()
        except AiSettingsConfigurationError:
            raise
        except (OSError, UnicodeError) as exc:
            raise AiSettingsConfigurationError(
                "旧版 AI 配置密钥文件无法读取"
            ) from exc
    else:
        raw = direct
    if not raw:
        return ()
    if len(raw.encode("utf-8")) > 16 * 1024:
        raise AiSettingsConfigurationError("旧版 AI 配置密钥列表过大")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AiSettingsConfigurationError(
            "旧版 AI 配置密钥必须是 JSON 数组"
        ) from exc
    if not isinstance(payload, list) or len(payload) > 4:
        raise AiSettingsConfigurationError(
            "旧版 AI 配置密钥必须是最多四项的 JSON 数组"
        )
    result: list[tuple[int, bytes]] = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != {"version", "key"}:
            raise AiSettingsConfigurationError(
                "每个旧版 AI 配置密钥必须只包含 version 和 key"
            )
        version = item["version"]
        encoded_key = item["key"]
        if not isinstance(version, int) or not isinstance(encoded_key, str):
            raise AiSettingsConfigurationError("旧版 AI 配置密钥字段类型无效")
        result.append(
            (version, _decode_ai_config_key(encoded_key.strip(), "旧版 AI 配置密钥"))
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class AiProviderDraft:
    model: str
    api_protocol: ModelApiProtocol
    api_base_url: str | None = None
    reasoning_effort: ModelReasoningEffort = ModelReasoningEffort.NONE
    context_window_tokens: int = 128_000
    max_output_tokens: int = 8192
    max_batch_input_tokens: int = DEFAULT_MAX_BATCH_INPUT_TOKENS
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 180.0
    write_timeout_seconds: float = 30.0
    pool_timeout_seconds: float = 5.0
    max_request_bytes: int = 4 * 1024 * 1024
    max_response_bytes: int = 2 * 1024 * 1024
    input_usd_per_million: Decimal | None = None
    output_usd_per_million: Decimal | None = None
    cache_read_usd_per_million: Decimal | None = None
    cache_write_usd_per_million: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ReviewPolicyDraft:
    max_units: int
    max_scope_depth: int
    max_unit_input_bytes: int
    max_total_input_bytes: int
    max_model_http_calls: int = DEFAULT_MAX_MODEL_HTTP_CALLS
    max_model_input_tokens: int = DEFAULT_MAX_MODEL_INPUT_TOKENS
    max_model_output_tokens: int = DEFAULT_MAX_MODEL_OUTPUT_TOKENS
    max_model_cost_microusd: int | None = DEFAULT_MAX_MODEL_COST_MICROUSD
    max_model_duration_seconds: int = DEFAULT_MAX_MODEL_DURATION_SECONDS


@dataclass(frozen=True, slots=True)
class AiProviderView:
    provider: ModelProvider
    configured: bool
    active: bool
    model: str
    api_protocol: ModelApiProtocol
    api_base_url: str | None
    reasoning_effort: ModelReasoningEffort
    api_key_configured: bool
    api_key_mask: str | None
    context_window_tokens: int
    max_output_tokens: int
    max_batch_input_tokens: int
    connect_timeout_seconds: float
    read_timeout_seconds: float
    write_timeout_seconds: float
    pool_timeout_seconds: float
    max_request_bytes: int
    max_response_bytes: int
    input_usd_per_million: Decimal | None
    output_usd_per_million: Decimal | None
    cache_read_usd_per_million: Decimal | None
    cache_write_usd_per_million: Decimal | None
    test_status: Literal["untested", "succeeded", "failed"]
    tested_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class AiSettingsView:
    revision: int
    active_provider: ModelProvider | None
    max_units: int
    max_scope_depth: int
    max_unit_input_bytes: int
    max_total_input_bytes: int
    max_model_http_calls: int
    max_model_input_tokens: int
    max_model_output_tokens: int
    max_model_cost_microusd: int | None
    max_model_duration_seconds: int
    updated_at: datetime | None
    updated_by: str | None
    providers: tuple[AiProviderView, ...]


@dataclass(frozen=True, slots=True)
class ConfigurationAuditView:
    revision: int
    actor: str
    action: str
    changed_fields: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ActiveAiSettings:
    revision: int
    model: ModelServiceSettings
    planning: ReviewPlanningSettings


@dataclass(frozen=True, slots=True)
class ActiveAiRuntime:
    revision: int
    reviewer: ModelReviewer | None
    planner: DeterministicReviewPlanner
    model_settings: ModelServiceSettings | None = None
    agent_workflow: FixedAgentWorkflow | None = None


class AiRuntimeProvider(Protocol):
    """Worker 每轮读取当前激活配置所需的最小边界。"""

    def current(self) -> ActiveAiRuntime | None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _PreparedProviderTest:
    revision: int
    provider: ModelProvider
    fingerprint: str
    settings: ModelServiceSettings


class AiSettingsService:
    """用固定次数查询管理草稿、测试、激活、动态读取与审计。"""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        cipher: AiSecretCipher,
        *,
        clock: Callable[[], datetime] | None = None,
        connection_tester: Callable[[ModelServiceSettings], None] | None = None,
    ) -> None:
        self._sessions = sessions
        self._cipher = cipher
        self._clock = clock or (lambda: datetime.now(UTC))
        self._connection_tester = connection_tester or _test_model_connection

    def get(self) -> AiSettingsView:
        with self._sessions() as session:
            try:
                return self._snapshot(session)
            except AiSettingsError:
                raise
            except SQLAlchemyError as exc:
                raise AiSettingsPersistenceError("AI 配置暂时无法读取") from exc

    def update_provider(
        self,
        provider: ModelProvider,
        draft: AiProviderDraft,
        *,
        expected_revision: int,
        actor: str,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> AiSettingsView:
        if api_key is not None and clear_api_key:
            raise AiSettingsValidationError("不能同时设置并清除 API Key")
        self._model_settings(provider, draft, api_key or "validation-key")
        now = self._clock()
        with self._sessions() as session:
            try:
                settings = self._lock_settings(session, expected_revision, now)
                record = session.get(AiProviderConfigRecord, provider.value)
                changed_fields = self._provider_changed_fields(record, draft)
                secret = session.get(AiProviderSecretRecord, provider.value)
                if (
                    record is not None
                    and "api_base_url" in changed_fields
                    and secret is not None
                    and api_key is None
                    and not clear_api_key
                ):
                    raise AiSettingsValidationError(
                        "切换 API 地址时必须同时提供新的 API Key"
                    )
                if record is None:
                    record = AiProviderConfigRecord(
                        provider=provider.value,
                        model=draft.model,
                        api_protocol=draft.api_protocol.value,
                        updated_by=actor,
                        updated_at=now,
                    )
                    session.add(record)
                self._apply_provider_draft(record, draft, actor, now)

                if api_key is not None:
                    encrypted = self._cipher.encrypt(provider, api_key)
                    if secret is None:
                        secret = AiProviderSecretRecord(
                            provider=provider.value,
                            ciphertext=encrypted.ciphertext,
                            nonce=encrypted.nonce,
                            key_version=encrypted.key_version,
                            updated_at=now,
                        )
                        session.add(secret)
                    else:
                        secret.ciphertext = encrypted.ciphertext
                        secret.nonce = encrypted.nonce
                        secret.key_version = encrypted.key_version
                        secret.updated_at = now
                    changed_fields.add("api_key")
                elif clear_api_key and secret is not None:
                    session.delete(secret)
                    changed_fields.add("api_key")

                if not changed_fields:
                    session.rollback()
                    return self.get()
                configuration_fields = changed_fields.intersection(
                    {
                        "model",
                        "api_protocol",
                        "api_base_url",
                        "reasoning_effort",
                        "max_output_tokens",
                        "connect_timeout_seconds",
                        "read_timeout_seconds",
                        "write_timeout_seconds",
                        "pool_timeout_seconds",
                        "max_request_bytes",
                        "max_response_bytes",
                        "api_key",
                    }
                )
                if configuration_fields:
                    record.tested_configuration_fingerprint = None
                    record.test_status = None
                    record.tested_at = None
                if configuration_fields and settings.active_provider == provider.value:
                    settings.active_provider = None
                    changed_fields.add("active_provider")
                self._commit_revision(
                    session,
                    settings,
                    actor=actor,
                    action=f"provider.{provider.value}.updated",
                    changed_fields=changed_fields,
                    now=now,
                )
                session.commit()
            except AiSettingsError:
                session.rollback()
                raise
            except IntegrityError as exc:
                session.rollback()
                raise AiSettingsConflictError(
                    "配置已被其他管理员更新，请刷新后重试"
                ) from exc
            except SQLAlchemyError as exc:
                session.rollback()
                raise AiSettingsPersistenceError("AI 配置暂时无法保存") from exc
        return self.get()

    def update_review_policy(
        self,
        draft: ReviewPolicyDraft,
        *,
        expected_revision: int,
        actor: str,
    ) -> AiSettingsView:
        try:
            model_budget = ModelBudgetPolicy(
                max_http_calls=draft.max_model_http_calls,
                max_input_tokens=draft.max_model_input_tokens,
                max_output_tokens=draft.max_model_output_tokens,
                max_estimated_cost_microusd=draft.max_model_cost_microusd,
                max_duration_seconds=draft.max_model_duration_seconds,
            )
            ReviewPlanningSettings(
                max_units=draft.max_units,
                max_scope_depth=draft.max_scope_depth,
                max_unit_input_bytes=draft.max_unit_input_bytes,
                max_total_input_bytes=draft.max_total_input_bytes,
                model_budget=model_budget,
            )
        except ValueError as exc:
            raise AiSettingsValidationError(str(exc)) from exc
        now = self._clock()
        with self._sessions() as session:
            try:
                settings = self._lock_settings(session, expected_revision, now)
                changed_fields = {
                    name
                    for name in (
                        "max_units",
                        "max_scope_depth",
                        "max_unit_input_bytes",
                        "max_total_input_bytes",
                        "max_model_http_calls",
                        "max_model_input_tokens",
                        "max_model_output_tokens",
                        "max_model_cost_microusd",
                        "max_model_duration_seconds",
                    )
                    if getattr(settings, name) != getattr(draft, name)
                }
                if not changed_fields:
                    session.rollback()
                    return self.get()
                for name in changed_fields:
                    setattr(settings, name, getattr(draft, name))
                self._commit_revision(
                    session,
                    settings,
                    actor=actor,
                    action="review_policy.updated",
                    changed_fields=changed_fields,
                    now=now,
                )
                session.commit()
            except AiSettingsError:
                session.rollback()
                raise
            except IntegrityError as exc:
                session.rollback()
                raise AiSettingsConflictError(
                    "配置已被其他管理员更新，请刷新后重试"
                ) from exc
            except SQLAlchemyError as exc:
                session.rollback()
                raise AiSettingsPersistenceError("审查预算暂时无法保存") from exc
        return self.get()

    def test_provider(
        self,
        provider: ModelProvider,
        *,
        expected_revision: int,
        actor: str,
    ) -> AiSettingsView:
        prepared = self._prepare_provider_test(provider, expected_revision)
        try:
            self._connection_tester(
                replace(
                    prepared.settings,
                    max_output_tokens=min(prepared.settings.max_output_tokens, 512),
                )
            )
        except SafeApplicationError as exc:
            message = exc.error.safe_message
            retryable = exc.error.retryable
            self._record_test(prepared, actor, succeeded=False)
            raise AiConnectionTestError(message, retryable=retryable) from exc
        except Exception as exc:
            self._record_test(prepared, actor, succeeded=False)
            raise AiConnectionTestError(
                "模型连接测试未通过",
                retryable=True,
            ) from exc
        self._record_test(prepared, actor, succeeded=True)
        return self.get()

    def activate_provider(
        self,
        provider: ModelProvider,
        *,
        expected_revision: int,
        actor: str,
    ) -> AiSettingsView:
        now = self._clock()
        with self._sessions() as session:
            try:
                settings = self._lock_settings(session, expected_revision, now)
                config = session.get(AiProviderConfigRecord, provider.value)
                secret = session.get(AiProviderSecretRecord, provider.value)
                if config is None or secret is None:
                    raise AiProviderNotReadyError("请先保存模型参数和 API Key")
                api_key = self._decrypt_secret(provider, secret)
                fingerprint = self._configuration_fingerprint(
                    self._record_to_model_settings(provider, config, api_key)
                )
                if (
                    config.test_status != "succeeded"
                    or config.tested_configuration_fingerprint != fingerprint
                ):
                    raise AiProviderNotReadyError("当前配置必须先通过连接测试")
                if settings.active_provider == provider.value:
                    session.rollback()
                    return self.get()
                settings.active_provider = provider.value
                self._commit_revision(
                    session,
                    settings,
                    actor=actor,
                    action=f"provider.{provider.value}.activated",
                    changed_fields={"active_provider"},
                    now=now,
                )
                session.commit()
            except AiSettingsError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise AiSettingsPersistenceError("AI 供应商暂时无法激活") from exc
        return self.get()

    def audits(self, limit: int = 50) -> tuple[ConfigurationAuditView, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("audit limit must be between 1 and 100")
        with self._sessions() as session:
            try:
                rows = session.scalars(
                    select(ConfigurationAuditRecord)
                    .order_by(ConfigurationAuditRecord.revision.desc())
                    .limit(limit)
                ).all()
            except SQLAlchemyError as exc:
                raise AiSettingsPersistenceError("配置审计暂时无法读取") from exc
        return tuple(
            ConfigurationAuditView(
                revision=row.revision,
                actor=row.actor,
                action=row.action,
                changed_fields=tuple(row.changed_fields),
                created_at=row.created_at,
            )
            for row in rows
        )

    def active_settings(self) -> ActiveAiSettings | None:
        """以一次有索引 JOIN 读取 Worker 下一阶段使用的完整配置快照。"""

        with self._sessions() as session:
            try:
                row = session.execute(
                    select(
                        AiSettingsRecord,
                        AiProviderConfigRecord,
                        AiProviderSecretRecord,
                    )
                    .join(
                        AiProviderConfigRecord,
                        AiProviderConfigRecord.provider
                        == AiSettingsRecord.active_provider,
                    )
                    .join(
                        AiProviderSecretRecord,
                        AiProviderSecretRecord.provider
                        == AiProviderConfigRecord.provider,
                    )
                    .where(AiSettingsRecord.id == AI_SETTINGS_ID)
                    .limit(1)
                ).one_or_none()
            except SQLAlchemyError as exc:
                raise AiSettingsPersistenceError("激活的 AI 配置暂时无法读取") from exc
        if row is None:
            return None
        settings, config, secret = row
        provider = ModelProvider(config.provider)
        api_key = self._decrypt_secret(provider, secret)
        try:
            model = self._record_to_model_settings(provider, config, api_key)
            planning = ReviewPlanningSettings(
                max_units=settings.max_units,
                max_scope_depth=settings.max_scope_depth,
                max_unit_input_bytes=settings.max_unit_input_bytes,
                max_total_input_bytes=settings.max_total_input_bytes,
                model_budget=self._model_budget(settings),
            )
        except ValueError as exc:
            raise AiSettingsConfigurationError(
                "激活的 AI 配置不符合当前程序约束"
            ) from exc
        return ActiveAiSettings(settings.revision, model, planning)

    def _prepare_provider_test(
        self,
        provider: ModelProvider,
        expected_revision: int,
    ) -> _PreparedProviderTest:
        with self._sessions() as session:
            try:
                settings = session.get(AiSettingsRecord, AI_SETTINGS_ID)
                revision = settings.revision if settings is not None else 0
                self._check_revision(revision, expected_revision)
                config = session.get(AiProviderConfigRecord, provider.value)
                secret = session.get(AiProviderSecretRecord, provider.value)
                if config is None or secret is None:
                    raise AiProviderNotReadyError("请先保存模型参数和 API Key")
                model_settings = self._record_to_model_settings(
                    provider,
                    config,
                    self._decrypt_secret(provider, secret),
                )
                return _PreparedProviderTest(
                    revision=revision,
                    provider=provider,
                    fingerprint=self._configuration_fingerprint(model_settings),
                    settings=model_settings,
                )
            except AiSettingsError:
                raise
            except SQLAlchemyError as exc:
                raise AiSettingsPersistenceError("AI 配置暂时无法读取") from exc

    def _record_test(
        self,
        prepared: _PreparedProviderTest,
        actor: str,
        *,
        succeeded: bool,
    ) -> None:
        now = self._clock()
        with self._sessions() as session:
            try:
                settings = self._lock_settings(session, prepared.revision, now)
                config = session.get(
                    AiProviderConfigRecord,
                    prepared.provider.value,
                )
                secret = session.get(
                    AiProviderSecretRecord,
                    prepared.provider.value,
                )
                if config is None or secret is None:
                    raise AiSettingsConflictError("测试期间配置已发生变化")
                current = self._record_to_model_settings(
                    prepared.provider,
                    config,
                    self._decrypt_secret(prepared.provider, secret),
                )
                if self._configuration_fingerprint(current) != prepared.fingerprint:
                    raise AiSettingsConflictError("测试期间配置已发生变化")
                config.test_status = "succeeded" if succeeded else "failed"
                config.tested_configuration_fingerprint = prepared.fingerprint
                config.tested_at = now
                self._commit_revision(
                    session,
                    settings,
                    actor=actor,
                    action=(
                        f"provider.{prepared.provider.value}.test_succeeded"
                        if succeeded
                        else f"provider.{prepared.provider.value}.test_failed"
                    ),
                    changed_fields={"test_status"},
                    now=now,
                )
                session.commit()
            except AiSettingsError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise AiSettingsPersistenceError("连接测试结果暂时无法保存") from exc

    def _snapshot(self, session: Session) -> AiSettingsView:
        settings = session.get(AiSettingsRecord, AI_SETTINGS_ID)
        rows = session.execute(
            select(AiProviderConfigRecord, AiProviderSecretRecord)
            .outerjoin(
                AiProviderSecretRecord,
                AiProviderSecretRecord.provider == AiProviderConfigRecord.provider,
            )
            .where(
                AiProviderConfigRecord.provider.in_(
                    tuple(provider.value for provider in SUPPORTED_PROVIDERS)
                )
            )
        ).all()
        by_provider = {record.provider: (record, secret) for record, secret in rows}
        active = (
            ModelProvider(settings.active_provider)
            if settings is not None and settings.active_provider is not None
            else None
        )
        providers: list[AiProviderView] = []
        for provider in SUPPORTED_PROVIDERS:
            pair = by_provider.get(provider.value)
            if pair is None:
                providers.append(self._empty_provider_view(provider, active))
                continue
            record, secret = pair
            api_key = self._decrypt_secret(provider, secret) if secret else None
            providers.append(
                AiProviderView(
                    provider=provider,
                    configured=True,
                    active=active is provider,
                    model=record.model,
                    api_protocol=ModelApiProtocol(record.api_protocol),
                    api_base_url=record.api_base_url,
                    reasoning_effort=ModelReasoningEffort(record.reasoning_effort),
                    api_key_configured=secret is not None,
                    api_key_mask=(f"****{api_key[-4:]}" if api_key else None),
                    context_window_tokens=record.context_window_tokens,
                    max_output_tokens=record.max_output_tokens,
                    max_batch_input_tokens=record.max_batch_input_tokens,
                    connect_timeout_seconds=record.connect_timeout_seconds,
                    read_timeout_seconds=record.read_timeout_seconds,
                    write_timeout_seconds=record.write_timeout_seconds,
                    pool_timeout_seconds=record.pool_timeout_seconds,
                    max_request_bytes=record.max_request_bytes,
                    max_response_bytes=record.max_response_bytes,
                    input_usd_per_million=record.input_usd_per_million,
                    output_usd_per_million=record.output_usd_per_million,
                    cache_read_usd_per_million=record.cache_read_usd_per_million,
                    cache_write_usd_per_million=record.cache_write_usd_per_million,
                    test_status=record.test_status or "untested",
                    tested_at=record.tested_at,
                    updated_at=record.updated_at,
                )
            )
        return AiSettingsView(
            revision=settings.revision if settings is not None else 0,
            active_provider=active,
            max_units=settings.max_units if settings else DEFAULT_MAX_UNITS,
            max_scope_depth=(
                settings.max_scope_depth if settings else DEFAULT_MAX_SCOPE_DEPTH
            ),
            max_unit_input_bytes=(
                settings.max_unit_input_bytes
                if settings
                else DEFAULT_MAX_UNIT_INPUT_BYTES
            ),
            max_total_input_bytes=(
                settings.max_total_input_bytes
                if settings
                else DEFAULT_MAX_TOTAL_INPUT_BYTES
            ),
            max_model_http_calls=(
                settings.max_model_http_calls
                if settings
                else DEFAULT_MAX_MODEL_HTTP_CALLS
            ),
            max_model_input_tokens=(
                settings.max_model_input_tokens
                if settings
                else DEFAULT_MAX_MODEL_INPUT_TOKENS
            ),
            max_model_output_tokens=(
                settings.max_model_output_tokens
                if settings
                else DEFAULT_MAX_MODEL_OUTPUT_TOKENS
            ),
            max_model_cost_microusd=(
                settings.max_model_cost_microusd
                if settings
                else DEFAULT_MAX_MODEL_COST_MICROUSD
            ),
            max_model_duration_seconds=(
                settings.max_model_duration_seconds
                if settings
                else DEFAULT_MAX_MODEL_DURATION_SECONDS
            ),
            updated_at=settings.updated_at if settings else None,
            updated_by=settings.updated_by if settings else None,
            providers=tuple(providers),
        )

    @staticmethod
    def _empty_provider_view(
        provider: ModelProvider,
        active: ModelProvider | None,
    ) -> AiProviderView:
        return AiProviderView(
            provider=provider,
            configured=False,
            active=active is provider,
            model="",
            api_protocol=(
                ModelApiProtocol.RESPONSES
                if provider is ModelProvider.OPENAI
                else ModelApiProtocol.MESSAGES
            ),
            api_base_url=None,
            reasoning_effort=ModelReasoningEffort.NONE,
            api_key_configured=False,
            api_key_mask=None,
            context_window_tokens=(
                128_000 if provider is ModelProvider.OPENAI else 200_000
            ),
            max_output_tokens=8192,
            max_batch_input_tokens=DEFAULT_MAX_BATCH_INPUT_TOKENS,
            connect_timeout_seconds=5.0,
            read_timeout_seconds=180.0,
            write_timeout_seconds=30.0,
            pool_timeout_seconds=5.0,
            max_request_bytes=4 * 1024 * 1024,
            max_response_bytes=2 * 1024 * 1024,
            input_usd_per_million=None,
            output_usd_per_million=None,
            cache_read_usd_per_million=None,
            cache_write_usd_per_million=None,
            test_status="untested",
            tested_at=None,
            updated_at=None,
        )

    def _lock_settings(
        self,
        session: Session,
        expected_revision: int,
        now: datetime,
    ) -> AiSettingsRecord:
        settings = session.scalar(
            select(AiSettingsRecord)
            .where(AiSettingsRecord.id == AI_SETTINGS_ID)
            .with_for_update()
        )
        if settings is None:
            self._check_revision(0, expected_revision)
            settings = AiSettingsRecord(
                id=AI_SETTINGS_ID,
                revision=0,
                max_units=DEFAULT_MAX_UNITS,
                max_scope_depth=DEFAULT_MAX_SCOPE_DEPTH,
                max_unit_input_bytes=DEFAULT_MAX_UNIT_INPUT_BYTES,
                max_total_input_bytes=DEFAULT_MAX_TOTAL_INPUT_BYTES,
                max_model_http_calls=DEFAULT_MAX_MODEL_HTTP_CALLS,
                max_model_input_tokens=DEFAULT_MAX_MODEL_INPUT_TOKENS,
                max_model_output_tokens=DEFAULT_MAX_MODEL_OUTPUT_TOKENS,
                max_model_cost_microusd=DEFAULT_MAX_MODEL_COST_MICROUSD,
                max_model_duration_seconds=DEFAULT_MAX_MODEL_DURATION_SECONDS,
                updated_at=now,
            )
            session.add(settings)
            session.flush()
        else:
            self._check_revision(settings.revision, expected_revision)
        return settings

    @staticmethod
    def _model_budget(settings: AiSettingsRecord) -> ModelBudgetPolicy:
        return ModelBudgetPolicy(
            max_http_calls=settings.max_model_http_calls,
            max_input_tokens=settings.max_model_input_tokens,
            max_output_tokens=settings.max_model_output_tokens,
            max_estimated_cost_microusd=settings.max_model_cost_microusd,
            max_duration_seconds=settings.max_model_duration_seconds,
        )

    @staticmethod
    def _check_revision(current: int, expected: int) -> None:
        if expected < 0 or current != expected:
            raise AiSettingsConflictError(
                "配置已被其他管理员更新，请刷新后重试"
            )

    def _commit_revision(
        self,
        session: Session,
        settings: AiSettingsRecord,
        *,
        actor: str,
        action: str,
        changed_fields: set[str],
        now: datetime,
    ) -> None:
        settings.revision += 1
        settings.updated_by = actor
        settings.updated_at = now
        session.add(
            ConfigurationAuditRecord(
                id=str(uuid4()),
                revision=settings.revision,
                actor=actor,
                action=action,
                changed_fields=sorted(changed_fields),
                created_at=now,
            )
        )

    @staticmethod
    def _provider_changed_fields(
        record: AiProviderConfigRecord | None,
        draft: AiProviderDraft,
    ) -> set[str]:
        names = (
            "model",
            "api_protocol",
            "api_base_url",
            "reasoning_effort",
            "context_window_tokens",
            "max_output_tokens",
            "max_batch_input_tokens",
            "connect_timeout_seconds",
            "read_timeout_seconds",
            "write_timeout_seconds",
            "pool_timeout_seconds",
            "max_request_bytes",
            "max_response_bytes",
            "input_usd_per_million",
            "output_usd_per_million",
            "cache_read_usd_per_million",
            "cache_write_usd_per_million",
        )
        if record is None:
            return set(names)
        return {
            name
            for name in names
            if (
                ModelApiProtocol(record.api_protocol) != draft.api_protocol
                if name == "api_protocol"
                else (
                    ModelReasoningEffort(record.reasoning_effort)
                    != draft.reasoning_effort
                    if name == "reasoning_effort"
                    else (
                        record.api_base_url
                        != normalize_api_base_url(draft.api_base_url)
                        if name == "api_base_url"
                        else getattr(record, name) != getattr(draft, name)
                    )
                )
            )
        }

    @staticmethod
    def _apply_provider_draft(
        record: AiProviderConfigRecord,
        draft: AiProviderDraft,
        actor: str,
        now: datetime,
    ) -> None:
        for name in (
            "model",
            "api_protocol",
            "api_base_url",
            "reasoning_effort",
            "context_window_tokens",
            "max_output_tokens",
            "max_batch_input_tokens",
            "connect_timeout_seconds",
            "read_timeout_seconds",
            "write_timeout_seconds",
            "pool_timeout_seconds",
            "max_request_bytes",
            "max_response_bytes",
            "input_usd_per_million",
            "output_usd_per_million",
            "cache_read_usd_per_million",
            "cache_write_usd_per_million",
        ):
            value = getattr(draft, name)
            if name == "api_base_url":
                value = normalize_api_base_url(value)
            setattr(
                record,
                name,
                value.value
                if name in {"api_protocol", "reasoning_effort"}
                else value,
            )
        record.updated_by = actor
        record.updated_at = now

    def _record_to_model_settings(
        self,
        provider: ModelProvider,
        record: AiProviderConfigRecord,
        api_key: str,
    ) -> ModelServiceSettings:
        draft = AiProviderDraft(
            model=record.model,
            api_protocol=ModelApiProtocol(record.api_protocol),
            api_base_url=record.api_base_url,
            reasoning_effort=ModelReasoningEffort(record.reasoning_effort),
            context_window_tokens=record.context_window_tokens,
            max_output_tokens=record.max_output_tokens,
            max_batch_input_tokens=record.max_batch_input_tokens,
            connect_timeout_seconds=record.connect_timeout_seconds,
            read_timeout_seconds=record.read_timeout_seconds,
            write_timeout_seconds=record.write_timeout_seconds,
            pool_timeout_seconds=record.pool_timeout_seconds,
            max_request_bytes=record.max_request_bytes,
            max_response_bytes=record.max_response_bytes,
            input_usd_per_million=record.input_usd_per_million,
            output_usd_per_million=record.output_usd_per_million,
            cache_read_usd_per_million=record.cache_read_usd_per_million,
            cache_write_usd_per_million=record.cache_write_usd_per_million,
        )
        return self._model_settings(provider, draft, api_key)

    @staticmethod
    def _model_settings(
        provider: ModelProvider,
        draft: AiProviderDraft,
        api_key: str,
    ) -> ModelServiceSettings:
        try:
            prices = (
                draft.input_usd_per_million,
                draft.output_usd_per_million,
                draft.cache_read_usd_per_million,
                draft.cache_write_usd_per_million,
            )
            if (prices[0] is None) != (prices[1] is None):
                raise ValueError("输入和输出 Token 单价必须同时填写")
            pricing = (
                ModelPricing(
                    input_usd_per_million=prices[0],
                    output_usd_per_million=prices[1],
                    cache_read_usd_per_million=prices[2],
                    cache_write_usd_per_million=prices[3],
                )
                if prices[0] is not None and prices[1] is not None
                else None
            )
            return ModelServiceSettings(
                provider=provider,
                model=draft.model,
                api_key=api_key,
                api_protocol=draft.api_protocol,
                api_base_url=normalize_api_base_url(draft.api_base_url),
                reasoning_effort=draft.reasoning_effort,
                pricing=pricing,
                context_window_tokens=draft.context_window_tokens,
                max_output_tokens=draft.max_output_tokens,
                max_batch_input_tokens=draft.max_batch_input_tokens,
                connect_timeout_seconds=draft.connect_timeout_seconds,
                read_timeout_seconds=draft.read_timeout_seconds,
                write_timeout_seconds=draft.write_timeout_seconds,
                pool_timeout_seconds=draft.pool_timeout_seconds,
                max_request_bytes=draft.max_request_bytes,
                max_response_bytes=draft.max_response_bytes,
            )
        except (TypeError, ValueError) as exc:
            raise AiSettingsValidationError(str(exc)) from exc

    def _decrypt_secret(
        self,
        provider: ModelProvider,
        secret: AiProviderSecretRecord,
    ) -> str:
        return self._cipher.decrypt(
            provider,
            secret.ciphertext,
            secret.nonce,
            secret.key_version,
        )

    @staticmethod
    def _configuration_fingerprint(settings: ModelServiceSettings) -> str:
        identity = {
            "provider": settings.provider.value,
            "api_protocol": settings.resolved_api_protocol.value,
            "model": settings.model,
            "api_base_url": settings.resolved_api_base_url,
            "reasoning_effort": settings.reasoning_effort.value,
            "api_key_sha256": sha256(settings.api_key.encode("utf-8")).hexdigest(),
            "context_window_tokens": settings.context_window_tokens,
            "max_batch_input_tokens": settings.max_batch_input_tokens,
            "max_output_tokens": settings.max_output_tokens,
            "timeouts": [
                settings.connect_timeout_seconds,
                settings.read_timeout_seconds,
                settings.write_timeout_seconds,
                settings.pool_timeout_seconds,
            ],
            "limits": [settings.max_request_bytes, settings.max_response_bytes],
            "pricing": (
                {
                    "input_usd_per_million": str(
                        settings.pricing.input_usd_per_million
                    ),
                    "output_usd_per_million": str(
                        settings.pricing.output_usd_per_million
                    ),
                    "cache_read_usd_per_million": (
                        str(settings.pricing.cache_read_usd_per_million)
                        if settings.pricing.cache_read_usd_per_million is not None
                        else None
                    ),
                    "cache_write_usd_per_million": (
                        str(settings.pricing.cache_write_usd_per_million)
                        if settings.pricing.cache_write_usd_per_million is not None
                        else None
                    ),
                }
                if settings.pricing is not None
                else None
            ),
        }
        encoded = json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


class SqlAlchemyAiRuntimeProvider:
    """按配置 revision 缓存旧版或固定多 Agent 模型运行时。"""

    def __init__(
        self,
        service: AiSettingsService,
        agent_settings_service: AgentSettingsService | None = None,
        *,
        max_agent_concurrency: int = 3,
    ) -> None:
        if not 1 <= max_agent_concurrency <= 3:
            raise ValueError("Agent 并发上限必须在 1 到 3 之间")
        self._service = service
        self._agent_settings_service = agent_settings_service
        self._max_agent_concurrency = max_agent_concurrency
        self._lock = RLock()
        self._cached: ActiveAiRuntime | None = None

    def current(self) -> ActiveAiRuntime | None:
        settings = self._service.active_settings()
        agent_view = (
            self._agent_settings_service.get()
            if self._agent_settings_service is not None
            else None
        )
        configured_agents = (
            tuple(item for item in agent_view.agents if item.configured)
            if agent_view is not None
            else ()
        )
        ready_agents = (
            {
                item.agent
                for item in agent_view.agents
                if item.configured
                and item.enabled
                and item.test_status == "succeeded"
                and item.api_key_configured
            }
            if agent_view is not None
            else set()
        )
        required_agents = set(ReviewAgent)
        revision = max(
            settings.revision if settings is not None else 0,
            agent_view.revision if agent_view is not None else 0,
        )
        with self._lock:
            if settings is None and not configured_agents:
                self._close_cached()
                return None
            # 一旦开始配置新版 Agent，就必须四个节点全部就绪。部分配置不能
            # 静默退回旧单模型，也不能让某个 Agent 代替缺失节点。
            if configured_agents and ready_agents != required_agents:
                self._close_cached()
                return None
            if self._cached is not None and self._cached.revision == revision:
                return self._cached
            use_agent_workflow = bool(configured_agents)
            reviewer = (
                create_model_reviewer(settings.model)
                if settings is not None and not use_agent_workflow
                else None
            )
            if settings is not None:
                planning = settings.planning
            else:
                snapshot = self._service.get()
                planning = ReviewPlanningSettings(
                    max_units=snapshot.max_units,
                    max_scope_depth=snapshot.max_scope_depth,
                    max_unit_input_bytes=snapshot.max_unit_input_bytes,
                    max_total_input_bytes=snapshot.max_total_input_bytes,
                    model_budget=ModelBudgetPolicy(
                        max_http_calls=snapshot.max_model_http_calls,
                        max_input_tokens=snapshot.max_model_input_tokens,
                        max_output_tokens=snapshot.max_model_output_tokens,
                        max_estimated_cost_microusd=(
                            snapshot.max_model_cost_microusd
                        ),
                        max_duration_seconds=snapshot.max_model_duration_seconds,
                    ),
                )
            agent_workflow = None
            if use_agent_workflow and self._agent_settings_service is not None:
                from services.agent_workflow import FixedAgentWorkflow

                agent_settings = self._agent_settings_service.model_settings()
                if set(agent_settings) != required_agents:
                    self._close_cached()
                    return None
                reviewers = {
                    agent: create_model_reviewer(model_settings)
                    for agent, model_settings in agent_settings.items()
                }

                agent_workflow = FixedAgentWorkflow(
                    reviewers,
                    summary_reviewer=reviewers.get(ReviewAgent.SUMMARY),
                    agent_settings=agent_settings,
                    max_concurrency=self._max_agent_concurrency,
                )
            runtime = ActiveAiRuntime(
                revision=revision,
                reviewer=reviewer,
                planner=DeterministicReviewPlanner(planning),
                model_settings=settings.model if settings is not None else None,
                agent_workflow=agent_workflow,
            )
            self._close_cached()
            self._cached = runtime
            return runtime

    def close(self) -> None:
        with self._lock:
            self._close_cached()

    def _close_cached(self) -> None:
        if self._cached is not None:
            if self._cached.agent_workflow is not None:
                self._cached.agent_workflow.close()
            if self._cached.reviewer is not None:
                self._cached.reviewer.close()
            self._cached = None


def _test_model_connection(settings: ModelServiceSettings) -> None:
    reviewer = create_model_reviewer(settings)
    try:
        reviewer.review(_connection_test_input())
    finally:
        reviewer.close()


def _connection_test_input() -> ModelReviewInput:
    head_sha = "0" * 40
    patch = "@@ -0,0 +1 @@\n+connection_test = True"
    unit = ReviewUnit(
        unit_key=sha256(b"openreviewer-ai-connection-test-unit").hexdigest(),
        review_version_key=build_review_version_key(1, 1, head_sha),
        head_sha=head_sha,
        file="connection_test.py",
        blob_sha="1" * 40,
        language="python",
        patch=patch,
        patch_sha256=sha256(patch.encode("utf-8")).hexdigest(),
        rule_paths=(),
        estimated_input_bytes=len(patch.encode("utf-8")),
        planner_version="connection-test-v1",
    )
    return ModelReviewInput(
        review_plan_id="connection-test",
        review_run_id="connection-test",
        plan_fingerprint=sha256(b"openreviewer-ai-connection-test-plan").hexdigest(),
        planner_version="connection-test-v1",
        review_version_key=unit.review_version_key,
        repository_id=1,
        repository="openreviewer/connection-test",
        pull_request_number=1,
        head_sha=head_sha,
        rules=(),
        units=(unit,),
        total_estimated_input_bytes=unit.estimated_input_bytes,
    )
