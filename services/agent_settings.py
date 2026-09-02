"""固定多 Agent 的独立配置与运行时装配。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import (
    ModelApiProtocol,
    ModelProvider,
    ModelReasoningEffort,
    ReviewAgent,
)
from domain.security import SafeApplicationError
from persistence.models import (
    AiAgentConfigRecord,
    AiAgentSecretRecord,
    AiProviderConfigRecord,
    AiProviderSecretRecord,
    AiSettingsRecord,
)
from services.ai_settings import (
    AI_SETTINGS_ID,
    AiConnectionTestError,
    AiSecretCipher,
    AiSettingsConfigurationError,
    AiSettingsConflictError,
    AiSettingsPersistenceError,
    AiSettingsValidationError,
)
from services.model_providers import create_model_reviewer
from services.model_review import (
    DEFAULT_MAX_BATCH_INPUT_TOKENS,
    ModelPricing,
    ModelReviewer,
    ModelServiceSettings,
    normalize_api_base_url,
)

AGENT_KEYS: tuple[ReviewAgent, ...] = (
    ReviewAgent.SECURITY,
    ReviewAgent.CONVENTION,
    ReviewAgent.LOGIC,
    ReviewAgent.SUMMARY,
)


def _normalized_test_status(
    value: str | None,
) -> Literal["untested", "succeeded", "failed"]:
    if value == "succeeded":
        return "succeeded"
    if value == "failed":
        return "failed"
    return "untested"


@dataclass(frozen=True, slots=True)
class AgentConfigDraft:
    provider: ModelProvider
    model: str
    api_protocol: ModelApiProtocol = ModelApiProtocol.CHAT_COMPLETIONS
    api_base_url: str | None = None
    reasoning_effort: ModelReasoningEffort = ModelReasoningEffort.NONE
    context_window_tokens: int = 128_000
    max_output_tokens: int = 8192
    max_batch_input_tokens: int = DEFAULT_MAX_BATCH_INPUT_TOKENS
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 180.0
    write_timeout_seconds: float = 30.0
    pool_timeout_seconds: float = 5.0
    max_retries: int = 2
    # 放在原有字段之后，保持旧版位置参数调用的兼容性。
    use_shared_connection: bool = False
    model_override: str | None = None


@dataclass(frozen=True, slots=True)
class AgentConfigView:
    agent: ReviewAgent
    configured: bool
    enabled: bool
    provider: ModelProvider
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
    max_retries: int
    test_status: Literal["untested", "succeeded", "failed"]
    tested_at: datetime | None
    updated_at: datetime | None
    use_shared_connection: bool = False
    model_override: str | None = None
    shared_connection_configured: bool = False
    shared_connection_ready: bool = False


@dataclass(frozen=True, slots=True)
class AgentSettingsView:
    revision: int
    agents: tuple[AgentConfigView, ...]


@dataclass(frozen=True, slots=True)
class _SharedConnection:
    """当前 AiSettingsRecord 激活的公共连接及其已解密密钥。"""

    provider: ModelProvider
    config: AiProviderConfigRecord
    secret: AiProviderSecretRecord | None
    api_key: str | None


class AgentSettingsService:
    """管理 Agent 的独立配置或当前激活的公共连接，密钥只保存加密结果。"""

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
        self._connection_tester = connection_tester or _test_connection

    def get(self) -> AgentSettingsView:
        with self._sessions() as session:
            try:
                settings, shared = self._load_settings_and_shared(session)
                agent_rows = session.execute(
                    select(AiAgentConfigRecord, AiAgentSecretRecord)
                    .outerjoin(
                        AiAgentSecretRecord,
                        AiAgentSecretRecord.agent == AiAgentConfigRecord.agent,
                    )
                    .where(
                        AiAgentConfigRecord.agent.in_(
                            tuple(item.value for item in AGENT_KEYS)
                        )
                    )
                    .order_by(AiAgentConfigRecord.agent.asc())
                    .limit(len(AGENT_KEYS))
                ).all()
                by_agent = {row.agent: (row, secret) for row, secret in agent_rows}

                def view_agent(agent: ReviewAgent) -> AgentConfigView:
                    pair = by_agent.get(agent.value)
                    return self._view(
                        agent,
                        pair[0] if pair is not None else None,
                        pair[1] if pair is not None else None,
                        shared,
                    )

                return AgentSettingsView(
                    revision=settings.revision if settings is not None else 0,
                    agents=tuple(view_agent(agent) for agent in AGENT_KEYS),
                )
            except (AiSettingsValidationError, AiSettingsPersistenceError):
                raise
            except SQLAlchemyError as exc:
                raise AiSettingsPersistenceError("Agent 配置暂时无法读取") from exc

    def update(
        self,
        agent: ReviewAgent,
        draft: AgentConfigDraft,
        *,
        expected_revision: int,
        actor: str,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> AgentSettingsView:
        if api_key is not None and clear_api_key:
            raise AiSettingsValidationError("不能同时设置并清除 API Key")
        if draft.use_shared_connection and api_key is not None:
            raise AiSettingsValidationError(
                "使用公共连接配置时不能同时保存 Agent 独立 API Key"
            )
        self._validate(agent, draft, api_key or "validation-key")
        now = self._clock()
        with self._sessions() as session:
            try:
                settings = self._lock_settings(session, expected_revision, now)
                row = session.get(AiAgentConfigRecord, agent.value)
                secret = session.get(AiAgentSecretRecord, agent.value)
                if row is None:
                    row = AiAgentConfigRecord(
                        agent=agent.value,
                        provider=draft.provider.value,
                        model=draft.model,
                        api_protocol=draft.api_protocol.value,
                        enabled=False,
                        updated_by=actor,
                        updated_at=now,
                    )
                    session.add(row)
                    session.flush()
                elif (
                    (
                        row.provider != draft.provider.value
                        or normalize_api_base_url(row.api_base_url)
                        != normalize_api_base_url(draft.api_base_url)
                    )
                    and not draft.use_shared_connection
                    and api_key is None
                    and secret is not None
                    and not clear_api_key
                ):
                    raise AiSettingsValidationError(
                        "切换供应商或 API 地址时必须同时提供新的 API Key"
                    )
                self._apply(row, draft, actor, now)
                if draft.use_shared_connection:
                    # 共享模式不再保留一份容易过期的重复密钥；切回独立模式
                    # 时管理员需要重新提供该 Agent 的密钥。
                    if secret is not None:
                        session.delete(secret)
                elif api_key is not None:
                    encrypted = self._cipher.encrypt(draft.provider, api_key)
                    if secret is None:
                        session.add(
                            AiAgentSecretRecord(
                                agent=agent.value,
                                ciphertext=encrypted.ciphertext,
                                nonce=encrypted.nonce,
                                key_version=encrypted.key_version,
                                updated_at=now,
                            )
                        )
                    else:
                        secret.ciphertext = encrypted.ciphertext
                        secret.nonce = encrypted.nonce
                        secret.key_version = encrypted.key_version
                        secret.updated_at = now
                elif clear_api_key and secret is not None:
                    session.delete(secret)
                row.test_status = None
                row.tested_configuration_fingerprint = None
                row.tested_at = None
                # 参数变化后旧连接测试不再可信，必须由管理员重新测试并启用。
                row.enabled = False
                self._commit_revision(session, settings, actor, now, f"agent.{agent.value}.updated")
                session.commit()
            except (AiSettingsConflictError, AiSettingsValidationError):
                session.rollback()
                raise
            except IntegrityError as exc:
                session.rollback()
                raise AiSettingsConflictError("配置已被其他管理员更新，请刷新后重试") from exc
            except SQLAlchemyError as exc:
                session.rollback()
                raise AiSettingsPersistenceError("Agent 配置暂时无法保存") from exc
        return self.get()

    def test(self, agent: ReviewAgent, *, expected_revision: int, actor: str) -> AgentSettingsView:
        with self._sessions() as session:
            settings = self._lock_settings(session, expected_revision, self._clock())
            row = session.get(AiAgentConfigRecord, agent.value)
            secret = session.get(AiAgentSecretRecord, agent.value)
            if row is None:
                raise AiSettingsValidationError("请先保存 Agent 参数")
            shared = self._load_shared_connection(session, settings)
            model_settings = self._effective_model_settings(row, secret, shared)
            fingerprint = self._fingerprint(model_settings)
        try:
            # 探测提示很短，实际消耗仍然很小；请求参数必须保持和正式 Agent
            # 审查一致，才能发现中转站对 32K 上限或结构化格式的限制。
            self._connection_tester(model_settings)
        except SafeApplicationError as exc:
            self._record_test(agent, expected_revision, actor, fingerprint, False)
            raise AiConnectionTestError(
                exc.error.safe_message,
                retryable=exc.error.retryable,
            ) from exc
        except Exception as exc:
            self._record_test(agent, expected_revision, actor, fingerprint, False)
            raise AiConnectionTestError(
                "模型连接测试未通过",
                retryable=True,
            ) from exc
        self._record_test(agent, expected_revision, actor, fingerprint, True)
        return self.get()

    def set_enabled(
        self,
        agent: ReviewAgent,
        enabled: bool,
        *,
        expected_revision: int,
        actor: str,
    ) -> AgentSettingsView:
        now = self._clock()
        with self._sessions() as session:
            try:
                settings = self._lock_settings(session, expected_revision, now)
                row = session.get(AiAgentConfigRecord, agent.value)
                if row is None:
                    raise AiSettingsValidationError("请先保存 Agent 配置")
                if enabled:
                    secret = session.get(AiAgentSecretRecord, agent.value)
                    shared = self._load_shared_connection(session, settings)
                    if row.test_status != "succeeded":
                        raise AiSettingsValidationError("启用前必须先通过连接测试")
                    effective = self._effective_model_settings(row, secret, shared)
                    fingerprint = self._fingerprint(effective)
                    if row.tested_configuration_fingerprint != fingerprint:
                        raise AiSettingsValidationError("Agent 配置已变化，请重新测试连接")
                row.enabled = enabled
                row.updated_by = actor
                row.updated_at = now
                self._commit_revision(session, settings, actor, now, f"agent.{agent.value}.enabled")
                session.commit()
            except (AiSettingsConflictError, AiSettingsValidationError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise AiSettingsPersistenceError("Agent 状态暂时无法保存") from exc
        return self.get()

    def model_settings(self) -> dict[ReviewAgent, ModelServiceSettings]:
        """一次批量读取已启用且测试通过的 Agent 模型配置。"""

        with self._sessions() as session:
            try:
                agent_rows = session.execute(
                    select(AiAgentConfigRecord, AiAgentSecretRecord)
                    .outerjoin(
                        AiAgentSecretRecord,
                        AiAgentSecretRecord.agent == AiAgentConfigRecord.agent,
                    )
                    .where(
                        AiAgentConfigRecord.enabled.is_(True),
                        AiAgentConfigRecord.test_status == "succeeded",
                    )
                    .order_by(AiAgentConfigRecord.agent.asc())
                    .limit(len(AGENT_KEYS))
                ).all()
                if not agent_rows:
                    return {}
                settings, shared = self._load_settings_and_shared(session)
                # ``settings`` 只用于装载公共连接；即使旧数据库没有单例行，
                # 独立 Agent 仍可按原配置返回。
                _ = settings
                result: dict[ReviewAgent, ModelServiceSettings] = {}
                for row, secret in agent_rows:
                    try:
                        result[ReviewAgent(row.agent)] = self._effective_model_settings(
                            row,
                            secret,
                            shared,
                        )
                    except AiSettingsValidationError:
                        # 共享连接尚未配置/测试时，保留其他已就绪 Agent；
                        # runtime provider 会把该节点报告为 disabled，而不是
                        # 因一个缺失密钥让整个固定 DAG 无法启动。
                        continue
                return result
            except (AiSettingsValidationError, AiSettingsConfigurationError):
                raise
            except SQLAlchemyError as exc:
                raise AiSettingsPersistenceError("Agent 配置暂时无法读取") from exc

    def reviewers(self) -> dict[ReviewAgent, ModelReviewer]:
        """一次有界读取后创建已启用且测试通过的 Agent 适配器。"""

        return {
            agent: create_model_reviewer(settings)
            for agent, settings in self.model_settings().items()
        }

    def _record_test(
        self,
        agent: ReviewAgent,
        expected_revision: int,
        actor: str,
        fingerprint: str,
        succeeded: bool,
    ) -> None:
        now = self._clock()
        with self._sessions() as session:
            try:
                settings = self._lock_settings(session, expected_revision, now)
                row = session.get(AiAgentConfigRecord, agent.value)
                secret = session.get(AiAgentSecretRecord, agent.value)
                if row is None:
                    raise AiSettingsConflictError("测试期间配置已发生变化")
                shared = self._load_shared_connection(session, settings)
                current = self._effective_model_settings(row, secret, shared)
                if self._fingerprint(current) != fingerprint:
                    raise AiSettingsConflictError("测试期间配置已发生变化")
                row.test_status = "succeeded" if succeeded else "failed"
                row.tested_configuration_fingerprint = fingerprint if succeeded else None
                row.tested_at = now
                self._commit_revision(session, settings, actor, now, f"agent.{agent.value}.test")
                session.commit()
            except (
                AiSettingsConfigurationError,
                AiSettingsConflictError,
                AiSettingsValidationError,
            ):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise AiSettingsPersistenceError(
                    "连接测试结果暂时无法保存"
                ) from exc

    def _lock_settings(self, session: Session, expected_revision: int, now: datetime) -> AiSettingsRecord:
        row = session.scalar(
            select(AiSettingsRecord)
            .where(AiSettingsRecord.id == AI_SETTINGS_ID)
            .with_for_update()
        )
        if row is None:
            if expected_revision != 0:
                raise AiSettingsConflictError("配置已被其他管理员更新，请刷新后重试")
            row = AiSettingsRecord(
                id=AI_SETTINGS_ID,
                revision=0,
                max_units=100,
                max_scope_depth=32,
                max_unit_input_bytes=192 * 1024,
                max_total_input_bytes=2 * 1024 * 1024,
                updated_at=now,
            )
            session.add(row)
            session.flush()
        elif row.revision != expected_revision:
            raise AiSettingsConflictError("配置已被其他管理员更新，请刷新后重试")
        return row

    @staticmethod
    def _commit_revision(session: Session, row: AiSettingsRecord, actor: str, now: datetime, action: str) -> None:
        # Agent 配置复用现有全局 revision；审计字段只记录动作，不保存参数值。
        from persistence.models import ConfigurationAuditRecord

        row.revision += 1
        row.updated_by = actor
        row.updated_at = now
        session.add(
            ConfigurationAuditRecord(
                id=__import__("uuid").uuid4().hex,
                revision=row.revision,
                actor=actor,
                action=action,
                changed_fields=["agent"],
                created_at=now,
            )
        )

    @staticmethod
    def _apply(row: AiAgentConfigRecord, draft: AgentConfigDraft, actor: str, now: datetime) -> None:
        row.use_shared_connection = draft.use_shared_connection
        row.model_override = (
            draft.model_override.strip() or None
            if draft.use_shared_connection and draft.model_override
            else None
        )
        row.provider = draft.provider.value
        row.model = draft.model
        row.api_protocol = draft.api_protocol.value
        row.api_base_url = normalize_api_base_url(draft.api_base_url)
        row.reasoning_effort = draft.reasoning_effort.value
        row.context_window_tokens = draft.context_window_tokens
        row.max_output_tokens = draft.max_output_tokens
        row.max_batch_input_tokens = draft.max_batch_input_tokens
        row.connect_timeout_seconds = draft.connect_timeout_seconds
        row.read_timeout_seconds = draft.read_timeout_seconds
        row.write_timeout_seconds = draft.write_timeout_seconds
        row.pool_timeout_seconds = draft.pool_timeout_seconds
        row.max_retries = draft.max_retries
        row.updated_by = actor
        row.updated_at = now

    @staticmethod
    def _draft(row: AiAgentConfigRecord) -> AgentConfigDraft:
        return AgentConfigDraft(
            provider=ModelProvider(row.provider),
            model=row.model,
            use_shared_connection=bool(row.use_shared_connection),
            model_override=row.model_override,
            api_protocol=ModelApiProtocol(row.api_protocol),
            api_base_url=row.api_base_url,
            reasoning_effort=ModelReasoningEffort(row.reasoning_effort),
            context_window_tokens=row.context_window_tokens,
            max_output_tokens=row.max_output_tokens,
            max_batch_input_tokens=row.max_batch_input_tokens,
            connect_timeout_seconds=row.connect_timeout_seconds,
            read_timeout_seconds=row.read_timeout_seconds,
            write_timeout_seconds=row.write_timeout_seconds,
            pool_timeout_seconds=row.pool_timeout_seconds,
            max_retries=row.max_retries,
        )

    @staticmethod
    def _to_model_settings(provider: ModelProvider, draft: AgentConfigDraft, key: str) -> ModelServiceSettings:
        try:
            return ModelServiceSettings(
                provider=provider,
                model=draft.model,
                api_key=key,
                api_protocol=draft.api_protocol,
                api_base_url=draft.api_base_url,
                reasoning_effort=draft.reasoning_effort,
                context_window_tokens=draft.context_window_tokens,
                max_output_tokens=draft.max_output_tokens,
                max_batch_input_tokens=draft.max_batch_input_tokens,
                max_retries=draft.max_retries,
                connect_timeout_seconds=draft.connect_timeout_seconds,
                read_timeout_seconds=draft.read_timeout_seconds,
                write_timeout_seconds=draft.write_timeout_seconds,
                pool_timeout_seconds=draft.pool_timeout_seconds,
            )
        except ValueError as exc:
            raise AiSettingsValidationError(str(exc)) from exc

    def _validate(self, agent: ReviewAgent, draft: AgentConfigDraft, key: str) -> None:
        if agent not in AGENT_KEYS:
            raise AiSettingsValidationError("未知的审查 Agent")
        if not 0 <= draft.max_retries <= 10:
            raise AiSettingsValidationError("重试次数必须在 0 到 10 之间")
        self._to_model_settings(draft.provider, draft, key)
        if draft.use_shared_connection and draft.model_override:
            override = draft.model_override.strip()
            if not override:
                return
            self._to_model_settings(
                draft.provider,
                replace(draft, model=override),
                key,
            )

    def _load_settings_and_shared(
        self,
        session: Session,
    ) -> tuple[AiSettingsRecord | None, _SharedConnection | None]:
        """一次 JOIN 同时读取全局 revision 和当前公共连接。"""

        row = session.execute(
            select(
                AiSettingsRecord,
                AiProviderConfigRecord,
                AiProviderSecretRecord,
            )
            .outerjoin(
                AiProviderConfigRecord,
                AiProviderConfigRecord.provider == AiSettingsRecord.active_provider,
            )
            .outerjoin(
                AiProviderSecretRecord,
                AiProviderSecretRecord.provider == AiProviderConfigRecord.provider,
            )
            .where(AiSettingsRecord.id == AI_SETTINGS_ID)
            .limit(1)
        ).one_or_none()
        if row is None:
            return None, None
        settings, config, secret = row
        if config is None:
            return settings, None
        provider = ModelProvider(config.provider)
        key = (
            self._cipher.decrypt(
                provider,
                secret.ciphertext,
                secret.nonce,
                secret.key_version,
            )
            if secret is not None
            else None
        )
        return settings, _SharedConnection(provider, config, secret, key)

    def _load_shared_connection(
        self,
        session: Session,
        settings: AiSettingsRecord | None,
    ) -> _SharedConnection | None:
        """读取公共连接；配置行最多一条，避免循环内查询。"""

        if settings is None or not settings.active_provider:
            return None
        row = session.execute(
            select(AiProviderConfigRecord, AiProviderSecretRecord)
            .outerjoin(
                AiProviderSecretRecord,
                AiProviderSecretRecord.provider == AiProviderConfigRecord.provider,
            )
            .where(AiProviderConfigRecord.provider == settings.active_provider)
            .limit(1)
        ).one_or_none()
        if row is None:
            return None
        config, secret = row
        provider = ModelProvider(config.provider)
        key = (
            self._cipher.decrypt(
                provider,
                secret.ciphertext,
                secret.nonce,
                secret.key_version,
            )
            if secret is not None
            else None
        )
        return _SharedConnection(provider, config, secret, key)

    def _effective_model_settings(
        self,
        row: AiAgentConfigRecord,
        secret: AiAgentSecretRecord | None,
        shared: _SharedConnection | None,
    ) -> ModelServiceSettings:
        """将 Agent 行解析为实际请求配置。"""

        if row.use_shared_connection:
            if shared is None or shared.secret is None or shared.api_key is None:
                raise AiSettingsValidationError(
                    "请先在 AI 设置中保存、测试并启用公共连接配置"
                )
            if shared.config.test_status != "succeeded":
                raise AiSettingsValidationError("公共连接必须先通过连接测试")
            model = row.model_override or shared.config.model
            return self._shared_model_settings(
                shared.provider,
                shared.config,
                shared.api_key,
                model=model,
                max_retries=row.max_retries,
            )
        if secret is None:
            raise AiSettingsValidationError("请先保存 Agent 参数和 API Key")
        provider = ModelProvider(row.provider)
        key = self._cipher.decrypt(
            provider,
            secret.ciphertext,
            secret.nonce,
            secret.key_version,
        )
        return self._to_model_settings(provider, self._draft(row), key)

    @staticmethod
    def _shared_model_settings(
        provider: ModelProvider,
        config: AiProviderConfigRecord,
        api_key: str,
        *,
        model: str,
        max_retries: int,
    ) -> ModelServiceSettings:
        """用全局供应商参数组装一份 Agent 可执行配置。"""

        try:
            prices = (
                config.input_usd_per_million,
                config.output_usd_per_million,
                config.cache_read_usd_per_million,
                config.cache_write_usd_per_million,
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
                model=model,
                api_key=api_key,
                api_protocol=ModelApiProtocol(config.api_protocol),
                reasoning_effort=ModelReasoningEffort(config.reasoning_effort),
                pricing=pricing,
                context_window_tokens=config.context_window_tokens,
                max_output_tokens=config.max_output_tokens,
                max_batch_input_tokens=config.max_batch_input_tokens,
                max_retries=max_retries,
                connect_timeout_seconds=config.connect_timeout_seconds,
                read_timeout_seconds=config.read_timeout_seconds,
                write_timeout_seconds=config.write_timeout_seconds,
                pool_timeout_seconds=config.pool_timeout_seconds,
                max_request_bytes=config.max_request_bytes,
                max_response_bytes=config.max_response_bytes,
                api_base_url=normalize_api_base_url(config.api_base_url),
            )
        except (TypeError, ValueError) as exc:
            raise AiSettingsValidationError(str(exc)) from exc

    def _view(
        self,
        agent: ReviewAgent,
        row: AiAgentConfigRecord | None,
        secret: AiAgentSecretRecord | None,
        shared: _SharedConnection | None,
    ) -> AgentConfigView:
        if row is None:
            return AgentConfigView(
                agent=agent,
                configured=False,
                enabled=False,
                use_shared_connection=False,
                model_override=None,
                shared_connection_configured=shared is not None and shared.secret is not None,
                shared_connection_ready=False,
                provider=ModelProvider.OPENAI,
                model="",
                api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
                api_base_url=None,
                reasoning_effort=ModelReasoningEffort.NONE,
                api_key_configured=False,
                api_key_mask=None,
                context_window_tokens=128_000,
                max_output_tokens=8192,
                max_batch_input_tokens=DEFAULT_MAX_BATCH_INPUT_TOKENS,
                connect_timeout_seconds=5.0,
                read_timeout_seconds=180.0,
                write_timeout_seconds=30.0,
                pool_timeout_seconds=5.0,
                max_retries=2,
                test_status="untested",
                tested_at=None,
                updated_at=None,
            )
        source_shared = bool(row.use_shared_connection)
        provider = ModelProvider(row.provider)
        model = row.model
        api_protocol = ModelApiProtocol(row.api_protocol)
        api_base_url = row.api_base_url
        reasoning_effort = ModelReasoningEffort(row.reasoning_effort)
        context_window_tokens = row.context_window_tokens
        max_output_tokens = row.max_output_tokens
        max_batch_input_tokens = row.max_batch_input_tokens
        connect_timeout_seconds = row.connect_timeout_seconds
        read_timeout_seconds = row.read_timeout_seconds
        write_timeout_seconds = row.write_timeout_seconds
        pool_timeout_seconds = row.pool_timeout_seconds
        key: str | None = None
        shared_configured = False
        shared_ready = False
        test_status = _normalized_test_status(row.test_status)
        if source_shared and shared is not None:
            shared_configured = shared.secret is not None and shared.api_key is not None
            provider = shared.provider
            model = row.model_override or shared.config.model
            api_protocol = ModelApiProtocol(shared.config.api_protocol)
            api_base_url = shared.config.api_base_url
            reasoning_effort = ModelReasoningEffort(shared.config.reasoning_effort)
            context_window_tokens = shared.config.context_window_tokens
            max_output_tokens = shared.config.max_output_tokens
            max_batch_input_tokens = shared.config.max_batch_input_tokens
            connect_timeout_seconds = shared.config.connect_timeout_seconds
            read_timeout_seconds = shared.config.read_timeout_seconds
            write_timeout_seconds = shared.config.write_timeout_seconds
            pool_timeout_seconds = shared.config.pool_timeout_seconds
            key = shared.api_key
            try:
                effective = self._shared_model_settings(
                    provider,
                    shared.config,
                    key or "validation-key",
                    model=model,
                    max_retries=row.max_retries,
                )
                shared_ready = bool(
                    shared_configured
                    and shared.config.test_status == "succeeded"
                    and row.test_status == "succeeded"
                    and row.tested_configuration_fingerprint == self._fingerprint(effective)
                )
            except AiSettingsValidationError:
                shared_ready = False
            if not shared_ready and test_status == "succeeded":
                test_status = "untested"
            if shared.config.test_status == "failed":
                test_status = "failed"
        elif secret is not None:
            key = self._cipher.decrypt(
                provider,
                secret.ciphertext,
                secret.nonce,
                secret.key_version,
            )
        return AgentConfigView(
            agent=agent,
            configured=True,
            enabled=row.enabled,
            use_shared_connection=source_shared,
            model_override=row.model_override,
            shared_connection_configured=shared_configured,
            shared_connection_ready=shared_ready,
            provider=provider,
            model=model,
            api_protocol=api_protocol,
            api_base_url=api_base_url,
            reasoning_effort=reasoning_effort,
            api_key_configured=shared_configured if source_shared else secret is not None,
            api_key_mask=f"****{key[-4:]}" if key else None,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
            max_batch_input_tokens=max_batch_input_tokens,
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
            write_timeout_seconds=write_timeout_seconds,
            pool_timeout_seconds=pool_timeout_seconds,
            max_retries=row.max_retries,
            test_status=test_status,
            tested_at=row.tested_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _fingerprint(settings: ModelServiceSettings) -> str:
        identity = {
            "provider": settings.provider.value,
            "protocol": settings.resolved_api_protocol.value,
            "model": settings.model,
            "base_url": settings.resolved_api_base_url,
            "reasoning": settings.reasoning_effort.value,
            "context": settings.context_window_tokens,
            "output": settings.max_output_tokens,
            "batch": settings.max_batch_input_tokens,
            "max_retries": settings.max_retries,
            "timeouts": [
                settings.connect_timeout_seconds,
                settings.read_timeout_seconds,
                settings.write_timeout_seconds,
                settings.pool_timeout_seconds,
            ],
            "api_key_sha256": sha256(settings.api_key.encode("utf-8")).hexdigest(),
        }
        return sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _test_connection(settings: ModelServiceSettings) -> None:
    reviewer = create_model_reviewer(settings)
    try:
        # Agent 连接测试复用已有最小请求，避免新增一套 HTTP 协议实现。
        from services.ai_settings import _connection_test_input

        reviewer.review(_connection_test_input())
    finally:
        reviewer.close()
