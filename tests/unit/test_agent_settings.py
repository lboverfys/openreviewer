from pathlib import Path

import pytest
from sqlalchemy import event

from domain.enums import ModelApiProtocol, ModelProvider, ReviewAgent
from domain.security import ErrorCode, SafeApplicationError, SafeError
from persistence.database import Database
from persistence.models import AiAgentSecretRecord, Base
from services.agent_settings import AgentConfigDraft, AgentSettingsService
from services.ai_settings import (
    AiConnectionTestError,
    AiSecretCipher,
    AiSettingsService,
    AiSettingsValidationError,
    SqlAlchemyAiRuntimeProvider,
)
from services.model_review import ModelServiceSettings


@pytest.fixture
def database(tmp_path: Path):
    path = (tmp_path / "agent-settings.sqlite3").as_posix()
    configured = Database.connect(f"sqlite:///{path}")
    Base.metadata.create_all(configured.engine)
    try:
        yield configured
    finally:
        configured.dispose()


def test_agent_configuration_is_masked_tested_enabled_and_batch_loaded(
    database: Database,
) -> None:
    tested: list[ModelServiceSettings] = []
    cipher = AiSecretCipher(b"a" * 32)
    service = AgentSettingsService(
        database.sessions,
        cipher,
        connection_tester=tested.append,
    )

    initial = service.get()
    assert initial.revision == 0
    assert tuple(item.agent for item in initial.agents) == tuple(ReviewAgent)

    api_key = "sk-agent-secret-9876"
    saved = service.update(
        ReviewAgent.SECURITY,
        AgentConfigDraft(
            provider=ModelProvider.OPENAI,
            model="security-model",
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
            api_base_url="https://relay.example.test/v1/",
            context_window_tokens=1_000_000,
            max_batch_input_tokens=64_000,
        ),
        expected_revision=0,
        actor="administrator",
        api_key=api_key,
    )
    security = saved.agents[0]
    assert saved.revision == 1
    assert security.api_key_mask == "****9876"
    assert security.api_base_url == "https://relay.example.test/v1"
    assert api_key not in repr(saved)

    with database.sessions() as session:
        secret = session.get(AiAgentSecretRecord, ReviewAgent.SECURITY.value)
        assert secret is not None
        assert api_key.encode() not in secret.ciphertext

    tested_view = service.test(
        ReviewAgent.SECURITY,
        expected_revision=1,
        actor="administrator",
    )
    assert tested_view.revision == 2
    assert tested_view.agents[0].test_status == "succeeded"
    assert tested[0].max_output_tokens == 512
    assert tested[0].api_key == api_key

    enabled = service.set_enabled(
        ReviewAgent.SECURITY,
        True,
        expected_revision=2,
        actor="administrator",
    )
    assert enabled.revision == 3
    assert enabled.agents[0].enabled is True

    selects: list[str] = []

    def capture_select(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(database.engine, "before_cursor_execute", capture_select)
    try:
        settings = service.model_settings()
    finally:
        event.remove(database.engine, "before_cursor_execute", capture_select)

    assert set(settings) == {ReviewAgent.SECURITY}
    assert settings[ReviewAgent.SECURITY].api_key == api_key
    assert len(selects) == 2


def test_failed_agent_test_is_safe_and_an_enabled_agent_can_be_disabled(
    database: Database,
) -> None:
    should_fail = False

    def test_connection(_settings: ModelServiceSettings) -> None:
        if should_fail:
            raise SafeApplicationError(
                SafeError(
                    code=ErrorCode.MODEL_TIMEOUT,
                    safe_message="中转站连接测试超时",
                    retryable=True,
                )
            )

    service = AgentSettingsService(
        database.sessions,
        AiSecretCipher(b"b" * 32),
        connection_tester=test_connection,
    )
    service.update(
        ReviewAgent.LOGIC,
        AgentConfigDraft(provider=ModelProvider.OPENAI, model="logic-model"),
        expected_revision=0,
        actor="administrator",
        api_key="secret-that-must-not-leak",
    )
    service.test(ReviewAgent.LOGIC, expected_revision=1, actor="administrator")
    service.set_enabled(
        ReviewAgent.LOGIC,
        True,
        expected_revision=2,
        actor="administrator",
    )

    should_fail = True
    with pytest.raises(AiConnectionTestError, match="中转站连接测试超时") as error:
        service.test(
            ReviewAgent.LOGIC,
            expected_revision=3,
            actor="administrator",
        )

    assert error.value.retryable is True
    failed = service.get()
    logic = next(item for item in failed.agents if item.agent is ReviewAgent.LOGIC)
    assert failed.revision == 4
    assert logic.test_status == "failed"
    assert logic.enabled is True

    disabled = service.set_enabled(
        ReviewAgent.LOGIC,
        False,
        expected_revision=4,
        actor="administrator",
    )
    logic = next(item for item in disabled.agents if item.agent is ReviewAgent.LOGIC)
    assert disabled.revision == 5
    assert logic.enabled is False


def test_provider_switch_requires_replacing_or_clearing_bound_secret(
    database: Database,
) -> None:
    service = AgentSettingsService(
        database.sessions,
        AiSecretCipher(b"e" * 32),
        connection_tester=lambda _settings: None,
    )
    openai = AgentConfigDraft(
        provider=ModelProvider.OPENAI,
        model="openai-model",
    )
    anthropic = AgentConfigDraft(
        provider=ModelProvider.ANTHROPIC,
        model="anthropic-model",
        api_protocol=ModelApiProtocol.MESSAGES,
    )
    service.update(
        ReviewAgent.CONVENTION,
        openai,
        expected_revision=0,
        actor="administrator",
        api_key="provider-bound-secret",
    )

    with pytest.raises(AiSettingsValidationError, match="必须同时提供新的 API Key"):
        service.update(
            ReviewAgent.CONVENTION,
            anthropic,
            expected_revision=1,
            actor="administrator",
        )

    cleared = service.update(
        ReviewAgent.CONVENTION,
        openai,
        expected_revision=1,
        actor="administrator",
        clear_api_key=True,
    )
    assert cleared.revision == 2
    assert cleared.agents[1].api_key_configured is False

    switched = service.update(
        ReviewAgent.CONVENTION,
        anthropic,
        expected_revision=2,
        actor="administrator",
    )
    assert switched.revision == 3
    assert switched.agents[1].provider is ModelProvider.ANTHROPIC
    assert switched.agents[1].api_key_configured is False


def test_runtime_requires_all_agents_and_preserves_concurrency_limit(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cipher = AiSecretCipher(b"c" * 32)
    agents = AgentSettingsService(
        database.sessions,
        cipher,
        connection_tester=lambda _settings: None,
    )
    revision = 0
    for agent in ReviewAgent:
        view = agents.update(
            agent,
            AgentConfigDraft(
                provider=ModelProvider.OPENAI,
                model=f"{agent.value}-model",
            ),
            expected_revision=revision,
            actor="administrator",
            api_key=f"{agent.value}-secret",
        )
        revision = view.revision
        view = agents.test(
            agent,
            expected_revision=revision,
            actor="administrator",
        )
        revision = view.revision
        view = agents.set_enabled(
            agent,
            True,
            expected_revision=revision,
            actor="administrator",
        )
        revision = view.revision

    created: list[StubReviewer] = []

    class StubReviewer:
        def __init__(self, settings: ModelServiceSettings) -> None:
            self.settings = settings
            self.closed = False

        def review(self, _review_input: object) -> object:
            raise AssertionError("本用例不应调用模型")

        def close(self) -> None:
            self.closed = True

    def create_reviewer(settings: ModelServiceSettings) -> StubReviewer:
        reviewer = StubReviewer(settings)
        created.append(reviewer)
        return reviewer

    monkeypatch.setattr(
        "services.ai_settings.create_model_reviewer",
        create_reviewer,
    )
    runtime_provider = SqlAlchemyAiRuntimeProvider(
        AiSettingsService(database.sessions, cipher),
        agents,
        max_agent_concurrency=2,
    )

    runtime = runtime_provider.current()
    assert runtime is not None
    assert runtime.agent_workflow is not None
    assert runtime.agent_workflow.max_concurrency == 2
    assert set(runtime.agent_workflow.agent_settings) == set(ReviewAgent)
    assert runtime_provider.current() is runtime
    assert len(created) == 4

    agents.set_enabled(
        ReviewAgent.SUMMARY,
        False,
        expected_revision=revision,
        actor="administrator",
    )
    assert runtime_provider.current() is None
    assert all(reviewer.closed for reviewer in created)
