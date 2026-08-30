import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI

from apps.api.routes.dashboard import register_dashboard_routes
from services.auth import SessionPrincipal
from services.dashboard_stream import (
    DashboardStreamCoordinator,
    DashboardStreamRegistry,
    DashboardStreamUnavailable,
    DashboardStreamUpdate,
)
from services.rbac import AccessRole, ResourceScope


def test_coordinator_reuses_poll_and_snapshot_within_interval() -> None:
    now = 10.0
    token_calls = 0
    snapshot_calls = 0

    def clock() -> float:
        return now

    def load_token() -> str:
        nonlocal token_calls
        token_calls += 1
        return "revision-1"

    def load_snapshot() -> dict[str, int]:
        nonlocal snapshot_calls
        snapshot_calls += 1
        return {"revision": snapshot_calls}

    coordinator = DashboardStreamCoordinator(
        load_token,
        load_snapshot,
        poll_interval_seconds=2,
        clock=clock,
    )

    first = coordinator.poll()
    second = coordinator.poll()

    assert first == second
    assert first.snapshot == {"revision": 1}
    assert token_calls == 1
    assert snapshot_calls == 1


def test_coordinator_only_reloads_snapshot_when_token_changes() -> None:
    now = 10.0
    token = "revision-1"
    snapshot_calls = 0

    def clock() -> float:
        return now

    def load_snapshot() -> str:
        nonlocal snapshot_calls
        snapshot_calls += 1
        return f"snapshot-{snapshot_calls}"

    coordinator = DashboardStreamCoordinator(
        lambda: token,
        load_snapshot,
        poll_interval_seconds=2,
        clock=clock,
    )

    assert coordinator.poll().snapshot == "snapshot-1"
    now = 12.0
    assert coordinator.poll().snapshot == "snapshot-1"
    token = "revision-2"
    now = 14.0
    changed = coordinator.poll()

    assert changed.change_token == "revision-2"
    assert changed.snapshot == "snapshot-2"
    assert snapshot_calls == 2


def test_coordinator_recovers_after_a_failed_poll_interval() -> None:
    now = 10.0
    should_fail = True

    def clock() -> float:
        return now

    def load_token() -> str:
        if should_fail:
            raise RuntimeError("database unavailable")
        return "revision-1"

    coordinator = DashboardStreamCoordinator(
        load_token,
        lambda: {"status": "ready"},
        poll_interval_seconds=2,
        clock=clock,
    )

    try:
        coordinator.poll()
    except DashboardStreamUnavailable:
        pass
    else:
        raise AssertionError("failed poll must be reported as unavailable")

    try:
        coordinator.poll()
    except DashboardStreamUnavailable:
        pass
    else:
        raise AssertionError("failed interval must remain unavailable")

    should_fail = False
    now = 12.0
    recovered = coordinator.poll()

    assert recovered.change_token == "revision-1"
    assert recovered.snapshot == {"status": "ready"}


def test_stream_registry_reuses_entries_and_evicts_least_recently_used() -> None:
    registry: DashboardStreamRegistry[str] = DashboardStreamRegistry(capacity=2)
    created: dict[str, int] = {}

    def factory(key: str) -> DashboardStreamCoordinator[str]:
        created[key] = created.get(key, 0) + 1
        return DashboardStreamCoordinator(lambda: key, lambda: key)

    first = registry.get_or_create("first", lambda: factory("first"))
    second = registry.get_or_create("second", lambda: factory("second"))
    assert registry.keys() == ("first", "second")

    # 命中条目会被移动到队尾，first 因而成为最新访问项。
    assert registry.get_or_create("first", lambda: factory("first")) is first
    assert registry.keys() == ("second", "first")

    registry.get_or_create("third", lambda: factory("third"))
    assert registry.keys() == ("first", "third")
    assert len(registry) == 2
    assert registry.get_or_create("second", lambda: factory("second")) is not second
    assert created == {"first": 1, "second": 2, "third": 1}


def test_stream_registry_rejects_invalid_capacity_and_key() -> None:
    for capacity in (0, -1, True):
        try:
            DashboardStreamRegistry(capacity=capacity)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid capacity must be rejected")

    registry: DashboardStreamRegistry[str] = DashboardStreamRegistry()
    try:
        registry.get_or_create("", lambda: DashboardStreamCoordinator(lambda: "", lambda: ""))
    except ValueError:
        pass
    else:
        raise AssertionError("empty cache key must be rejected")


def test_stream_route_emits_authenticated_dashboard_event() -> None:
    class RequestStub:
        cookies = {"session-cookie": "signed-session"}
        headers = {"last-event-id": "revision-0"}

        async def is_disconnected(self) -> bool:
            return False

    class SnapshotStub:
        @staticmethod
        def model_dump_json() -> str:
            return '{"status":"ready"}'

    verified_tokens: list[str | None] = []

    class AuthServiceStub:
        settings = SimpleNamespace(cookie_name="session-cookie")

        @staticmethod
        def verify_session(token: str | None) -> None:
            verified_tokens.append(token)

    update = DashboardStreamUpdate(
        change_token="revision-1",
        snapshot=SnapshotStub(),
    )
    stream = SimpleNamespace(poll=lambda: update)
    principal = SessionPrincipal(
        username="reviewer",
        role=AccessRole.VIEWER,
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        resource_scope=ResourceScope.unrestricted_scope(),
    )
    application = FastAPI()
    register_dashboard_routes(
        application,
        dashboard_snapshot=lambda _limit, _cursor: SnapshotStub(),
        get_dashboard_stream=lambda: stream,
        get_auth_service=AuthServiceStub,
        require_review_viewer=lambda: principal,
    )
    endpoint = next(
        route.endpoint
        for route in application.routes
        if getattr(route, "path", None) == "/api/v1/reviews/stream"
    )

    async def consume_first_event() -> tuple[str, str | None]:
        response = await endpoint(RequestStub(), principal)
        event = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return event, response.headers.get("x-accel-buffering")

    event, buffering = asyncio.run(consume_first_event())

    assert verified_tokens == ["signed-session"]
    assert event == (
        "id: revision-1\n"
        "event: dashboard\n"
        'data: {"status":"ready"}\n\n'
    )
    assert buffering == "no"
