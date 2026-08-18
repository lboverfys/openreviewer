import asyncio
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select

from apps.api.main import create_app
from persistence.database import Database
from persistence.models import Base, OutboxEventRecord, ReviewRunRecord, ReviewTaskRecord
from persistence.repositories import SqlAlchemyReviewRepository
from services.reviews import ReviewService
from tests.support import TEST_PASSWORD, TEST_USERNAME, make_auth_service


HEAD_SHA = "a" * 40


@pytest.fixture
def database(tmp_path: Path):
    database_path = (tmp_path / "reviews.sqlite3").as_posix()
    configured_database = Database.connect(f"sqlite:///{database_path}")
    Base.metadata.create_all(configured_database.engine)
    try:
        yield configured_database
    finally:
        configured_database.dispose()


def review_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "installation_id": 10,
        "repository_id": 42,
        "repository": "lboverfys/NiuMa",
        "pull_request_number": 128,
        "head_sha": HEAD_SHA.upper(),
    }
    payload.update(overrides)
    return payload


async def post_review(
    application,
    payload: dict[str, object],
    idempotency_key: str | None,
) -> httpx.Response:
    headers = (
        {"Idempotency-Key": idempotency_key}
        if idempotency_key is not None
        else None
    )
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
        return await client.post(
            "/api/v1/reviews",
            json=payload,
            headers=headers,
        )


def app_for(database: Database):
    repository = SqlAlchemyReviewRepository(database.sessions)
    return create_app(
        ReviewService(repository),
        auth_service=make_auth_service(),
    )


def test_create_review_persists_run_task_and_outbox_atomically(
    database: Database,
) -> None:
    response = asyncio.run(
        post_review(app_for(database), review_payload(), "manual-request-001")
    )

    assert response.status_code == 202
    body = response.json()
    assert UUID(body["review_run_id"])
    assert UUID(body["review_task_id"])
    assert body["review_version_key"] == f"42:128:{HEAD_SHA}"
    assert body["execution_status"] == "queued"
    assert datetime.fromisoformat(body["accepted_at"])
    assert body["created"] is True

    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ReviewRunRecord)) == 1
        assert session.scalar(select(func.count()).select_from(ReviewTaskRecord)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEventRecord)) == 1
        event = session.scalar(select(OutboxEventRecord))
        assert event is not None
        assert event.event_type == "review.requested"
        assert event.payload == {
            "review_run_id": body["review_run_id"],
            "review_task_id": body["review_task_id"],
            "review_version_key": body["review_version_key"],
        }


def test_same_idempotency_key_returns_the_original_task(
    database: Database,
) -> None:
    application = app_for(database)
    first = asyncio.run(
        post_review(application, review_payload(), "manual-request-002")
    )
    repeated = asyncio.run(
        post_review(application, review_payload(), "manual-request-002")
    )

    assert first.status_code == repeated.status_code == 202
    assert repeated.json() == {**first.json(), "created": False}
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ReviewRunRecord)) == 1
        assert session.scalar(select(func.count()).select_from(ReviewTaskRecord)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEventRecord)) == 1


def test_idempotency_key_reuse_with_different_content_is_rejected(
    database: Database,
) -> None:
    application = app_for(database)
    first = asyncio.run(
        post_review(application, review_payload(), "manual-request-003")
    )
    conflict = asyncio.run(
        post_review(
            application,
            review_payload(head_sha="b" * 40),
            "manual-request-003",
        )
    )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json() == {
        "detail": "Idempotency-Key was already used for a different request"
    }
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ReviewRunRecord)) == 1


def test_new_idempotency_key_explicitly_creates_another_review_run(
    database: Database,
) -> None:
    application = app_for(database)
    first = asyncio.run(post_review(application, review_payload(), "rerun-001"))
    second = asyncio.run(post_review(application, review_payload(), "rerun-002"))

    assert first.status_code == second.status_code == 202
    assert first.json()["review_run_id"] != second.json()["review_run_id"]
    assert first.json()["review_task_id"] != second.json()["review_task_id"]
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ReviewRunRecord)) == 2
        assert session.scalar(select(func.count()).select_from(ReviewTaskRecord)) == 2
        assert session.scalar(select(func.count()).select_from(OutboxEventRecord)) == 2


@pytest.mark.parametrize(
    ("payload", "idempotency_key"),
    [
        (review_payload(head_sha="short"), "invalid-sha"),
        (review_payload(repository="not-a-full-name"), "invalid-repository"),
        ({**review_payload(), "unexpected": True}, "extra-field"),
        (review_payload(), None),
    ],
)
def test_invalid_review_request_is_rejected_before_persistence(
    database: Database,
    payload: dict[str, object],
    idempotency_key: str | None,
) -> None:
    response = asyncio.run(
        post_review(app_for(database), payload, idempotency_key)
    )

    assert response.status_code == 422
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ReviewRunRecord)) == 0


def test_unconfigured_persistence_returns_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (
        "OPENREVIEWER_DATABASE_URL",
        "OPENREVIEWER_DB_PASSWORD",
    ):
        monkeypatch.delenv(variable, raising=False)

    response = asyncio.run(
        post_review(
            create_app(auth_service=make_auth_service()),
            review_payload(),
            "no-database",
        )
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "review persistence is not configured"}
