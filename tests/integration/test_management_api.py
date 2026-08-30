import asyncio
import base64
from pathlib import Path

import httpx
import pytest

from apps.api.main import create_app
from domain.models import ReviewRequest
from domain.security import ErrorCode, SafeApplicationError, SafeError
from persistence.auth import SqlAlchemySessionStore
from persistence.dashboard import SqlAlchemyDashboardRepository
from persistence.database import Database
from persistence.models import Base, ReviewTaskRecord
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.review_management import SqlAlchemyReviewManagementRepository
from services.agent_settings import AgentSettingsService
from services.ai_settings import AiSecretCipher, AiSettingsService
from services.auth import AuthService, AuthSettings, UserCredential
from services.dashboard import DashboardService
from services.rbac import AccessRole, ResourceScope
from services.review_management import ReviewManagementService
from services.reviews import ReviewService
from tests.support import (
    TEST_GITHUB_ACCESS_POLICY,
    TEST_HASHER,
    TEST_PASSWORD,
    TEST_USERNAME,
    make_auth_service,
)


@pytest.fixture
def database(tmp_path: Path):
    """为每个管理 API 用例创建隔离的临时数据库。

    参数：
        tmp_path: pytest 为当前用例分配的临时目录。

    产生：
        已创建全部 ORM 表的 ``Database``，测试结束后无论成功失败都会释放连接池。

    使用 SQLite 是为了验证完整 SQLAlchemy/API 协作而不依赖外部 PostgreSQL；
    PostgreSQL 专属迁移/锁语义由迁移和队列契约另行覆盖。
    """
    path = (tmp_path / "management.sqlite3").as_posix()
    configured = Database.connect(f"sqlite:///{path}")
    Base.metadata.create_all(configured.engine)
    try:
        yield configured
    finally:
        configured.dispose()


def application_for(database: Database):
    """组装管理 API 集成测试使用的完整应用。

    参数：
        database: fixture 创建的隔离数据库。

    返回：
        注入真实 SQLAlchemy 审查仓储、真实 Dashboard 查询服务和测试认证服务的
        FastAPI 实例；不会读取当前机器的生产环境变量。
    """
    return create_app(
        ReviewService(SqlAlchemyReviewRepository(database.sessions)),
        auth_service=make_auth_service(
            session_store=SqlAlchemySessionStore(database.sessions)
        ),
        dashboard_service=DashboardService(
            SqlAlchemyDashboardRepository(database.sessions)
        ),
        review_management_service=ReviewManagementService(
            SqlAlchemyReviewManagementRepository(database.sessions)
        ),
        github_access_policy=TEST_GITHUB_ACCESS_POLICY,
    )


def role_application_for(database: Database, role: AccessRole):
    """创建带指定非管理员角色的真实登录与权限集成测试应用。"""

    username = f"test-{role.value}"
    auth_service = AuthService(
        AuthSettings(
            username=TEST_USERNAME,
            password_hash=TEST_HASHER.hash(TEST_PASSWORD),
            session_secret=b"test-session-secret-is-at-least-32-bytes-long",
            cookie_secure=False,
            additional_users=(
                UserCredential(
                    username=username,
                    password_hash=TEST_HASHER.hash(TEST_PASSWORD),
                    role=role,
                ),
            ),
        ),
        password_hasher=TEST_HASHER,
    )
    return (
        create_app(
            ReviewService(SqlAlchemyReviewRepository(database.sessions)),
            auth_service=auth_service,
            dashboard_service=DashboardService(
                SqlAlchemyDashboardRepository(database.sessions)
            ),
            github_access_policy=TEST_GITHUB_ACCESS_POLICY,
        ),
        username,
    )


