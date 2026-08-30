from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from persistence.database import Database
from persistence.external_actions import SqlAlchemyExternalActionStore
from persistence.models import Base, ExternalActionRecord
from services.external_actions import ExternalActionBusyError
from services.github import GitHubCallAudit


def _audit() -> GitHubCallAudit:
    return GitHubCallAudit(
        request_method="POST",
        request_path="/repos/o/r/check-runs",
        response_status=201,
        github_request_id="req-1",
        duration_ms=12,
        rate_limit_remaining=4999,
    )


def _database(tmp_path: Path) -> Database:
    database = Database.connect(f"sqlite:///{(tmp_path / 'actions.sqlite3').as_posix()}")
    Base.metadata.create_all(database.engine)
    return database


def test_external_action_store_records_success_and_audit(tmp_path: Path) -> None:
    database = _database(tmp_path)
    now = datetime(2026, 8, 30, tzinfo=UTC)
    store = SqlAlchemyExternalActionStore(
        database.sessions,
        clock=lambda: now,
        uuid_factory=lambda: "action-1",
    )
    try:
        store.acquire(
            action_key="action-key",
            review_run_id="run-1",
            action_type="check_run",
            owner="owner-1",
            request_method="POST",
            request_path="/check-runs",
        )
        store.succeed(
            action_key="action-key",
            owner="owner-1",
            remote_id="123",
            audit=_audit(),
        )
        assert (
            store.acquire(
                action_key="action-key",
                review_run_id="run-1",
                action_type="check_run",
                owner="owner-2",
                request_method="PATCH",
                request_path="/check-runs/123",
            )
            == "123"
        )
        with database.sessions() as session:
            row = session.get(ExternalActionRecord, "action-1")
            assert row is not None
            assert row.state == "succeeded"
            assert row.remote_id == "123"
            assert row.github_request_id == "req-1"
            assert row.lease_owner is None
            assert row.lease_expires_at is None
    finally:
        database.dispose()


def test_external_action_store_rejects_live_competing_lease(tmp_path: Path) -> None:
    database = _database(tmp_path)
    now = datetime(2026, 8, 30, tzinfo=UTC)
    store = SqlAlchemyExternalActionStore(
        database.sessions,
        clock=lambda: now,
        uuid_factory=lambda: "action-1",
        lease_duration=timedelta(minutes=5),
    )
    try:
        kwargs = {
            "action_key": "action-key",
            "review_run_id": "run-1",
            "action_type": "summary_comment",
            "request_method": "POST",
            "request_path": "/comments",
        }
        store.acquire(owner="owner-1", **kwargs)
        with pytest.raises(ExternalActionBusyError):
            store.acquire(owner="owner-2", **kwargs)
    finally:
        database.dispose()


def test_expired_external_action_lease_can_be_recovered(tmp_path: Path) -> None:
    database = _database(tmp_path)
    current = [datetime(2026, 8, 30, tzinfo=UTC)]
    store = SqlAlchemyExternalActionStore(
        database.sessions,
        clock=lambda: current[0],
        uuid_factory=lambda: "action-1",
        lease_duration=timedelta(seconds=10),
    )
    try:
        kwargs = {
            "action_key": "action-key",
            "review_run_id": "run-1",
            "action_type": "inline_review",
            "request_method": "POST",
            "request_path": "/reviews",
        }
        store.acquire(owner="owner-1", **kwargs)
        current[0] += timedelta(seconds=11)
        store.acquire(owner="owner-2", **kwargs)
        with database.sessions() as session:
            row = session.get(ExternalActionRecord, "action-1")
            assert row is not None
            assert row.lease_owner == "owner-2"
            assert row.attempt_count == 2
    finally:
        database.dispose()
