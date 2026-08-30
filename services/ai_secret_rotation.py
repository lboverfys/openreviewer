"""按有限批次把旧版 AI API Key 重加密为当前密钥版本。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ModelProvider
from persistence.models import (
    AiAgentConfigRecord,
    AiAgentSecretRecord,
    AiProviderSecretRecord,
)
from services.ai_settings import (
    AiSecretCipher,
    AiSettingsConfigurationError,
    AiSettingsPersistenceError,
)


@dataclass(frozen=True, slots=True)
class AiSecretRotationBatch:
    provider_secrets: int
    agent_secrets: int
    complete: bool

    @property
    def rotated(self) -> int:
        return self.provider_secrets + self.agent_secrets


class AiSecretRotationService:
    """用至多两次有界查询轮换供应商和 Agent 密钥。"""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        cipher: AiSecretCipher,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._cipher = cipher
        self._clock = clock or (lambda: datetime.now(UTC))

    def rotate_batch(self, batch_size: int = 50) -> AiSecretRotationBatch:
        if not 1 <= batch_size <= 500:
            raise ValueError("batch_size 必须在 1 到 500 之间")
        now = self._clock()
        with self._sessions() as session:
            try:
                providers = list(
                    session.scalars(
                        select(AiProviderSecretRecord)
                        .where(
                            AiProviderSecretRecord.key_version
                            != self._cipher.key_version
                        )
                        .order_by(AiProviderSecretRecord.provider.asc())
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                )
                remaining = batch_size - len(providers)
                agents: list[tuple[AiAgentSecretRecord, str]] = []
                if remaining:
                    agents = list(
                        session.execute(
                            select(
                                AiAgentSecretRecord,
                                AiAgentConfigRecord.provider,
                            )
                            .join(
                                AiAgentConfigRecord,
                                AiAgentConfigRecord.agent
                                == AiAgentSecretRecord.agent,
                            )
                            .where(
                                AiAgentSecretRecord.key_version
                                != self._cipher.key_version
                            )
                            .order_by(AiAgentSecretRecord.agent.asc())
                            .limit(remaining)
                            .with_for_update(
                                skip_locked=True,
                                of=AiAgentSecretRecord,
                            )
                        ).tuples().all()
                    )

                for provider_secret in providers:
                    provider = ModelProvider(provider_secret.provider)
                    plaintext = self._cipher.decrypt(
                        provider,
                        provider_secret.ciphertext,
                        provider_secret.nonce,
                        provider_secret.key_version,
                    )
                    encrypted = self._cipher.encrypt(provider, plaintext)
                    provider_secret.ciphertext = encrypted.ciphertext
                    provider_secret.nonce = encrypted.nonce
                    provider_secret.key_version = encrypted.key_version
                    provider_secret.updated_at = now

                for agent_secret, provider_name in agents:
                    provider = ModelProvider(provider_name)
                    plaintext = self._cipher.decrypt(
                        provider,
                        agent_secret.ciphertext,
                        agent_secret.nonce,
                        agent_secret.key_version,
                    )
                    encrypted = self._cipher.encrypt(provider, plaintext)
                    agent_secret.ciphertext = encrypted.ciphertext
                    agent_secret.nonce = encrypted.nonce
                    agent_secret.key_version = encrypted.key_version
                    agent_secret.updated_at = now

                session.commit()
                rotated = len(providers) + len(agents)
                return AiSecretRotationBatch(
                    provider_secrets=len(providers),
                    agent_secrets=len(agents),
                    complete=rotated < batch_size,
                )
            except AiSettingsConfigurationError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise AiSettingsPersistenceError(
                    "AI API Key 重加密暂时无法完成"
                ) from exc
