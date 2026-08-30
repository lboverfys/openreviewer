import json
from base64 import urlsafe_b64encode
from decimal import Decimal

import pytest
from sqlalchemy import select

from domain.enums import ModelApiProtocol, ModelProvider, ModelReasoningEffort
from persistence.database import Database
from persistence.models import (
    AiProviderSecretRecord,
    Base,
    ConfigurationAuditRecord,
)
from services.ai_settings import (
    AiProviderDraft,
    AiProviderNotReadyError,
    AiSecretCipher,
    AiSettingsConfigurationError,
    AiSettingsConflictError,
    AiSettingsService,
    AiSettingsValidationError,
    ReviewPolicyDraft,
)
from services.model_review import ModelServiceSettings


@pytest.fixture
def database(tmp_path):
    path = (tmp_path / "ai-settings.sqlite3").as_posix()
    configured = Database.connect(f"sqlite:///{path}")
    Base.metadata.create_all(configured.engine)
    try:
        yield configured
    finally:
        configured.dispose()


def test_secret_cipher_authenticates_provider_and_key_version() -> None:
    cipher = AiSecretCipher(bytes(range(32)), key_version=3)
    encrypted = cipher.encrypt(ModelProvider.OPENAI, "sk-test-secret-1234")

    assert encrypted.ciphertext != b"sk-test-secret-1234"
    assert len(encrypted.nonce) == 12
    assert cipher.decrypt(
        ModelProvider.OPENAI,
        encrypted.ciphertext,
        encrypted.nonce,
        encrypted.key_version,
    ) == "sk-test-secret-1234"

    with pytest.raises(AiSettingsConfigurationError):
        cipher.decrypt(
            ModelProvider.ANTHROPIC,
            encrypted.ciphertext,
            encrypted.nonce,
            encrypted.key_version,
        )


def test_secret_cipher_loads_bounded_previous_key_ring() -> None:
    current_key = b"n" * 32
    previous_key = b"o" * 32
    encoded_current = urlsafe_b64encode(current_key).decode("ascii").rstrip("=")
    encoded_previous = urlsafe_b64encode(previous_key).decode("ascii").rstrip("=")
    old_cipher = AiSecretCipher(previous_key, key_version=4)
    encrypted = old_cipher.encrypt(ModelProvider.OPENAI, "sk-before-rotation")

    cipher = AiSecretCipher.from_environment(
        {
            "OPENREVIEWER_AI_CONFIG_KEY": encoded_current,
            "OPENREVIEWER_AI_CONFIG_KEY_VERSION": "5",
            "OPENREVIEWER_AI_CONFIG_PREVIOUS_KEYS_JSON": json.dumps(
                [{"version": 4, "key": encoded_previous}]
            ),
        }
    )

    assert cipher.decrypt(
        ModelProvider.OPENAI,
        encrypted.ciphertext,
        encrypted.nonce,
        encrypted.key_version,
    ) == "sk-before-rotation"
    assert cipher.encrypt(ModelProvider.OPENAI, "sk-current").key_version == 5
    assert encoded_current not in repr(cipher)
    assert encoded_previous not in repr(cipher)

    with pytest.raises(AiSettingsConfigurationError, match="不在当前解密密钥环"):
        cipher.decrypt(
            ModelProvider.OPENAI,
            encrypted.ciphertext,
            encrypted.nonce,
            3,
        )


