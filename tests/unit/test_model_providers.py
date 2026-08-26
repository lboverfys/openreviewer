from decimal import Decimal
import json

import httpx
import pytest

from domain.enums import ModelApiProtocol, ModelCallStatus, ModelProvider
from domain.security import ErrorCode, SafeApplicationError
from services.model_providers import create_model_reviewer
from services.model_review import ModelPricing, ModelServiceSettings
from tests.unit.test_model_review import make_model_input, make_output


def _settings(
    provider: ModelProvider,
    pricing: ModelPricing | None = None,
    *,
    api_protocol: ModelApiProtocol | None = None,
    api_base_url: str | None = None,
):
    return ModelServiceSettings(
        provider=provider,
        model="test-model",
        api_key="test-only-api-key",
        api_protocol=api_protocol,
        pricing=pricing,
        api_base_url=api_base_url or f"https://api.{provider.value}.test",
    )


def test_openai_responses_request_and_usage_are_normalized() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert request.headers["authorization"] == "Bearer test-only-api-key"
        assert body["store"] is False
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        return httpx.Response(
            200,
            headers={"x-request-id": "req_openai_1"},
            json={
                "id": "resp_openai_1",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": make_output().model_dump_json(),
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 40,
                    "input_tokens_details": {"cached_tokens": 20},
                    "output_tokens_details": {"reasoning_tokens": 10},
                },
            },
        )

    ticks = iter((10.0, 10.25))
    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            ModelPricing(
                input_usd_per_million=Decimal("2"),
                output_usd_per_million=Decimal("10"),
                cache_read_usd_per_million=Decimal("0.5"),
            ),
        ),
        client=client,
        monotonic=lambda: next(ticks),
    )

    result = reviewer.review(make_model_input())

    assert len(requests) == 1
    assert result.status is ModelCallStatus.SUCCEEDED
    assert result.api_protocol is ModelApiProtocol.RESPONSES
    assert result.provider_response_id == "resp_openai_1"
    assert result.provider_request_id == "req_openai_1"
    assert result.duration_ms == 250
    assert result.usage.input_tokens == 100
    assert result.usage.cache_read_input_tokens == 20
    assert result.usage.reasoning_output_tokens == 10
    assert result.estimated_cost_microusd == 610
    assert len(result.output.findings) == 1
    client.close()


def test_openai_chat_completions_request_and_usage_are_normalized() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-only-api-key"
        assert body["store"] is False
        assert body["max_completion_tokens"] == 8192
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][1]["role"] == "user"
        schema = body["response_format"]["json_schema"]
        assert body["response_format"]["type"] == "json_schema"
        assert schema["strict"] is True
        return httpx.Response(
            200,
            headers={"x-request-id": "req_chat_1"},
            json={
                "id": "chatcmpl_test_1",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": make_output().model_dump_json(),
                            "refusal": None,
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 20},
                    "completion_tokens_details": {"reasoning_tokens": 10},
                },
            },
        )

    ticks = iter((20.0, 20.125))
    client = httpx.Client(
        base_url="https://api.openai.test/v1",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            ModelPricing(
                input_usd_per_million=Decimal("2"),
                output_usd_per_million=Decimal("10"),
                cache_read_usd_per_million=Decimal("0.5"),
            ),
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
            api_base_url="https://api.openai.test/v1",
        ),
        client=client,
        monotonic=lambda: next(ticks),
    )

    result = reviewer.review(make_model_input())

    assert len(requests) == 1
    assert result.api_protocol is ModelApiProtocol.CHAT_COMPLETIONS
    assert result.provider_response_id == "chatcmpl_test_1"
    assert result.provider_request_id == "req_chat_1"
    assert result.duration_ms == 125
    assert result.usage.input_tokens == 100
    assert result.usage.cache_read_input_tokens == 20
    assert result.usage.output_tokens == 40
    assert result.usage.reasoning_output_tokens == 10
    assert result.estimated_cost_microusd == 610
    client.close()


