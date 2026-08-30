from sqlalchemy import select

from domain.enums import ModelApiProtocol, ModelProvider, ReviewAgent
from persistence.database import Database
from persistence.models import AiAgentSecretRecord, AiProviderSecretRecord, Base
from services.agent_settings import AgentConfigDraft, AgentSettingsService
from services.ai_secret_rotation import AiSecretRotationService
from services.ai_settings import AiProviderDraft, AiSecretCipher, AiSettingsService


def test_ai_secrets_are_reencrypted_in_bounded_batches(tmp_path) -> None:
    path = (tmp_path / "ai-secret-rotation.sqlite3").as_posix()
    database = Database.connect(f"sqlite:///{path}")
    Base.metadata.create_all(database.engine)
    old_cipher = AiSecretCipher(b"o" * 32, key_version=1)
    try:
        provider_service = AiSettingsService(database.sessions, old_cipher)
        provider_service.update_provider(
            ModelProvider.OPENAI,
            AiProviderDraft(
                model="provider-model",
                api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
            ),
            expected_revision=0,
            actor="administrator",
            api_key="sk-provider-before-rotation",
        )
        agent_service = AgentSettingsService(database.sessions, old_cipher)
        agent_service.update(
            ReviewAgent.SECURITY,
            AgentConfigDraft(
                provider=ModelProvider.ANTHROPIC,
                model="agent-model",
                api_protocol=ModelApiProtocol.MESSAGES,
            ),
            expected_revision=1,
            actor="administrator",
            api_key="sk-agent-before-rotation",
        )

        current_cipher = AiSecretCipher(
            b"n" * 32,
            key_version=2,
            previous_keys=((1, b"o" * 32),),
        )
        rotation = AiSecretRotationService(database.sessions, current_cipher)

        first = rotation.rotate_batch(batch_size=1)
        second = rotation.rotate_batch(batch_size=1)
        final = rotation.rotate_batch(batch_size=1)

        assert first.provider_secrets == 1
        assert first.agent_secrets == 0
        assert first.complete is False
        assert second.provider_secrets == 0
        assert second.agent_secrets == 1
        assert second.complete is False
        assert final.rotated == 0
        assert final.complete is True

        with database.sessions() as session:
            provider_secret = session.scalar(select(AiProviderSecretRecord))
            agent_secret = session.scalar(select(AiAgentSecretRecord))
            assert provider_secret is not None
            assert agent_secret is not None
            assert provider_secret.key_version == agent_secret.key_version == 2
            assert current_cipher.decrypt(
                ModelProvider.OPENAI,
                provider_secret.ciphertext,
                provider_secret.nonce,
                provider_secret.key_version,
            ) == "sk-provider-before-rotation"
            assert current_cipher.decrypt(
                ModelProvider.ANTHROPIC,
                agent_secret.ciphertext,
                agent_secret.nonce,
                agent_secret.key_version,
            ) == "sk-agent-before-rotation"
    finally:
        database.dispose()