def test_provider_must_be_tested_before_activation_and_worker_reads_revision(
    database: Database,
) -> None:
    tested: list[ModelServiceSettings] = []
    service = AiSettingsService(
        database.sessions,
        AiSecretCipher(b"k" * 32),
        connection_tester=tested.append,
    )

    initial = service.get()
    assert initial.revision == 0
    assert initial.active_provider is None

    saved = service.update_provider(
        ModelProvider.OPENAI,
        AiProviderDraft(
            model="gpt-5",
            api_protocol=ModelApiProtocol.RESPONSES,
            api_base_url="https://relay.example.test/v1/",
            input_usd_per_million=Decimal("1.25"),
            output_usd_per_million=Decimal("10.00"),
        ),
        expected_revision=0,
        actor="administrator",
        api_key="sk-test-secret-5678",
    )
    assert saved.revision == 1
    assert saved.providers[0].api_key_mask == "****5678"
    assert saved.providers[0].api_base_url == "https://relay.example.test/v1"
    assert saved.providers[0].test_status == "untested"

    with pytest.raises(AiProviderNotReadyError, match="必须先通过连接测试"):
        service.activate_provider(
            ModelProvider.OPENAI,
            expected_revision=1,
            actor="administrator",
        )

    tested_view = service.test_provider(
        ModelProvider.OPENAI,
        expected_revision=1,
        actor="administrator",
    )
    assert tested_view.revision == 2
    assert tested_view.providers[0].test_status == "succeeded"
    assert len(tested) == 1
    assert tested[0].provider is ModelProvider.OPENAI
    assert tested[0].resolved_api_protocol is ModelApiProtocol.RESPONSES
    assert tested[0].resolved_api_base_url == "https://relay.example.test/v1"
    assert tested[0].api_request_path("/v1/responses") == "responses"
    assert tested[0].api_key == "sk-test-secret-5678"
    assert tested[0].max_output_tokens == 512
    assert tested[0].reasoning_effort is ModelReasoningEffort.NONE
    assert tested[0].max_batch_input_tokens == 64_000

    activated = service.activate_provider(
        ModelProvider.OPENAI,
        expected_revision=2,
        actor="administrator",
    )
    assert activated.revision == 3
    assert activated.active_provider is ModelProvider.OPENAI

    active = service.active_settings()
    assert active is not None
    assert active.revision == 3
    assert active.model.model == "gpt-5"
    assert active.model.resolved_api_protocol is ModelApiProtocol.RESPONSES
    assert active.model.resolved_api_base_url == "https://relay.example.test/v1"
    assert active.model.api_key == "sk-test-secret-5678"

    updated = service.update_review_policy(
        ReviewPolicyDraft(
            max_units=50,
            max_scope_depth=16,
            max_unit_input_bytes=128 * 1024,
            max_total_input_bytes=1024 * 1024,
        ),
        expected_revision=3,
        actor="administrator",
    )
    assert updated.revision == 4
    active = service.active_settings()
    assert active is not None
    assert active.planning.max_units == 50

    with database.sessions() as session:
        secret = session.get(AiProviderSecretRecord, ModelProvider.OPENAI.value)
        assert secret is not None
        assert b"sk-test-secret-5678" not in secret.ciphertext
        audit_rows = session.scalars(
            select(ConfigurationAuditRecord).order_by(
                ConfigurationAuditRecord.revision
            )
        ).all()
        assert [row.revision for row in audit_rows] == [1, 2, 3, 4]
        assert "sk-test-secret-5678" not in str(
            [row.changed_fields for row in audit_rows]
        )
        assert "api_key" in audit_rows[0].changed_fields


def test_updating_active_provider_deactivates_it_and_rejects_stale_revision(
    database: Database,
) -> None:
    service = AiSettingsService(
        database.sessions,
        AiSecretCipher(b"z" * 32),
        connection_tester=lambda _settings: None,
    )
    draft = AiProviderDraft(
        model="claude-sonnet-4-5",
        api_protocol=ModelApiProtocol.MESSAGES,
    )
    service.update_provider(
        ModelProvider.ANTHROPIC,
        draft,
        expected_revision=0,
        actor="administrator",
        api_key="sk-ant-test-secret-0001",
    )
    service.test_provider(
        ModelProvider.ANTHROPIC,
        expected_revision=1,
        actor="administrator",
    )
    service.activate_provider(
        ModelProvider.ANTHROPIC,
        expected_revision=2,
        actor="administrator",
    )

    with pytest.raises(AiSettingsConflictError):
        service.update_provider(
            ModelProvider.ANTHROPIC,
            draft,
            expected_revision=1,
            actor="administrator",
        )

    changed = service.update_provider(
        ModelProvider.ANTHROPIC,
        AiProviderDraft(
            model="claude-opus-4-1",
            api_protocol=ModelApiProtocol.MESSAGES,
        ),
        expected_revision=3,
        actor="administrator",
    )
    assert changed.active_provider is None
    assert changed.providers[1].test_status == "untested"
    assert service.active_settings() is None


