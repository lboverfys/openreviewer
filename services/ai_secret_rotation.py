"""在事务外批量重加密供应商、Agent、检索与审查方案的凭据。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from persistence.secret_rotation import (
    SecretReplacement,
    SqlAlchemySecretRotationRepository,
)
from services.ai_settings import AiSecretCipher, AiSettingsPersistenceError


@dataclass(frozen=True, slots=True)
class AiSecretRotationBatch:
    provider_secrets: int
    agent_secrets: int
    complete: bool
    retrieval_secrets: int = 0
    profile_secrets: int = 0

    @property
    def rotated(self) -> int:
        return (
            self.provider_secrets
            + self.agent_secrets
            + self.retrieval_secrets
            + self.profile_secrets
        )


class AiSecretRotationService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        cipher: AiSecretCipher,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = SqlAlchemySecretRotationRepository(sessions)
        self._cipher = cipher
        self._clock = clock or (lambda: datetime.now(UTC))

    def rotate_batch(self, batch_size: int = 50) -> AiSecretRotationBatch:
        if not 1 <= batch_size <= 500:
            raise ValueError("batch_size 必须在 1 到 500 之间")
        try:
            originals = self._repository.read_batch(
                self._cipher.key_version, batch_size
            )
            replacements = []
            # 密文已批量取回；循环只做加密运算，不占用数据库事务或发出查询。
            for original in originals:
                plaintext = self._cipher.decrypt(
                    original.scope,
                    original.ciphertext,
                    original.nonce,
                    original.key_version,
                )
                encrypted = self._cipher.encrypt(original.scope, plaintext)
                replacements.append(
                    SecretReplacement(
                        original,
                        encrypted.ciphertext,
                        encrypted.nonce,
                        encrypted.key_version,
                    )
                )
            counts = self._repository.save_batch(tuple(replacements), self._clock())
        except SQLAlchemyError as exc:
            raise AiSettingsPersistenceError("AI 凭据重加密暂时无法完成") from exc
        changed = sum(counts.values())
        return AiSecretRotationBatch(
            provider_secrets=counts["provider"],
            agent_secrets=counts["agent"],
            retrieval_secrets=counts["retrieval"],
            profile_secrets=counts["profile"],
            complete=len(originals) < batch_size and changed == len(originals),
        )
