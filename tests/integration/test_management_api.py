import asyncio
from pathlib import Path

import httpx
import pytest

from apps.api.main import create_app
from persistence.dashboard import SqlAlchemyDashboardRepository
from persistence.database import Database
from persistence.models import Base
from persistence.repositories import SqlAlchemyReviewRepository
from services.dashboard import DashboardService
from services.reviews import ReviewService
from tests.support import TEST_PASSWORD, TEST_USERNAME, make_auth_service


@pytest.fixture
def database(tmp_path: Path):
    path = (tmp_path / "management.sqlite3").as_posix()
    configured = Database.connect(f"sqlite:///{path}")
    Base.metadata.create_all(configured.engine)
    try:
        yield configured
    finally:
        configured.dispose()


def application_for(database: Database):
    return create_app(
        ReviewService(SqlAlchemyReviewRepository(database.sessions)),
        auth_service=make_auth_service(),
        dashboard_service=DashboardService(
            SqlAlchemyDashboardRepository(database.sessions)
        ),
    )


async def exercise_login_and_dashboard(application) -> None:
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


def test_login_cookie_and_authenticated_dashboard(database: Database) -> None:
    asyncio.run(exercise_login_and_dashboard(application_for(database)))


def test_cross_origin_login_is_rejected(database: Database) -> None:
    async def request() -> httpx.Response:
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
