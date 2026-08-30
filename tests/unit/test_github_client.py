import json

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


@pytest.mark.parametrize(
    ("link_header", "expected"),
    [
        ('<https://api.github.test/items?page=2>; rel="next"', True),
        (
            '<https://api.github.test/items?page=1>; rel="prev", '
            '<https://api.github.test/items?page=3>; rel="last"',
            False,
        ),
        (None, None),
        ("<https://api.github.test/items?page=2>", None),
    ],
)
def test_github_client_records_only_pagination_relation(
    link_header: str | None,
    expected: bool | None,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        headers = {} if link_header is None else {"Link": link_header}
        return httpx.Response(200, json=[], headers=headers)

    client = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
    )

    result = client.request_json(
        "GET",
        "/items",
        bearer_token=FAKE_TOKEN,
    )

    assert result.audit.has_next_page is expected
    assert "api.github.test" not in str(result.audit)


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


def test_github_client_sends_bounded_json_without_exposing_it_in_audit() -> None:
    """验证 GraphQL 所需 JSON 请求体被正确编码，审计仍只记录方法和路径。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "query": "query Test { viewer { login } }",
            "variables": {"name": "测试"},
        }
        return httpx.Response(200, json={"data": {"viewer": {"login": "octocat"}}})

    client = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
    )

    result = client.request_json(
        "POST",
        "/graphql",
        bearer_token=FAKE_TOKEN,
        json_body={
            "query": "query Test { viewer { login } }",
            "variables": {"name": "测试"},
        },
    )

    assert result.payload == {"data": {"viewer": {"login": "octocat"}}}
    assert result.audit.request_path == "/graphql"
    assert "测试" not in str(result.audit)


def test_github_client_rejects_json_body_on_get_before_network() -> None:
    client = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(
                lambda _request: pytest.fail("invalid request must not be sent")
            ),
        ),
    )

    with pytest.raises(ValueError):
        client.request_json(
            "GET",
            "/graphql",
            bearer_token=FAKE_TOKEN,
            json_body={"query": "query Test { viewer { login } }"},
        )