@pytest.mark.parametrize(
    ("role", "action", "expected_status"),
    [
        (AccessRole.VIEWER, "approve", 403),
        (AccessRole.VIEWER, "publish", 403),
        (AccessRole.VIEWER, "cancel", 403),
        (AccessRole.ADJUDICATOR, "approve", 503),
        (AccessRole.ADJUDICATOR, "publish", 403),
        (AccessRole.ADJUDICATOR, "cancel", 403),
        (AccessRole.PUBLISHER, "approve", 403),
        (AccessRole.PUBLISHER, "publish", 503),
        (AccessRole.PUBLISHER, "cancel", 403),
    ],
)
def test_role_action_permission_matrix(
    database: Database,
    role: AccessRole,
    action: str,
    expected_status: int,
) -> None:
    application, username = role_application_for(database, role)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            logged_in = await client.post(
                "/api/v1/auth/login",
                json={"username": username, "password": TEST_PASSWORD},
            )
            assert logged_in.status_code == 200
            assert logged_in.json()["role"] == role.value

            readable = await client.get("/api/v1/dashboard")
            assert readable.status_code == 200

            response = await client.post(
                "/api/v1/reviews/missing-run/actions",
                headers={"Idempotency-Key": f"rbac-{role.value}-{action}"},
                json={"action": action},
            )
            assert response.status_code == expected_status

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("role", "expected_finding_status"),
    [
        (AccessRole.VIEWER, 403),
        (AccessRole.ADJUDICATOR, 503),
        (AccessRole.PUBLISHER, 403),
    ],
)
def test_role_endpoint_permission_matrix(
    database: Database,
    role: AccessRole,
    expected_finding_status: int,
) -> None:
    application, username = role_application_for(database, role)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            logged_in = await client.post(
                "/api/v1/auth/login",
                json={"username": username, "password": TEST_PASSWORD},
            )
            assert logged_in.status_code == 200

            settings = await client.get("/api/v1/settings/ai")
            assert settings.status_code == 403

            create = await client.post(
                "/api/v1/reviews",
                headers={"Idempotency-Key": f"rbac-create-{role.value}"},
                json={
                    "installation_id": 10,
                    "repository_id": 42,
                    "repository": "lboverfys/NiuMa",
                    "pull_request_number": 128,
                    "head_sha": "a" * 40,
                },
            )
            assert create.status_code == 403

            finding = await client.post(
                "/api/v1/reviews/missing-run/findings/missing-finding",
                headers={"Idempotency-Key": f"rbac-finding-{role.value}"},
                json={"decision": "valid"},
            )
            assert finding.status_code == expected_finding_status

    asyncio.run(exercise())


async def exercise_login_and_dashboard(application) -> None:
    """在同一浏览器会话中验证完整管理操作链路。

    参数：
        application: 已注入隔离数据库和测试凭据的 FastAPI 应用。

    流程与预期：
        未登录 Dashboard 返回 401；错误密码返回统一 401；正确登录设置 HttpOnly、
        SameSite Cookie 且不含明文密码；随后可以读取当前用户、创建任务、在
        Dashboard/列表看到任务和“暂无 Worker”状态；注销后同一客户端再次访问
        ``/auth/me`` 必须恢复为 401。

    使用同一个 httpx 客户端是为了让 Cookie jar 模拟真实浏览器自动保存/发送 Cookie。
    """
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        unauthenticated = await client.get("/api/v1/dashboard")
        assert unauthenticated.status_code == 401

        wrong = await client.post(
            "/api/v1/auth/login",
            json={"username": TEST_USERNAME, "password": "incorrect"},
        )
        assert wrong.status_code == 401
        assert wrong.json() == {"detail": "invalid username or password"}

        login = await client.post(
            "/api/v1/auth/login",
            json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        )
        assert login.status_code == 200
        cookie = login.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert TEST_PASSWORD not in cookie
        copied_session = client.cookies.get("openreviewer_session")
        assert copied_session is not None

        current_user = await client.get("/api/v1/auth/me")
        assert current_user.status_code == 200
        assert current_user.json()["username"] == TEST_USERNAME

        created = await client.post(
            "/api/v1/reviews",
            headers={"Idempotency-Key": "dashboard-task-001"},
            json={
                "installation_id": 10,
                "repository_id": 42,
                "repository": "lboverfys/NiuMa",
                "pull_request_number": 128,
                "head_sha": "a" * 40,
            },
        )
        assert created.status_code == 202

        dashboard = await client.get("/api/v1/dashboard")
        assert dashboard.status_code == 200
        body = dashboard.json()
        assert body["total_reviews"] == 1
        assert body["status_counts"]["queued"] == 1
        assert body["worker"] == {
            "configured": False,
            "online": False,
            "worker_id": None,
            "status": None,
            "current_task_id": None,
            "started_at": None,
            "last_seen_at": None,
        }
        assert body["recent_reviews"][0]["repository"] == "lboverfys/NiuMa"

        listed = await client.get("/api/v1/reviews?limit=10")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1

        logout = await client.post("/api/v1/auth/logout")
        assert logout.status_code == 204
        assert (await client.get("/api/v1/auth/me")).status_code == 401

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={"openreviewer_session": copied_session},
        ) as copied_cookie_client:
            copied_cookie_response = await copied_cookie_client.get("/api/v1/auth/me")
            assert copied_cookie_response.status_code == 401