def test_switching_openai_protocol_invalidates_test_and_activation(
    database: Database,
) -> None:
    tested: list[ModelApiProtocol] = []
    service = AiSettingsService(
        database.sessions,
        AiSecretCipher(b"p" * 32),
        connection_tester=lambda settings: tested.append(
            settings.resolved_api_protocol
        ),
    )
    responses = AiProviderDraft(
        model="gpt-5",
        api_protocol=ModelApiProtocol.RESPONSES,
    )
    service.update_provider(
        ModelProvider.OPENAI,
        responses,
        expected_revision=0,
        actor="administrator",
        api_key="sk-test-protocol-secret",
    )
    service.test_provider(
        ModelProvider.OPENAI,
        expected_revision=1,
        actor="administrator",
    )
    service.activate_provider(
        ModelProvider.OPENAI,
        expected_revision=2,
        actor="administrator",
    )

    switched = service.update_provider(
        ModelProvider.OPENAI,
        AiProviderDraft(
            model="gpt-5",
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        ),
        expected_revision=3,
        actor="administrator",
    )

    assert switched.active_provider is None
    assert switched.providers[0].api_protocol is ModelApiProtocol.CHAT_COMPLETIONS
    assert switched.providers[0].test_status == "untested"
    with pytest.raises(AiProviderNotReadyError, match="必须先通过连接测试"):
        service.activate_provider(
            ModelProvider.OPENAI,
            expected_revision=4,
            actor="administrator",
        )
    service.test_provider(
        ModelProvider.OPENAI,
        expected_revision=4,
        actor="administrator",
    )
    assert tested == [
        ModelApiProtocol.RESPONSES,
        ModelApiProtocol.CHAT_COMPLETIONS,
    ]


def test_switching_api_base_url_invalidates_test_and_active_provider(
    database: Database,
) -> None:
    tested: list[str] = []
    service = AiSettingsService(
        database.sessions,
        AiSecretCipher(b"u" * 32),
        connection_tester=lambda settings: tested.append(
            settings.resolved_api_base_url
        ),
    )
    initial = AiProviderDraft(
        model="relay-model",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        api_base_url="https://relay-one.example/v1",
    )
    service.update_provider(
        ModelProvider.OPENAI,
        initial,
        expected_revision=0,
        actor="administrator",
        api_key="relay-key",
    )
    service.test_provider(
        ModelProvider.OPENAI,
        expected_revision=1,
        actor="administrator",
    )
    service.activate_provider(
        ModelProvider.OPENAI,
        expected_revision=2,
        actor="administrator",
    )

    with pytest.raises(AiSettingsValidationError, match="API 地址"):
        service.update_provider(
            ModelProvider.OPENAI,
            AiProviderDraft(
                model="relay-model",
                api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
                api_base_url="https://relay-two.example/api/v1/",
            ),
            expected_revision=3,
            actor="administrator",
        )

    changed = service.update_provider(
        ModelProvider.OPENAI,
        AiProviderDraft(
            model="relay-model",
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
            api_base_url="https://relay-two.example/api/v1/",
        ),
        expected_revision=3,
        actor="administrator",
        api_key="new-relay-key",
    )

    assert changed.active_provider is None
    assert changed.providers[0].api_base_url == "https://relay-two.example/api/v1"
    assert changed.providers[0].test_status == "untested"
    with pytest.raises(AiProviderNotReadyError, match="必须先通过连接测试"):
        service.activate_provider(
            ModelProvider.OPENAI,
            expected_revision=4,
            actor="administrator",
        )
    assert tested == ["https://relay-one.example/v1"]


def test_updating_planning_limits_keeps_test_status_and_active_provider(
    database: Database,
) -> None:
    service = AiSettingsService(
        database.sessions,
        AiSecretCipher(b"c" * 32),
        connection_tester=lambda _settings: None,
    )
    service.update_provider(
        ModelProvider.OPENAI,
        AiProviderDraft(
            model="priced-model",
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        ),
        expected_revision=0,
        actor="administrator",
        api_key="priced-key",
    )
    service.test_provider(
        ModelProvider.OPENAI,
        expected_revision=1,
        actor="administrator",
    )
    service.activate_provider(
        ModelProvider.OPENAI,
        expected_revision=2,
        actor="administrator",
    )

    updated = service.update_provider(
        ModelProvider.OPENAI,
        AiProviderDraft(
            model="priced-model",
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
            context_window_tokens=1_000_000,
            input_usd_per_million=Decimal("0.50"),
            output_usd_per_million=Decimal("2.00"),
        ),
        expected_revision=3,
        actor="administrator",
    )

    assert updated.active_provider is ModelProvider.OPENAI
    assert updated.providers[0].active is True
    assert updated.providers[0].test_status == "succeeded"
    assert updated.providers[0].context_window_tokens == 1_000_000
