import asyncio

import httpx
import pytest

from apps.api.main import create_app


async def get_from_app(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        return await client.get(path)


def test_healthz_returns_minimal_public_status() -> None:
    response = asyncio.run(get_from_app("/healthz"))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "status": "ok",
        "service": "openreviewer",
    }


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_documentation_is_not_public(path: str) -> None:
    response = asyncio.run(get_from_app(path))

    assert response.status_code == 404