def test_login_cookie_and_authenticated_dashboard(database: Database) -> None:
    """把异步管理链路作为同步 pytest 用例执行。

    参数：
        database: 当前用例的隔离数据库。

    该入口本身只负责组装应用并运行 ``exercise_login_and_dashboard``；所有断言集中
    在异步辅助函数中，确保客户端生命周期和 Cookie 状态处于同一个事件循环。
    """
    asyncio.run(exercise_login_and_dashboard(application_for(database)))


def test_cross_origin_login_is_rejected(database: Database) -> None:
    """验证跨站页面不能借浏览器请求发起管理员登录。

    参数：
        database: 当前用例的隔离数据库。

    动作：使用正确凭据，但显式携带攻击者域名 Origin。
    预期：API 在凭据处理前返回 403，证明同源依赖独立于用户名/密码是否正确。
    """
    async def request() -> httpx.Response:
        """在独立异步客户端生命周期内发送恶意 Origin 请求。

        返回：
            登录端点的原始 httpx 响应，供外层同步测试断言状态码。

        客户端直接调用 ASGI 应用，不需要启动端口；base URL 决定合法 Host，显式
        Origin 则故意使用另一站点来触发防护。
        """
        transport = httpx.ASGITransport(app=application_for(database))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/api/v1/auth/login",
                headers={"Origin": "https://attacker.example"},
                json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
            )

    response = asyncio.run(request())
    assert response.status_code == 403


def test_malformed_origin_is_rejected_without_server_error(database: Database) -> None:
    """Origin 端口解析失败时也必须返回稳定的跨站拒绝。"""

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=application_for(database))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/api/v1/auth/login",
                headers={"Origin": "https://testserver:not-a-port"},
                json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
            )

    response = asyncio.run(request())
    assert response.status_code == 403


def test_dashboard_redacts_legacy_error_text_and_details(database: Database) -> None:
    fake_token = "ghp_FAKE_DASHBOARD_TOKEN_1234567890123"
    application = application_for(database)

    async def request() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
            )
            assert login.status_code == 200
            created = await client.post(
                "/api/v1/reviews",
                headers={"Idempotency-Key": "dashboard-redaction"},
                json={
                    "installation_id": 10,
                    "repository_id": 42,
                    "repository": "lboverfys/NiuMa",
                    "pull_request_number": 129,
                    "head_sha": "b" * 40,
                },
            )
            assert created.status_code == 202
            with database.sessions() as session:
                task = session.get(
                    ReviewTaskRecord, created.json()["review_task_id"]
                )
                task.last_error = f"Authorization: Bearer {fake_token}"
                task.last_error_code = "future_worker_error"
                task.last_error_retryable = True
                task.last_error_details = {"password": "plain-password"}
                session.commit()
            return (
                await client.get("/api/v1/dashboard"),
                await client.get("/api/v1/reviews?limit=10"),
            )

    dashboard, listed = asyncio.run(request())
    assert dashboard.status_code == listed.status_code == 200
    for response in (dashboard, listed):
        assert fake_token not in response.text
        assert "plain-password" not in response.text
        item = (
            response.json()["recent_reviews"][0]
            if "recent_reviews" in response.json()
            else response.json()["items"][0]
        )
        assert item["last_error_code"] == "future_worker_error"
        assert item["last_error_retryable"] is True
        assert "<redacted>" in str(item)


