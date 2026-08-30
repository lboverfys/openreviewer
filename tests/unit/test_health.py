import asyncio

import httpx
import pytest

from apps.api.main import create_app
from services.operations import ReadinessSnapshot
from services.telemetry import TelemetryRegistry


async def get_from_app(path: str, application=None) -> httpx.Response:
    """通过内存 ASGI 传输向新建 API 应用发送 GET 请求。

    参数：
        path: 要访问的应用相对路径。

    返回：
        httpx 响应对象，包含状态码、响应头和响应体。

    该辅助函数不绑定端口、不启动 Uvicorn，也不读取真实浏览器状态；它直接调用
    ASGI 应用，适合验证路由和中间件。每次调用创建独立客户端并在退出时关闭。
    """
    transport = httpx.ASGITransport(app=application or create_app())
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        return await client.get(path)


def test_healthz_returns_minimal_public_status() -> None:
    """验证公开健康检查只返回固定的最小存活信息。

    动作：在没有数据库和管理员环境配置的情况下请求 ``/healthz``。
    预期：仍返回 200 JSON，且响应体只有状态和服务名；这证明健康路由采用懒加载，
    不会泄露或依赖数据库连接、用户名、密码哈希和会话密钥。
    """
    response = asyncio.run(get_from_app("/healthz"))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "status": "ok",
        "service": "openreviewer",
    }


class StubOperationsService:
    def __init__(self, snapshot: ReadinessSnapshot) -> None:
        self._snapshot = snapshot

    def readiness(self) -> ReadinessSnapshot:
        return self._snapshot

    def metrics(self) -> str:
        return "# TYPE openreviewer_workers_fresh gauge\nopenreviewer_workers_fresh 1\n"


def test_readyz_requires_database_migration_and_worker() -> None:
    ready_app = create_app(
        operations_service=StubOperationsService(
            ReadinessSnapshot(database=True, migration=True, worker=True)
        )
    )
    ready = asyncio.run(get_from_app("/readyz", ready_app))

    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "checks": {"database": "ok", "migration": "ok", "worker": "ok"},
    }

    unavailable_app = create_app(
        operations_service=StubOperationsService(
            ReadinessSnapshot(database=True, migration=False, worker=False)
        )
    )
    unavailable = asyncio.run(get_from_app("/readyz", unavailable_app))

    assert unavailable.status_code == 503
    assert unavailable.json()["status"] == "not_ready"
    assert unavailable.json()["checks"] == {
        "database": "ok",
        "migration": "failed",
        "worker": "failed",
    }


def test_metrics_returns_prometheus_text_without_authentication() -> None:
    telemetry = TelemetryRegistry()
    application = create_app(
        operations_service=StubOperationsService(
            ReadinessSnapshot(database=True, migration=True, worker=True)
        ),
        telemetry_registry=telemetry,
    )

    asyncio.run(get_from_app("/healthz", application))
    response = asyncio.run(get_from_app("/metrics", application))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "openreviewer_workers_fresh 1" in response.text
    assert (
        'openreviewer_http_server_request_duration_seconds_count{method="GET",'
        'route="/healthz",status_class="2xx"} 1'
        in response.text
    )


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_documentation_is_not_public(path: str) -> None:
    """验证生产应用关闭所有自动 API 文档入口。

    参数：
        path: 参数化传入 Swagger、ReDoc 或 OpenAPI JSON 的默认路径。

    动作：逐个向新建应用发起未认证 GET 请求。
    预期：全部返回 404，而不是 200 或 401，证明这些路由根本没有注册，攻击者
    无法借公开 Schema 快速枚举管理接口。
    """
    response = asyncio.run(get_from_app(path))

    assert response.status_code == 404
