import httpx
import pytest

from domain.security import SafeApplicationError
from services.github import GitHubApiClient
from services.telemetry import TelemetryHttpServer, TelemetryRegistry


def test_registry_renders_cumulative_low_cardinality_histograms() -> None:
    registry = TelemetryRegistry()

    registry.observe_http("GET", '/reviews/{review_id}/"detail"', 200, 0.25)
    registry.observe_external("github", 1.5, status_code=429)

    metrics = registry.render()

    assert (
        'openreviewer_http_server_request_duration_seconds_bucket{method="GET",'
        'route="/reviews/{review_id}/\\"detail\\"",status_class="2xx",le="0.25"} 1'
        in metrics
    )
    assert (
        'openreviewer_http_server_request_duration_seconds_count{method="GET",'
        'route="/reviews/{review_id}/\\"detail\\"",status_class="2xx"} 1'
        in metrics
    )
    assert (
        'openreviewer_external_http_request_duration_seconds_count{service="github",'
        'outcome="rate_limited"} 1'
        in metrics
    )


def test_worker_telemetry_server_exports_health_and_metrics() -> None:
    registry = TelemetryRegistry()
    registry.observe_external("model_openai", 0.1, status_code=200)
    server = TelemetryHttpServer("127.0.0.1", 0, registry)
    server.start()

    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.port}",
            timeout=2,
            trust_env=False,
        ) as client:
            health = client.get("/healthz")
            metrics = client.get("/metrics")
            missing = client.get("/missing")
    finally:
        server.close()

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "service": "openreviewer-worker",
    }
    assert metrics.status_code == 200
    assert "openreviewer_external_http_request_duration_seconds" in metrics.text
    assert missing.status_code == 404


def test_github_client_records_each_transport_request_once() -> None:
    registry = TelemetryRegistry()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ok":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(429, json={"message": "rate limited"})

    ticks = iter((1.0, 1.2, 2.0, 2.3))
    with httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        api = GitHubApiClient(
            client=client,
            monotonic=lambda: next(ticks),
            telemetry=registry,
        )
        assert api.request_json("GET", "/ok", bearer_token="test-token").payload == {
            "ok": True
        }
        with pytest.raises(SafeApplicationError):
            api.request_json("GET", "/limited", bearer_token="test-token")

    metrics = registry.render()
    assert (
        'openreviewer_external_http_request_duration_seconds_count{service="github",'
        'outcome="success"} 1'
        in metrics
    )
    assert (
        'openreviewer_external_http_request_duration_seconds_count{service="github",'
        'outcome="rate_limited"} 1'
        in metrics
    )