def test_resource_scoped_user_cannot_enumerate_other_repository_runs(
    database: Database,
) -> None:
    """资源范围必须同时约束 Dashboard 聚合和单条详情。"""

    repository = SqlAlchemyReviewRepository(database.sessions)
    visible = repository.create_or_get(
        ReviewRequest(
            installation_id=10,
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=200,
            head_sha="a" * 40,
        ),
        "scope-visible",
        "scope-visible-fingerprint",
    )
    hidden = repository.create_or_get(
        ReviewRequest(
            installation_id=10,
            repository_id=43,
            repository="other/Secret",
            pull_request_number=201,
            head_sha="b" * 40,
        ),
        "scope-hidden",
        "scope-hidden-fingerprint",
    )
    scoped_username = "scoped-viewer"
    scoped_auth = AuthService(
        AuthSettings(
            username=TEST_USERNAME,
            password_hash=TEST_HASHER.hash(TEST_PASSWORD),
            session_secret=b"test-session-secret-is-at-least-32-bytes-long",
            cookie_secure=False,
            additional_users=(
                UserCredential(
                    username=scoped_username,
                    password_hash=TEST_HASHER.hash(TEST_PASSWORD),
                    role=AccessRole.VIEWER,
                    resource_scope=ResourceScope(
                        installation_ids=frozenset({10}),
                        # 数据库保留 GitHub 的展示大小写；资源范围使用规范化键
                        # 后，仓库名大小写差异也必须仍然可见。
                        repositories=frozenset({"LBOVERFYS/NIUMA"}),
                    ),
                ),
            ),
        ),
        password_hasher=TEST_HASHER,
        session_store=SqlAlchemySessionStore(database.sessions),
    )
    application = create_app(
        ReviewService(SqlAlchemyReviewRepository(database.sessions)),
        auth_service=scoped_auth,
        dashboard_service=DashboardService(
            SqlAlchemyDashboardRepository(database.sessions)
        ),
        review_management_service=ReviewManagementService(
            SqlAlchemyReviewManagementRepository(database.sessions)
        ),
        github_access_policy=TEST_GITHUB_ACCESS_POLICY,
    )

    async def exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": scoped_username, "password": TEST_PASSWORD},
            )
            assert login.status_code == 200
            return (
                await client.get("/api/v1/dashboard"),
                await client.get(f"/api/v1/reviews/{visible.review_run_id}"),
                await client.get(f"/api/v1/reviews/{hidden.review_run_id}"),
            )

    dashboard, visible_details, hidden_details = asyncio.run(exercise())
    assert dashboard.status_code == 200
    assert dashboard.json()["total_reviews"] == 1
    assert [item["review_run_id"] for item in dashboard.json()["recent_reviews"]] == [
        visible.review_run_id
    ]
    assert visible_details.status_code == 200
    assert hidden_details.status_code == 404


def test_dynamic_ai_settings_are_authenticated_redacted_tested_and_activated(
    database: Database,
) -> None:
    """验证管理页配置链路不会把 API Key 明文或密文返回浏览器。"""

    tested_models: list[str] = []
    settings_service = AiSettingsService(
        database.sessions,
        AiSecretCipher(b"a" * 32),
        connection_tester=lambda settings: tested_models.append(settings.model),
    )
    application = create_app(
        ReviewService(SqlAlchemyReviewRepository(database.sessions)),
        auth_service=make_auth_service(),
        dashboard_service=DashboardService(
            SqlAlchemyDashboardRepository(database.sessions)
        ),
        ai_settings_service=settings_service,
    )
    api_key = "sk-test-management-secret-9876"

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            assert (await client.get("/api/v1/settings/ai")).status_code == 401
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
            )
            assert login.status_code == 200

            initial = await client.get("/api/v1/settings/ai")
            assert initial.status_code == 200
            assert initial.json()["revision"] == 0

            saved = await client.put(
                "/api/v1/settings/ai/providers/openai",
                json={
                    "expected_revision": 0,
                    "model": "gpt-5",
                    "api_protocol": "chat_completions",
                    "api_base_url": "https://relay.example.test/v1/",
                    "api_key": api_key,
                    "clear_api_key": False,
                    "context_window_tokens": 1_000_000,
                    "max_output_tokens": 8192,
                    "connect_timeout_seconds": 5,
                    "read_timeout_seconds": 180,
                    "write_timeout_seconds": 30,
                    "pool_timeout_seconds": 5,
                    "max_request_bytes": 4194304,
                    "max_response_bytes": 2097152,
                    "input_usd_per_million": "1.25",
                    "output_usd_per_million": "10",
                    "cache_read_usd_per_million": None,
                    "cache_write_usd_per_million": None,
                },
            )
            assert saved.status_code == 200
            assert api_key not in saved.text
            assert saved.json()["providers"][0]["api_key_mask"] == "****9876"
            assert saved.json()["providers"][0]["api_protocol"] == (
                "chat_completions"
            )
            assert saved.json()["providers"][0]["api_base_url"] == (
                "https://relay.example.test/v1"
            )
            assert saved.json()["providers"][0]["context_window_tokens"] == 1_000_000
            assert saved.json()["providers"][0]["reasoning_effort"] == "none"
            assert saved.json()["providers"][0]["max_batch_input_tokens"] == 64_000

            premature = await client.post(
                "/api/v1/settings/ai/providers/openai/activate",
                json={"expected_revision": 1},
            )
            assert premature.status_code == 422

            tested = await client.post(
                "/api/v1/settings/ai/providers/openai/test",
                json={"expected_revision": 1},
            )
            assert tested.status_code == 200
            assert tested.json()["revision"] == 2
            assert tested_models == ["gpt-5"]

            activated = await client.post(
                "/api/v1/settings/ai/providers/openai/activate",
                json={"expected_revision": 2},
            )
            assert activated.status_code == 200
            assert activated.json()["active_provider"] == "openai"
            assert api_key not in activated.text

            stale = await client.put(
                "/api/v1/settings/ai/review-policy",
                json={
                    "expected_revision": 1,
                    "max_units": 50,
                    "max_scope_depth": 16,
                    "max_unit_input_bytes": 131072,
                    "max_total_input_bytes": 1048576,
                },
            )
            assert stale.status_code == 409

            audits = await client.get("/api/v1/settings/audits")
            assert audits.status_code == 200
            assert api_key not in audits.text
            assert audits.json()["items"][0]["action"] == (
                "provider.openai.activated"
            )

    asyncio.run(exercise())


