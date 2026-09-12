import httpx
import pytest

from domain.retrieval import RetrievalSettings
from services.retrieval_providers import (
    AliyunRetrievalClient,
    RequestBudget,
    RetrievalError,
    normalize_aliyun_host,
)


def test_aliyun_batch_order_is_restored_and_credentials_stay_in_header():
    observed = []

    def handler(request):
        observed.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.5] * 1024},
                    {"index": 0, "embedding": [1.0] * 1024},
                ],
                "usage": {"prompt_tokens": 7},
            },
        )

    client = AliyunRetrievalClient(
        RetrievalSettings(api_host="https://example.cn-beijing.maas.aliyuncs.com"),
        "test-key",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.embed(("first", "second"))
    assert result.vectors[0][0] == 1
    assert result.input_tokens == 7
    assert observed[0].headers["authorization"] == "Bearer test-key"
    assert b"test-key" not in observed[0].content


def test_vector_dimensions_duplicates_and_nonfinite_values_are_rejected():
    for rows in [
        [{"index": 0, "embedding": [1.0] * 100}],
        [{"index": 1, "embedding": [1.0] * 1024}],
        [{"index": 0, "embedding": [0.0] * 1024}],
    ]:
        client = AliyunRetrievalClient(
            RetrievalSettings(api_host="https://example.cn-beijing.maas.aliyuncs.com"),
            "test-key",
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda request, rows=rows: httpx.Response(200, json={"data": rows})
                )
            ),
        )
        with pytest.raises(RetrievalError):
            client.embed(("one",))


def test_quota_failure_exposes_only_safe_code_and_does_not_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": "insufficient_quota",
                    "message": "test-key private text",
                }
            },
        )

    client = AliyunRetrievalClient(
        RetrievalSettings(api_host="https://example.cn-beijing.maas.aliyuncs.com"),
        "test-key",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RetrievalError, match="insufficient_quota") as failure:
        client.embed(("one",))
    assert "test-key" not in str(failure.value)
    assert failure.value.response_status == 403
    assert len(calls) == 1


def test_429_retry_uses_bounded_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr("services.retrieval_providers.time.sleep", sleeps.append)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, headers={"retry-after": "2"}, json={"code": "Throttling"}
        )

    client = AliyunRetrievalClient(
        RetrievalSettings(api_host="https://example.cn-beijing.maas.aliyuncs.com"),
        "test-key",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RetrievalError):
        client.embed(("one",))
    assert calls == 3 and sleeps == [2, 2]


def test_rerank_checks_candidate_indices_and_model_endpoint():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": 1, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 0.2},
                    ]
                }
            },
        )

    client = AliyunRetrievalClient(
        RetrievalSettings(api_host="https://example.cn-beijing.maas.aliyuncs.com"),
        "test-key",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.rerank("query", ("a", "b")).ranking[0][0] == 1
    assert requests[0].url.path == "/api/v1/services/rerank/text-rerank/text-rerank"


def test_provider_host_does_not_accept_unrelated_or_private_endpoints():
    assert (
        normalize_aliyun_host(
            "https://a.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        )
        == "https://a.cn-beijing.maas.aliyuncs.com"
    )
    for value in [
        "http://127.0.0.1",
        "https://evil.example",
        "https://maas.aliyuncs.com.evil.example",
        "https://a.cn-beijing.maas.aliyuncs.com/arbitrary",
    ]:
        with pytest.raises(ValueError):
            normalize_aliyun_host(value)


def test_server_pause_switch_blocks_real_requests_before_transport(monkeypatch):
    monkeypatch.setenv("OPENREVIEWER_RETRIEVAL_API_DISABLED", "true")
    calls = []
    http = httpx.Client(
        transport=httpx.MockTransport(lambda request: calls.append(request))
    )
    client = AliyunRetrievalClient(
        RetrievalSettings(api_host="https://example.cn-beijing.maas.aliyuncs.com"),
        "test-key",
        http,
    )
    client._owns_client = True
    try:
        with pytest.raises(RetrievalError, match="外部调用已暂停"):
            client.embed(("one",))
        assert calls == []
    finally:
        client.close()


def test_http_retries_consume_the_same_operation_budget(monkeypatch):
    calls = []
    monkeypatch.setattr("services.retrieval_providers.time.sleep", lambda _: None)
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (calls.append(request), httpx.Response(429))[1]
        )
    )
    client = AliyunRetrievalClient(
        RetrievalSettings(api_host="https://example.cn-beijing.maas.aliyuncs.com"),
        "test-key",
        http,
        budget=RequestBudget(2),
    )
    with pytest.raises(RetrievalError, match="请求上限"):
        client.embed(("bounded",))
    assert len(calls) == 2


def test_retrieval_attempt_is_settled_before_backoff(monkeypatch):
    from services.model_budget import ModelBudgetReservation, model_budget_scope

    events = []

    class Accountant:
        def reserve(self, request):
            events.append(("reserve", request.purpose))
            return ModelBudgetReservation("fixture", "plan", 1, 1, 0, 0, 0)

        def settle(self, reservation, **usage):
            events.append(("settle", usage["response_status"]))

    monkeypatch.setattr(
        "services.retrieval_providers.time.sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )
    responses = iter(
        (
            httpx.Response(429),
            httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "embedding": [1.0] * 1024}],
                    "usage": {"input_tokens": 1},
                },
            ),
        )
    )
    client = AliyunRetrievalClient(
        RetrievalSettings(api_host="https://example.cn-beijing.maas.aliyuncs.com"),
        "fixture-key",
        httpx.Client(transport=httpx.MockTransport(lambda _: next(responses))),
    )
    with model_budget_scope(Accountant()):
        result = client.embed(("bounded",))
    assert result.input_tokens == 1
    assert events == [
        ("reserve", "embedding"),
        ("settle", 429),
        ("sleep", 1),
        ("reserve", "embedding"),
        ("settle", 200),
    ]
