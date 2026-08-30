import asyncio
import hmac
import json
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from sqlalchemy import event, func, select

from apps.api.main import create_app
from persistence.database import Database
from persistence.models import (
    Base,
    GitHubInstallationRecord,
    GitHubWebhookDeliveryRecord,
    OutboxEventRecord,
    PullRequestVersionRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.webhooks import SqlAlchemyGitHubWebhookRepository
from services.webhooks import GitHubWebhookService, GitHubWebhookSettings
from tests.support import TEST_GITHUB_ACCESS_POLICY

WEBHOOK_SECRET = b"test-webhook-secret-is-at-least-32-bytes"
FAKE_PAYLOAD_TOKEN = "github_pat_FAKE_PAYLOAD_TOKEN_123456789"


@pytest.fixture
def database(tmp_path: Path):
    database_path = (tmp_path / "webhooks.sqlite3").as_posix()
    configured = Database.connect(f"sqlite:///{database_path}")

    @event.listens_for(configured.engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(configured.engine)
    try:
        yield configured
    finally:
        configured.dispose()


def webhook_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": "opened",
        "installation": {"id": 10},
        "repository": {"id": 42, "full_name": "lboverfys/NiuMa"},
        "pull_request": {"number": 128, "head": {"sha": "a" * 40}},
        "sender": {"token": FAKE_PAYLOAD_TOKEN},
    }
    payload.update(overrides)
    return payload


def encode(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def signature(body: bytes, secret: bytes = WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret, body, sha256).hexdigest()
    return f"sha256={digest}"


def application_for(database: Database, *, max_body_bytes: int = 256 * 1024):
    repository = SqlAlchemyGitHubWebhookRepository(database.sessions)
    service = GitHubWebhookService(
        repository,
        GitHubWebhookSettings(
            secret=WEBHOOK_SECRET,
            max_body_bytes=max_body_bytes,
        ),
        TEST_GITHUB_ACCESS_POLICY,
    )
    return create_app(webhook_service=service)


async def post_webhook(
    application,
    body: bytes,
    *,
    delivery_id: str = "delivery-001",
    event_name: str = "pull_request",
    supplied_signature: str | None = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        return await client.post(
            "/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": event_name,
                "X-GitHub-Delivery": delivery_id,
                "X-Hub-Signature-256": supplied_signature or signature(body),
            },
        )


def table_count(database: Database, model: type[Base]) -> int:
    with database.sessions() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_verified_delivery_atomically_creates_review_records(
    database: Database,
) -> None:
    body = encode(webhook_payload())

    response = asyncio.run(post_webhook(application_for(database), body))

    assert response.status_code == 202
    result = response.json()
    assert result["accepted"] is True
    assert result["created"] is True
    assert result["delivery_id"] == "delivery-001"
    assert result["review_version_key"] == f"42:128:{'a' * 40}"
    assert result["execution_status"] == "queued"
    for model in (
        GitHubInstallationRecord,
        PullRequestVersionRecord,
        GitHubWebhookDeliveryRecord,
        ReviewRunRecord,
        ReviewTaskRecord,
        OutboxEventRecord,
    ):
        assert table_count(database, model) == 1

    with database.sessions() as session:
        delivery = session.get(GitHubWebhookDeliveryRecord, "delivery-001")
        event = session.scalar(select(OutboxEventRecord))
        task = session.scalar(select(ReviewTaskRecord))
        persisted = f"{delivery.payload_sha256} {event.payload} {task.last_error}"
        assert delivery.payload_sha256 == sha256(body).hexdigest()
        assert FAKE_PAYLOAD_TOKEN not in persisted


def test_duplicate_delivery_returns_original_task_without_duplicate_rows(
    database: Database,
) -> None:
    application = application_for(database)
    body = encode(webhook_payload())

    first = asyncio.run(post_webhook(application, body))
    repeated = asyncio.run(post_webhook(application, body))

    assert first.status_code == repeated.status_code == 202
    assert repeated.json() == {**first.json(), "created": False}
    assert table_count(database, GitHubWebhookDeliveryRecord) == 1
    assert table_count(database, ReviewRunRecord) == 1
    assert table_count(database, ReviewTaskRecord) == 1
    assert table_count(database, OutboxEventRecord) == 1


def test_delivery_id_reuse_with_different_body_is_rejected(database: Database) -> None:
    application = application_for(database)
    first_body = encode(webhook_payload())
    changed_body = encode(webhook_payload(action="synchronize"))

    assert asyncio.run(post_webhook(application, first_body)).status_code == 202
    conflict = asyncio.run(post_webhook(application, changed_body))

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "webhook_delivery_conflict"
    assert FAKE_PAYLOAD_TOKEN not in conflict.text
    assert table_count(database, GitHubWebhookDeliveryRecord) == 1
    assert table_count(database, ReviewRunRecord) == 1


def test_invalid_signature_and_unsupported_events_never_enter_queue(
    database: Database,
) -> None:
    application = application_for(database)
    body = encode(webhook_payload())

    invalid = asyncio.run(
        post_webhook(
            application,
            body,
            supplied_signature=f"sha256={'0' * 64}",
        )
    )
    unsupported = asyncio.run(
        post_webhook(
            application,
            body,
            delivery_id="delivery-002",
            event_name="issues",
        )
    )
    unsupported_action_body = encode(webhook_payload(action="closed"))
    unsupported_action = asyncio.run(
        post_webhook(
            application,
            unsupported_action_body,
            delivery_id="delivery-003",
        )
    )

    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "webhook_invalid_signature"
    assert unsupported.status_code == 202
    assert unsupported.json()["reason"] == "unsupported_event"
    assert unsupported_action.status_code == 202
    assert unsupported_action.json()["reason"] == "unsupported_action"
    assert table_count(database, GitHubWebhookDeliveryRecord) == 0
    assert table_count(database, ReviewTaskRecord) == 0


def test_webhook_secret_rotation_accepts_previous_secret(
    database: Database,
) -> None:
    """Webhook 密钥轮换期间，旧密钥签名仍可完成一次可信投递。"""

    previous_secret = b"previous-webhook-secret-is-at-least-32-bytes"
    repository = SqlAlchemyGitHubWebhookRepository(database.sessions)
    service = GitHubWebhookService(
        repository,
        GitHubWebhookSettings(
            secret=WEBHOOK_SECRET,
            previous_secrets=(previous_secret,),
        ),
        TEST_GITHUB_ACCESS_POLICY,
    )
    body = encode(webhook_payload())

    response = asyncio.run(
        post_webhook(
            create_app(webhook_service=service),
            body,
            supplied_signature=signature(body, previous_secret),
        )
    )

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert table_count(database, GitHubWebhookDeliveryRecord) == 1


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"installation": {"id": 11}}, "installation_not_allowed"),
        (
            {"repository": {"id": 42, "full_name": "outside/other"}},
            "repository_not_allowed",
        ),
    ],
)
def test_signed_but_unapproved_source_is_ignored_before_persistence(
    database: Database,
    overrides: dict[str, object],
    reason: str,
) -> None:
    body = encode(webhook_payload(**overrides))

    response = asyncio.run(
        post_webhook(application_for(database), body, delivery_id=f"denied-{reason}")
    )

    assert response.status_code == 202
    assert response.json()["accepted"] is False
    assert response.json()["reason"] == reason
    assert table_count(database, GitHubWebhookDeliveryRecord) == 0
    assert table_count(database, ReviewTaskRecord) == 0


