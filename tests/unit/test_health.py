import asyncio

import httpx
import pytest

from apps.api.main import create_app


async def get_from_app(path: str) -> httpx.Response:
    """通过内存 ASGI 传输向新建 API 应用发送 GET 请求。

    参数：
        path: 要访问的应用相对路径。

    返回：
        httpx 响应对象，包含状态码、响应头和响应体。

    该辅助函数不绑定端口、不启动 Uvicorn，也不读取真实浏览器状态；它直接调用
    ASGI 应用，适合验证路由和中间件。每次调用创建独立客户端并在退出时关闭。
    """
    transport = httpx.ASGITransport(app=create_app())
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
