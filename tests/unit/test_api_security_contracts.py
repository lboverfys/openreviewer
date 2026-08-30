"""API 路由层必须长期保持的安全边界。"""

import asyncio

import httpx
from fastapi.routing import APIRoute

from apps.api.main import create_app
from tests.support import make_auth_service


def test_authenticated_mutation_routes_require_same_origin() -> None:
    application = create_app()
    mutation_methods = {"POST", "PUT", "PATCH", "DELETE"}
    missing: list[str] = []

    for route in application.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = route.methods & mutation_methods
        if not methods or not route.path.startswith("/api/v1/"):
            continue
        dependency_names = {
            getattr(dependency.call, "__name__", "")
            for dependency in route.dependant.dependencies
        }
        if "require_same_origin" not in dependency_names:
            missing.append(f"{','.join(sorted(methods))} {route.path}")

    assert missing == []


def test_trusted_compose_proxy_headers_are_used_for_https_origin(
    monkeypatch,
) -> None:
    """Docker edge 网段的 Nginx 转发头应让 HTTPS 管理请求通过同源校验。"""

    monkeypatch.setenv(
        "OPENREVIEWER_TRUSTED_PROXY_CIDRS",
        "127.0.0.1/32,::1/128,172.23.0.0/16",
    )
    application = create_app(auth_service=make_auth_service())

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(
            app=application,
            client=("172.23.4.10", 43120),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://api:18090",
        ) as client:
            return await client.post(
                "/api/v1/auth/login",
                headers={
                    "Origin": "https://openreviewer.example",
                    "Host": "openreviewer.example",
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "openreviewer.example",
                },
                json={
                    "username": "test-administrator",
                    "password": "test-only-password",
                },
            )

    response = asyncio.run(request())
    assert response.status_code == 200


def test_untrusted_proxy_cannot_supply_forwarded_https_headers(monkeypatch) -> None:
    """非 edge 来源即使伪造转发头也不能绕过内部 URL。"""

    monkeypatch.setenv(
        "OPENREVIEWER_TRUSTED_PROXY_CIDRS",
        "127.0.0.1/32,::1/128,172.23.0.0/16",
    )
    application = create_app(auth_service=make_auth_service())

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(
            app=application,
            client=("10.99.0.8", 43120),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://api:18090",
        ) as client:
            return await client.post(
                "/api/v1/auth/login",
                headers={
                    "Origin": "https://openreviewer.example",
                    "Host": "openreviewer.example",
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "openreviewer.example",
                },
                json={
                    "username": "test-administrator",
                    "password": "test-only-password",
                },
            )

    response = asyncio.run(request())
    assert response.status_code == 403
