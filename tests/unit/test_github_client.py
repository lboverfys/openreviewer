import httpx
import pytest

from domain.security import ErrorCode, SafeApplicationError
from services.github import GitHubApiClient, GitHubClientSettings


FAKE_TOKEN = "ghs_FAKEINSTALLATIONTOKEN123456789"


def test_github_client_returns_bounded_payload_and_audit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
        return httpx.Response(
            200,
            json={"number": 12},
            headers={
                "x-github-request-id": "request-001",
                "x-ratelimit-remaining": "4999",
            },
        )

    ticks = iter((10.0, 10.025))
    client = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
        monotonic=lambda: next(ticks),
    )

    result = client.request_json(
        "GET",
        "/repos/example/project/pulls/12",
        bearer_token=FAKE_TOKEN,
    )

    assert result.payload == {"number": 12}
    assert result.audit.response_status == 200
    assert result.audit.github_request_id == "request-001"
    assert result.audit.duration_ms == 25
    assert result.audit.rate_limit_remaining == 4999


def test_github_client_classifies_rate_limit_without_leaking_token() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": f"do not expose {FAKE_TOKEN}"},
            headers={"x-ratelimit-remaining": "4999", "retry-after": "60"},
        )

    client = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
    )

    with pytest.raises(SafeApplicationError) as captured:
        client.request_json(
            "GET",
            "/rate-limited",
            bearer_token=FAKE_TOKEN,
        )

    safe_error = captured.value.error
    assert safe_error.code is ErrorCode.GITHUB_RATE_LIMITED
    assert safe_error.retryable is True
    assert safe_error.details["retry_after_seconds"] == 60
    assert FAKE_TOKEN not in str(safe_error.public_payload())


def test_github_client_rejects_absolute_target_before_sending_token() -> None:
    client = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(
                lambda _request: pytest.fail("request must not be sent")
            ),
        ),
    )

    with pytest.raises(ValueError):
        client.request_json(
            "GET",
            "https://attacker.example/token",
            bearer_token=FAKE_TOKEN,
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.github.test",
        "https://user:password@api.github.test",
        "https://api.github.test/api/v3",
        "https://api.github.test?token=secret",
    ],
)
def test_github_client_rejects_unsafe_base_urls(base_url: str) -> None:
    with pytest.raises(ValueError):
        GitHubClientSettings(api_base_url=base_url)


def test_github_client_classifies_transport_timeout_as_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    client = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
    )

    with pytest.raises(SafeApplicationError) as captured:
        client.request_json(
            "GET",
            "/slow",
            bearer_token=FAKE_TOKEN,
        )

    assert captured.value.error.code is ErrorCode.GITHUB_TIMEOUT
    assert captured.value.error.retryable is True
    assert FAKE_TOKEN not in str(captured.value.error.public_payload())


def test_github_client_rejects_zero_response_limit_instead_of_using_default() -> None:
    """验证显式零上限不会被 ``or`` 逻辑误当成未配置。"""

    client = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(
                lambda _request: pytest.fail("invalid limit must be rejected first")
            ),
        ),
    )

    with pytest.raises(ValueError):
        client.request_json(
            "GET",
            "/bounded",
            bearer_token=FAKE_TOKEN,
            max_response_bytes=0,
        )
