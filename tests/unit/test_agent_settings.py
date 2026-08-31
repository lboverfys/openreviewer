from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from domain.enums import ModelApiProtocol, ModelProvider, ReviewAgent
from domain.security import ErrorCode, SafeApplicationError, SafeError
from persistence.database import Database
from persistence.models import AiAgentSecretRecord, Base
from services.agent_settings import AgentConfigDraft, AgentSettingsService
from services.ai_settings import (
    ActiveAiSettings,
    AiConnectionTestError,
    AiSecretCipher,
    AiSettingsService,
    AiSettingsValidationError,
    SqlAlchemyAiRuntimeProvider,
)
from services.model_review import ModelServiceSettings
from services.review_planning import ReviewPlanningSettings


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
            max_output_tokens=32_768,
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
    assert tested[0].max_output_tokens == 32_768
    assert tested[0].max_response_bytes == 16 * 1024 * 1024
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
    assert settings[ReviewAgent.SECURITY].max_response_bytes == 16 * 1024 * 1024
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


def test_agent_api_base_url_change_requires_replacing_bound_secret(
    database: Database,
) -> None:
    service = AgentSettingsService(
        database.sessions,
        AiSecretCipher(b"h" * 32),
        connection_tester=lambda _settings: None,
    )
    service.update(
        ReviewAgent.SECURITY,
        AgentConfigDraft(
            provider=ModelProvider.OPENAI,
            model="relay-model",
            api_base_url="https://relay-one.example/v1",
        ),
        expected_revision=0,
        actor="administrator",
        api_key="host-bound-secret",
    )

    with pytest.raises(AiSettingsValidationError, match="API 地址"):
        service.update(
            ReviewAgent.SECURITY,
            AgentConfigDraft(
                provider=ModelProvider.OPENAI,
                model="relay-model",
                api_base_url="https://relay-two.example/v1",
            ),
            expected_revision=1,
            actor="administrator",
        )


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

    class StubReviewer:
        def __init__(self, settings: ModelServiceSettings) -> None:
            self.settings = settings
            self.closed = False

        def review(self, _review_input: object) -> object:
            raise AssertionError("本用例不应调用模型")

        def close(self) -> None:
            self.closed = True

    created: list[StubReviewer] = []

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
        # 本用例需要在写入后立即验证失效；生产默认 TTL 允许极短轮询合并。
        revision_cache_ttl_seconds=0,
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


def test_runtime_revision_fast_path_skips_full_reads_until_revision_changes() -> None:
    """相同 revision 只查轻量版本，不重复读取或解密完整配置。"""

    class LegacyStub:
        revision_value = 7
        revision_calls = 0
        active_calls = 0

        def revision(self) -> int:
            self.revision_calls += 1
            return self.revision_value

        def active_settings(self) -> None:
            self.active_calls += 1
            return None

    class AgentStub:
        get_calls = 0

        def get(self) -> SimpleNamespace:
            self.get_calls += 1
            return SimpleNamespace(revision=legacy.revision_value, agents=())

    legacy = LegacyStub()
    agents = AgentStub()
    provider = SqlAlchemyAiRuntimeProvider(
        legacy,  # type: ignore[arg-type]
        agents,  # type: ignore[arg-type]
        revision_cache_ttl_seconds=0,
    )

    assert provider.current() is None
    assert provider.current() is None
    # 首次冷启动会在读取完整快照后再做一次尾部 revision 校验；第二次
    # 调用只做轻量 revision 查询，不会重新读取配置或解密密钥。
    assert legacy.revision_calls == 3
    assert legacy.active_calls == 1
    assert agents.get_calls == 1

    legacy.revision_value = 8
    assert provider.current() is None
    assert legacy.active_calls == 2
    assert agents.get_calls == 2


def test_runtime_revision_ttl_can_skip_even_the_revision_query() -> None:
    now = 0.0

    class LegacyStub:
        revision_calls = 0

        def revision(self) -> int:
            self.revision_calls += 1
            return 0

        def active_settings(self) -> None:
            return None

    legacy = LegacyStub()
    provider = SqlAlchemyAiRuntimeProvider(
        legacy,  # type: ignore[arg-type]
        None,
        revision_cache_ttl_seconds=5,
        clock=lambda: now,
    )

    assert provider.current() is None
    now = 1
    assert provider.current() is None
    assert legacy.revision_calls == 2
    now = 5
    assert provider.current() is None
    assert legacy.revision_calls == 3


def test_runtime_closes_reviewer_when_final_revision_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """尾部 revision 查询失败时，旧版 reviewer 不能泄漏。"""

    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="test-model",
        api_key="test-key",
    )

    class LegacyStub:
        revision_calls = 0

        def revision(self) -> int:
            self.revision_calls += 1
            if self.revision_calls == 3:
                raise RuntimeError("revision unavailable")
            return 1

        def active_settings(self) -> ActiveAiSettings:
            return ActiveAiSettings(1, settings, ReviewPlanningSettings())

    class StubReviewer:
        closed = False

        def close(self) -> None:
            self.closed = True

    reviewer = StubReviewer()
    monkeypatch.setattr(
        "services.ai_settings.create_model_reviewer",
        lambda _settings: reviewer,
    )
    provider = SqlAlchemyAiRuntimeProvider(
        LegacyStub(),
        None,
        revision_cache_ttl_seconds=0,
    )

    with pytest.raises(RuntimeError, match="revision unavailable"):
        provider.current()

    assert reviewer.closed is True
    assert provider._cached is None


def test_runtime_closes_agent_workflow_when_final_revision_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """尾部 revision 查询失败时，已创建的四路 Agent 客户端都要释放。"""

    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="test-model",
        api_key="test-key",
    )

    class LegacyStub:
        revision_calls = 0

        def revision(self) -> int:
            self.revision_calls += 1
            if self.revision_calls == 5:
                raise RuntimeError("revision unavailable")
            return 1

        def active_settings(self) -> None:
            return None

        def get(self) -> SimpleNamespace:
            return SimpleNamespace(
                max_units=100,
                max_scope_depth=32,
                max_unit_input_bytes=192 * 1024,
                max_total_input_bytes=2 * 1024 * 1024,
                max_model_http_calls=64,
                max_model_input_tokens=2_000_000,
                max_model_output_tokens=250_000,
                max_model_cost_microusd=None,
                max_model_duration_seconds=3_600,
            )

    class AgentStub:
        def get(self) -> SimpleNamespace:
            return SimpleNamespace(
                revision=1,
                agents=tuple(
                    SimpleNamespace(
                        agent=agent,
                        configured=True,
                        enabled=True,
                        test_status="succeeded",
                        api_key_configured=True,
                    )
                    for agent in ReviewAgent
                ),
            )

        def model_settings(self) -> dict[ReviewAgent, ModelServiceSettings]:
            return {agent: settings for agent in ReviewAgent}

    class StubReviewer:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    created: list[StubReviewer] = []

    def create_reviewer(_settings: ModelServiceSettings) -> StubReviewer:
        reviewer = StubReviewer()
        created.append(reviewer)
        return reviewer

    monkeypatch.setattr(
        "services.ai_settings.create_model_reviewer",
        create_reviewer,
    )
    provider = SqlAlchemyAiRuntimeProvider(
        LegacyStub(),
        AgentStub(),  # type: ignore[arg-type]
        revision_cache_ttl_seconds=0,
    )

    with pytest.raises(RuntimeError, match="revision unavailable"):
        provider.current()

    assert len(created) == len(ReviewAgent)
    assert all(reviewer.closed for reviewer in created)
    assert provider._cached is None
