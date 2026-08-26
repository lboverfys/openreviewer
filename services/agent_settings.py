"""固定多 Agent 的独立配置与运行时装配。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
import json
from threading import RLock
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ModelApiProtocol, ModelProvider, ModelReasoningEffort, ReviewAgent
from domain.security import SafeApplicationError
from persistence.models import (
    AiAgentConfigRecord,
    AiAgentSecretRecord,
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


@dataclass(frozen=True, slots=True)
class AgentSettingsView:
    revision: int
    agents: tuple[AgentConfigView, ...]


class AgentSettingsService:
    """每个 Agent 独立保存、测试和启停，密钥只保存加密结果。"""

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
                settings = session.get(AiSettingsRecord, AI_SETTINGS_ID)
                rows = list(
                    session.scalars(
                        select(AiAgentConfigRecord)
                        .where(
                            AiAgentConfigRecord.agent.in_(
                                tuple(item.value for item in AGENT_KEYS)
                            )
                        )
                        .order_by(AiAgentConfigRecord.agent.asc())
                        .limit(len(AGENT_KEYS))
                    )
                )
                secrets = {
                    row.agent: row
                    for row in session.scalars(
                        select(AiAgentSecretRecord)
                        .where(
                            AiAgentSecretRecord.agent.in_(
                                tuple(item.value for item in AGENT_KEYS)
                            )
                        )
                        .limit(len(AGENT_KEYS))
                    )
                }
                by_agent = {row.agent: row for row in rows}
                return AgentSettingsView(
                    revision=settings.revision if settings is not None else 0,
                    agents=tuple(
                        self._view(
                            agent,
                            by_agent.get(agent.value),
                            secrets.get(agent.value),
                        )
                        for agent in AGENT_KEYS
                    ),
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
                    row.provider != draft.provider.value
                    and api_key is None
                    and secret is not None
                    and not clear_api_key
                ):
                    # 密文的附加认证数据绑定供应商；切换供应商时不能复用旧密钥。
                    raise AiSettingsValidationError("切换供应商时必须同时提供新的 API Key")
                self._apply(row, draft, actor, now)
                if api_key is not None:
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
            if row is None or secret is None:
                raise AiSettingsValidationError("请先保存 Agent 参数和 API Key")
            provider = ModelProvider(row.provider)
            key = self._cipher.decrypt(provider, secret.ciphertext, secret.nonce, secret.key_version)
            draft = self._draft(row)
            model_settings = self._to_model_settings(provider, draft, key)
            fingerprint = self._fingerprint(model_settings)
        try:
            self._connection_tester(
                replace(
                    model_settings,
                    max_output_tokens=min(model_settings.max_output_tokens, 512),
                )
            )
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
                    if secret is None or row.test_status != "succeeded":
                        raise AiSettingsValidationError("启用前必须先通过连接测试")
                    provider = ModelProvider(row.provider)
                    key = self._cipher.decrypt(
                        provider,
                        secret.ciphertext,
                        secret.nonce,
                        secret.key_version,
                    )
                    fingerprint = self._fingerprint(
                        self._to_model_settings(provider, self._draft(row), key)
                    )
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
                rows = list(
                    session.scalars(
                        select(AiAgentConfigRecord)
                        .where(
                            AiAgentConfigRecord.enabled.is_(True),
                            AiAgentConfigRecord.test_status == "succeeded",
                        )
                        .order_by(AiAgentConfigRecord.agent.asc())
                        .limit(len(AGENT_KEYS))
                    )
                )
                if not rows:
                    return {}
                secrets = {
                    row.agent: row
                    for row in session.scalars(
                        select(AiAgentSecretRecord).where(
                            AiAgentSecretRecord.agent.in_(
                                [row.agent for row in rows]
                            )
                        ).limit(len(AGENT_KEYS))
                    )
                }
                result: dict[ReviewAgent, ModelServiceSettings] = {}
                for row in rows:
                    secret = secrets.get(row.agent)
                    if secret is None:
                        continue
                    provider = ModelProvider(row.provider)
                    result[ReviewAgent(row.agent)] = self._to_model_settings(
                        provider,
                        self._draft(row),
                        self._cipher.decrypt(
                            provider,
                            secret.ciphertext,
                            secret.nonce,
                            secret.key_version,
                        ),
                    )
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
                if row is None or secret is None:
                    raise AiSettingsConflictError("测试期间配置已发生变化")
                provider = ModelProvider(row.provider)
                current = self._to_model_settings(
                    provider,
                    self._draft(row),
                    self._cipher.decrypt(
                        provider,
                        secret.ciphertext,
                        secret.nonce,
                        secret.key_version,
                    ),
                )
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

    def _view(
        self,
        agent: ReviewAgent,
        row: AiAgentConfigRecord | None,
        secret: AiAgentSecretRecord | None,
    ) -> AgentConfigView:
        if row is None:
            return AgentConfigView(
                agent=agent,
                configured=False,
                enabled=False,
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
        key = None
        if secret is not None:
            provider = ModelProvider(row.provider)
            key = self._cipher.decrypt(provider, secret.ciphertext, secret.nonce, secret.key_version)
        return AgentConfigView(
            agent=agent,
            configured=True,
            enabled=row.enabled,
            provider=ModelProvider(row.provider),
            model=row.model,
            api_protocol=ModelApiProtocol(row.api_protocol),
            api_base_url=row.api_base_url,
            reasoning_effort=ModelReasoningEffort(row.reasoning_effort),
            api_key_configured=secret is not None,
            api_key_mask=f"****{key[-4:]}" if key else None,
            context_window_tokens=row.context_window_tokens,
            max_output_tokens=row.max_output_tokens,
            max_batch_input_tokens=row.max_batch_input_tokens,
            connect_timeout_seconds=row.connect_timeout_seconds,
            read_timeout_seconds=row.read_timeout_seconds,
            write_timeout_seconds=row.write_timeout_seconds,
            pool_timeout_seconds=row.pool_timeout_seconds,
            max_retries=row.max_retries,
            test_status=row.test_status or "untested",
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
