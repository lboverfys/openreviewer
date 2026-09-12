"""批量读取加密信封，按旧密文比较更新，避免覆盖同时修改的凭据。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import and_, case, literal, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from persistence.models import (
    AiAgentConfigRecord,
    AiAgentSecretRecord,
    AiProviderSecretRecord,
    RetrievalSettingsRecord,
    ReviewProfileRecord,
)

SecretKind = Literal["provider", "agent", "retrieval", "profile"]


@dataclass(frozen=True, slots=True)
class SecretEnvelope:
    kind: SecretKind
    identifier: str | int
    scope: str
    ciphertext: bytes
    nonce: bytes
    key_version: int


@dataclass(frozen=True, slots=True)
class SecretReplacement:
    original: SecretEnvelope
    ciphertext: bytes
    nonce: bytes
    key_version: int


class SqlAlchemySecretRotationRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    @staticmethod
    def _read(
        session: Session,
        model,
        identifier,
        scope,
        kind: SecretKind,
        version: int,
        limit: int,
        *,
        join_agents: bool = False,
    ):
        if limit <= 0:
            return ()
        statement = select(
            identifier.label("identifier"),
            scope.label("scope"),
            model.ciphertext,
            model.nonce,
            model.key_version,
        ).where(
            model.key_version != version,
            model.ciphertext.is_not(None),
            model.nonce.is_not(None),
        )
        if join_agents:
            statement = statement.join(
                AiAgentConfigRecord, AiAgentConfigRecord.agent == model.agent
            )
        rows = session.execute(statement.order_by(identifier).limit(limit)).all()
        return tuple(
            SecretEnvelope(
                kind,
                row.identifier,
                row.scope,
                row.ciphertext,
                row.nonce,
                row.key_version,
            )
            for row in rows
        )

    def read_batch(self, version: int, limit: int) -> tuple[SecretEnvelope, ...]:
        with self.sessions() as session:
            providers = self._read(
                session,
                AiProviderSecretRecord,
                AiProviderSecretRecord.provider,
                AiProviderSecretRecord.provider,
                "provider",
                version,
                limit,
            )
            agents = self._read(
                session,
                AiAgentSecretRecord,
                AiAgentSecretRecord.agent,
                AiAgentConfigRecord.provider,
                "agent",
                version,
                limit - len(providers),
                join_agents=True,
            )
            retrieval = self._read(
                session,
                RetrievalSettingsRecord,
                RetrievalSettingsRecord.id,
                literal("retrieval_aliyun"),
                "retrieval",
                version,
                limit - len(providers) - len(agents),
            )
            profiles = self._read(
                session,
                ReviewProfileRecord,
                ReviewProfileRecord.id,
                literal("review_profile:") + ReviewProfileRecord.id,
                "profile",
                version,
                limit - len(providers) - len(agents) - len(retrieval),
            )
        return (*providers, *agents, *retrieval, *profiles)

    @staticmethod
    def _save(
        session: Session,
        model,
        identifier,
        replacements: tuple[SecretReplacement, ...],
        now: datetime,
    ) -> int:
        if not replacements:
            return 0
        conditions = [
            and_(
                identifier == item.original.identifier,
                model.ciphertext == item.original.ciphertext,
                model.key_version == item.original.key_version,
            )
            for item in replacements
        ]
        values: dict[str, object] = {
            "ciphertext": case(
                {item.original.identifier: item.ciphertext for item in replacements},
                value=identifier,
            ),
            "nonce": case(
                {item.original.identifier: item.nonce for item in replacements},
                value=identifier,
            ),
            "key_version": case(
                {item.original.identifier: item.key_version for item in replacements},
                value=identifier,
            ),
        }
        if hasattr(model, "updated_at"):
            values["updated_at"] = now
        rows = session.execute(
            update(model)
            .where(or_(*conditions))
            .values(**values)
            .returning(identifier)
            .execution_options(synchronize_session=False)
        ).all()
        return len(rows)

    def save_batch(
        self, replacements: tuple[SecretReplacement, ...], now: datetime
    ) -> dict[SecretKind, int]:
        groups = {
            kind: tuple(item for item in replacements if item.original.kind == kind)
            for kind in ("provider", "agent", "retrieval", "profile")
        }
        with self.sessions() as session, session.begin():
            providers = self._save(
                session,
                AiProviderSecretRecord,
                AiProviderSecretRecord.provider,
                groups["provider"],
                now,
            )
            agents = self._save(
                session,
                AiAgentSecretRecord,
                AiAgentSecretRecord.agent,
                groups["agent"],
                now,
            )
            retrieval = self._save(
                session,
                RetrievalSettingsRecord,
                RetrievalSettingsRecord.id,
                groups["retrieval"],
                now,
            )
            profiles = self._save(
                session,
                ReviewProfileRecord,
                ReviewProfileRecord.id,
                groups["profile"],
                now,
            )
        return {
            "provider": providers,
            "agent": agents,
            "retrieval": retrieval,
            "profile": profiles,
        }