def test_agent_settings_are_independent_redacted_and_translate_test_failures(
    database: Database,
) -> None:
    """四个 Agent 使用独立配置，连接故障返回稳定状态且密钥不出浏览器。"""

    should_fail = False

    def test_connection(_settings) -> None:
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
        AiSecretCipher(b"d" * 32),
        connection_tester=test_connection,
    )
    application = create_app(
        ReviewService(SqlAlchemyReviewRepository(database.sessions)),
        auth_service=make_auth_service(),
        dashboard_service=DashboardService(
            SqlAlchemyDashboardRepository(database.sessions)
        ),
        agent_settings_service=service,
    )
    api_key = "sk-agent-api-secret-4321"

    async def exercise() -> None:
        nonlocal should_fail
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            assert (
                await client.get("/api/v1/settings/ai/agents")
            ).status_code == 401
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
            )
            assert login.status_code == 200

            initial = await client.get("/api/v1/settings/ai/agents")
            assert initial.status_code == 200
            assert [item["agent"] for item in initial.json()["agents"]] == [
                "security",
                "convention",
                "logic",
                "summary",
            ]

            saved = await client.put(
                "/api/v1/settings/ai/agents/security",
                json={
                    "expected_revision": 0,
                    "provider": "openai",
                    "model": "security-model",
                    "api_protocol": "chat_completions",
                    "api_base_url": "https://relay.example.test/v1/",
                    "api_key": api_key,
                },
            )
            assert saved.status_code == 200
            assert api_key not in saved.text
            security = saved.json()["agents"][0]
            assert security["api_key_mask"] == "****4321"
            assert security["enabled"] is False

            tested = await client.post(
                "/api/v1/settings/ai/agents/security/test",
                json={"expected_revision": 1},
            )
            assert tested.status_code == 200
            assert tested.json()["agents"][0]["test_status"] == "succeeded"

            enabled = await client.post(
                "/api/v1/settings/ai/agents/security/enabled",
                json={"expected_revision": 2, "enabled": True},
            )
            assert enabled.status_code == 200
            assert enabled.json()["agents"][0]["enabled"] is True

            should_fail = True
            failed = await client.post(
                "/api/v1/settings/ai/agents/security/test",
                json={"expected_revision": 3},
            )
            assert failed.status_code == 503
            assert failed.json()["detail"] == "中转站连接测试超时"
            assert api_key not in failed.text

            disabled = await client.post(
                "/api/v1/settings/ai/agents/security/enabled",
                json={"expected_revision": 4, "enabled": False},
            )
            assert disabled.status_code == 200
            security = disabled.json()["agents"][0]
            assert security["test_status"] == "failed"
            assert security["enabled"] is False
            assert api_key not in disabled.text

    asyncio.run(exercise())


def test_dynamic_ai_settings_are_lazy_loaded_when_not_injected(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产入口未注入服务时，设置接口也必须能按环境懒加载。"""

    monkeypatch.setenv(
        "OPENREVIEWER_DATABASE_URL",
        database.engine.url.render_as_string(hide_password=False),
    )
    monkeypatch.setenv(
        "OPENREVIEWER_AI_CONFIG_KEY",
        base64.urlsafe_b64encode(b"b" * 32).decode().rstrip("="),
    )
    application = create_app(auth_service=make_auth_service())

    async def exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
            )
            assert login.status_code == 200
            return await client.get("/api/v1/settings/ai")

    response = asyncio.run(exercise())
    assert response.status_code == 200
    assert response.json()["revision"] == 0