def test_invalid_or_oversized_payload_is_rejected_before_persistence(
    database: Database,
) -> None:
    application = application_for(database, max_body_bytes=1024)
    invalid_body = encode({"action": "opened"})
    invalid_action_body = encode(webhook_payload(action={"unexpected": True}))
    oversized_body = encode(
        webhook_payload(extra="x" * 1200)
    )

    invalid = asyncio.run(
        post_webhook(application, invalid_body, delivery_id="delivery-invalid")
    )
    oversized = asyncio.run(
        post_webhook(application, oversized_body, delivery_id="delivery-large")
    )
    invalid_action = asyncio.run(
        post_webhook(
            application,
            invalid_action_body,
            delivery_id="delivery-invalid-action",
        )
    )

    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "webhook_invalid_payload"
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "webhook_payload_too_large"
    assert invalid_action.status_code == 400
    assert invalid_action.json()["error"]["code"] == "webhook_invalid_payload"
    assert table_count(database, GitHubWebhookDeliveryRecord) == 0
    assert table_count(database, ReviewTaskRecord) == 0


def test_unconfigured_webhook_returns_structured_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (
        "OPENREVIEWER_GITHUB_WEBHOOK_SECRET",
        "OPENREVIEWER_GITHUB_WEBHOOK_SECRET_FILE",
        "OPENREVIEWER_DATABASE_URL",
        "OPENREVIEWER_DB_PASSWORD",
    ):
        monkeypatch.delenv(variable, raising=False)
    body = encode(webhook_payload())

    response = asyncio.run(post_webhook(create_app(), body))

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "webhook_not_configured",
            "message": "GitHub Webhook 接入尚未完成配置",
            "retryable": True,
        }
    }