def test_anthropic_messages_request_and_cache_usage_are_normalized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "test-only-api-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert body["output_config"]["format"]["type"] == "json_schema"
        assert body["messages"][0]["role"] == "user"
        return httpx.Response(
            200,
            headers={"request-id": "req_anthropic_1"},
            json={
                "id": "msg_anthropic_1",
                "type": "message",
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": make_output().model_dump_json()}
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 40,
                },
            },
        )

    client = httpx.Client(
        base_url="https://api.anthropic.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.ANTHROPIC,
            ModelPricing(
                input_usd_per_million=Decimal("3"),
                output_usd_per_million=Decimal("15"),
                cache_read_usd_per_million=Decimal("0.3"),
                cache_write_usd_per_million=Decimal("3.75"),
            ),
        ),
        client=client,
    )

    result = reviewer.review(make_model_input())

    assert result.api_protocol is ModelApiProtocol.MESSAGES
    assert result.provider_response_id == "msg_anthropic_1"
    assert result.provider_request_id == "req_anthropic_1"
    assert result.usage.input_tokens == 100
    assert result.usage.cache_read_input_tokens == 30
    assert result.usage.cache_write_input_tokens == 40
    assert result.estimated_cost_microusd == 759
    client.close()


@pytest.mark.parametrize(
    ("provider", "status_code", "expected_code", "retryable"),
    [
        (ModelProvider.OPENAI, 429, ErrorCode.MODEL_RATE_LIMITED, True),
        (ModelProvider.ANTHROPIC, 401, ErrorCode.MODEL_AUTHENTICATION_FAILED, False),
        (ModelProvider.ANTHROPIC, 529, ErrorCode.MODEL_SERVER_ERROR, True),
    ],
)
def test_provider_http_errors_are_classified_without_response_body(
    provider: ModelProvider,
    status_code: int,
    expected_code: ErrorCode,
    retryable: bool,
) -> None:
    client = httpx.Client(
        base_url=f"https://api.{provider.value}.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                status_code,
                headers={"request-id": "safe-request-id"},
                json={"error": {"message": "must not be persisted"}},
            )
        ),
    )
    reviewer = create_model_reviewer(_settings(provider), client=client)

    with pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert captured.value.error.code is expected_code
    assert captured.value.error.retryable is retryable
    assert "must not be persisted" not in str(captured.value.error.details)
    client.close()


@pytest.mark.parametrize("provider", [ModelProvider.OPENAI, ModelProvider.ANTHROPIC])
def test_provider_truncation_is_not_accepted_as_structured_output(
    provider: ModelProvider,
) -> None:
    payload = (
        {"id": "resp_1", "status": "incomplete", "output": [], "usage": {}}
        if provider is ModelProvider.OPENAI
        else {
            "id": "msg_1",
            "stop_reason": "max_tokens",
            "content": [],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )
    client = httpx.Client(
        base_url=f"https://api.{provider.value}.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
    )
    reviewer = create_model_reviewer(_settings(provider), client=client)

    with pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert captured.value.error.code is ErrorCode.MODEL_OUTPUT_TRUNCATED
    assert captured.value.error.retryable is False
    client.close()


@pytest.mark.parametrize(
    ("finish_reason", "refusal", "expected_code"),
    [
        ("length", None, ErrorCode.MODEL_OUTPUT_TRUNCATED),
        ("stop", "request refused", ErrorCode.MODEL_OUTPUT_REFUSED),
    ],
)
def test_chat_completions_rejects_truncation_and_refusal(
    finish_reason: str,
    refusal: str | None,
    expected_code: ErrorCode,
) -> None:
    payload = {
        "id": "chatcmpl_1",
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": make_output().model_dump_json(),
                    "refusal": refusal,
                },
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=payload)
        ),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        ),
        client=client,
    )

    with pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert captured.value.error.code is expected_code
    assert "request refused" not in str(captured.value.error.details)
    client.close()


def test_empty_plan_skips_http_but_records_zero_usage() -> None:
    review_input = make_model_input().model_copy(
        update={"units": (), "total_estimated_input_bytes": 0}
    )
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=client,
    )

    result = reviewer.review(review_input)

    assert calls == 0
    assert result.status is ModelCallStatus.SKIPPED
    assert result.estimated_cost_microusd == 0
    assert result.output.findings == ()
    client.close()
