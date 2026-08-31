import json
from decimal import Decimal

import httpx
import pytest

from domain.enums import (
    ModelApiProtocol,
    ModelCallStatus,
    ModelProvider,
    ModelReasoningEffort,
    ReviewAgent,
)
from domain.security import ErrorCode, SafeApplicationError
from services.model_budget import (
    ModelBudgetRequest,
    ModelBudgetReservation,
    model_budget_scope,
)
from services.model_providers import (
    _ResponsesSseAccumulator,
    _SseResponseTooLarge,
    create_model_reviewer,
)
from services.model_review import ModelPricing, ModelServiceSettings
from services.task_queue import ModelBudgetExceededError, TaskQueueError
from services.telemetry import TelemetryRegistry
from services.token_estimation import estimate_model_request_tokens
from tests.unit.test_model_review import (
    make_large_v2_input,
    make_model_input,
    make_output,
)


class RecordingBudgetAccountant:
    def __init__(self) -> None:
        self.requests: list[ModelBudgetRequest] = []
        self.settlements: list[dict[str, object]] = []
        self._settled_ids: set[str] = set()

    def reserve(self, request: ModelBudgetRequest) -> ModelBudgetReservation:
        self.requests.append(request)
        sequence = len(self.requests)
        return ModelBudgetReservation(
            id=f"reservation-{sequence}",
            review_plan_id="plan-1",
            sequence=sequence,
            reserved_input_tokens=request.input_token_upper_bound,
            reserved_output_tokens=request.output_token_upper_bound,
            reserved_cost_microusd=request.cost_upper_bound_microusd or 0,
            remaining_duration_ms=30_000,
        )

    def settle(
        self,
        reservation: ModelBudgetReservation,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        estimated_cost_microusd: int | None,
        response_status: int | None,
        duration_ms: int,
        uncertain: bool = False,
    ) -> None:
        assert reservation.id not in self._settled_ids
        self._settled_ids.add(reservation.id)
        self.settlements.append(
            {
                "reservation_id": reservation.id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_microusd": estimated_cost_microusd,
                "response_status": response_status,
                "duration_ms": duration_ms,
                "uncertain": uncertain,
            }
        )


def _settings(
    provider: ModelProvider,
    pricing: ModelPricing | None = None,
    *,
    api_protocol: ModelApiProtocol | None = None,
    api_base_url: str | None = None,
    reasoning_effort: ModelReasoningEffort = ModelReasoningEffort.NONE,
    max_output_tokens: int = 8192,
):
    return ModelServiceSettings(
        provider=provider,
        model="test-model",
        api_key="test-only-api-key",
        api_protocol=api_protocol,
        reasoning_effort=reasoning_effort,
        pricing=pricing,
        max_output_tokens=max_output_tokens,
        api_base_url=api_base_url or f"https://api.{provider.value}.test",
    )


def test_openai_responses_request_and_usage_are_normalized() -> None:
    requests: list[httpx.Request] = []
    telemetry = TelemetryRegistry()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert request.headers["authorization"] == "Bearer test-only-api-key"
        assert request.headers["accept"] == "text/event-stream"
        assert body["stream"] is True
        assert body["store"] is False
        assert body["reasoning"] == {"effort": "medium"}
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        prompt_payload = json.loads(body["input"][1]["content"][0]["text"])
        assert prompt_payload["output_contract"]["required"] == [
            "verdict",
            "summary",
            "checked_areas",
            "findings",
        ]
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
            reasoning_effort=ModelReasoningEffort.MEDIUM,
        ),
        client=client,
        monotonic=lambda: next(ticks),
        telemetry=telemetry,
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
    assert (
        'openreviewer_external_http_request_duration_seconds_count{'
        'service="model_openai",outcome="success"} 1'
        in telemetry.render()
    )
    client.close()


def test_budget_reservation_uses_estimate_margin_for_valid_request() -> None:
    """适配器的预算预留必须与批次规划使用同一 Token 估算口径。"""

    accountant = RecordingBudgetAccountant()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chat-budget-estimate",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": make_output().model_dump_json()},
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 20},
            },
        )

    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    settings = _settings(
        ModelProvider.OPENAI,
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
    )
    reviewer = create_model_reviewer(settings, client=client)

    with model_budget_scope(accountant):
        reviewer.review(make_model_input())

    assert len(requests) == 1
    body = json.loads(requests[0].content)
    estimate = estimate_model_request_tokens(
        body,
        requests[0].content,
        provider=settings.provider,
        protocol=settings.resolved_api_protocol,
        model=settings.model,
    )
    assert accountant.requests[0].input_token_upper_bound == estimate.reservation_tokens
    # 小请求的 4096 Token 最低安全余量可能已经覆盖结构化上界；此时
    # 预留值按边界取最小值，允许与 upper_bound_tokens 相等。
    assert accountant.requests[0].input_token_upper_bound <= estimate.upper_bound_tokens
    client.close()


def test_openai_responses_streaming_sse_uses_completed_response() -> None:
    requests: list[dict[str, object]] = []
    output_text = make_output().model_dump_json()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        completed = {
            "id": "resp_stream_1",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": output_text}],
                }
            ],
            "usage": {"input_tokens": 120, "output_tokens": 40},
        }
        content = (
            "event: response.created\n"
            "data: {\"type\":\"response.created\"}\n\n"
            "event: response.output_text.delta\n"
            "data: {\"type\":\"response.output_text.delta\",\"delta\":\"{\"}\n\n"
            "event: response.completed\n"
            "data: "
            + json.dumps(
                {"type": "response.completed", "response": completed},
                separators=(",", ":"),
            )
            + "\n\n"
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream; charset=utf-8",
                "x-request-id": "relay-stream-1",
            },
            content=content.encode("utf-8"),
        )

    ticks = iter((10.0, 12.5))
    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=client,
        monotonic=lambda: next(ticks),
    )

    result = reviewer.review(make_model_input())

    assert len(requests) == 1
    assert result.status is ModelCallStatus.SUCCEEDED
    assert result.provider_response_id == "resp_stream_1"
    assert result.provider_request_id == "relay-stream-1"
    assert result.duration_ms == 2_500
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 40
    assert result.output == make_output()
    client.close()


def test_openai_responses_sse_probe_skips_large_relay_preamble() -> None:
    """未声明 Content-Type 的中转站可带较长空白/注释前导。"""

    output_text = make_output().model_dump_json()

    def handler(_request: httpx.Request) -> httpx.Response:
        delta_event = json.dumps(
            {
                "type": "response.output_text.delta",
                "delta": output_text,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        content = (
            b" " * (128 * 1024)
            + b": relay keep-alive\r\r"
            + b"event: response.output_text.delta\r"
            + b"data: "
            + delta_event
            + b"\r\r"
            + b"event: response.completed\r"
            + b'data: {"type":"response.completed","response":{"id":"resp-preamble",'
            + b'"status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\r\r'
            + b"data: [DONE]\r\r"
        )
        return httpx.Response(200, content=content)

    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(_settings(ModelProvider.OPENAI), client=client)

    result = reviewer.review(make_model_input())

    assert result.status is ModelCallStatus.SUCCEEDED
    assert result.provider_response_id == "resp-preamble"
    assert result.output == make_output()
    client.close()


def test_openai_responses_streaming_sse_without_terminal_response_is_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        content = (
            "event: response.output_text.delta\n"
            "data: {\"type\":\"response.output_text.delta\",\"delta\":\"{}\"}\n\n"
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=content.encode("utf-8"),
        )

    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=client,
    )

    with pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert captured.value.error.code is ErrorCode.MODEL_INVALID_RESPONSE
    assert captured.value.error.details["status_code"] == 200
    client.close()


def test_responses_sse_accumulator_handles_arbitrary_chunk_boundaries() -> None:
    output_text = make_output().model_dump_json()
    events = (
        "event: response.output_text.delta\r\n"
        + "data: "
        + json.dumps(
            {
                "type": "response.output_text.delta",
                "delta": output_text,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\r\n\r\n"
        "event: response.completed\r\n"
        "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp-chunked\",\"status\":\"completed\",\"usage\":{\"input_tokens\":3,\"output_tokens\":4}}}\r\n\r\n"
        "data: [DONE]\r\n\r\n"
    ).encode("utf-8")

    parser = _ResponsesSseAccumulator(max_response_bytes=64 * 1024)
    for byte in events:
        parser.feed(bytes((byte,)))

    payload = parser.finish()
    assert payload["id"] == "resp-chunked"
    assert payload["status"] == "completed"
    assert payload["usage"] == {"input_tokens": 3, "output_tokens": 4}
    output = payload["output"]
    assert isinstance(output, list)
    assert output[0]["content"][0]["text"] == output_text


def test_responses_sse_accumulator_preserves_event_name_across_split_crlf() -> None:
    """CRLF 拆在两个 HTTP 分块时，不能丢失依赖 event 字段的事件类型。"""

    chunks = (
        b"event: response.output_text.delta\r",
        b'\ndata: {"delta":"{}"}\r',
        b"\n\r",
        b"\nevent: response.completed\r\n",
        b'data: {"response":{"id":"resp-crlf","status":"completed",'
        b'"usage":{"input_tokens":1,"output_tokens":1}}}\r\n\r\n',
        b"data: [DONE]\r\n\r\n",
    )

    parser = _ResponsesSseAccumulator(max_response_bytes=64 * 1024)
    for chunk in chunks:
        parser.feed(chunk)

    payload = parser.finish()
    assert payload["id"] == "resp-crlf"
    assert payload["status"] == "completed"
    output = payload["output"]
    assert isinstance(output, list)
    assert output[0]["content"][0]["text"] == "{}"


def test_responses_sse_accumulator_separates_raw_and_final_text_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.model_providers as model_providers

    monkeypatch.setattr(model_providers, "_MAX_SSE_RAW_RESPONSE_BYTES", 8)
    parser = _ResponsesSseAccumulator()
    with pytest.raises(_SseResponseTooLarge):
        parser.feed(b"data: {}\n\n")

    parser = _ResponsesSseAccumulator(max_response_bytes=4)
    event = json.dumps(
        {"type": "response.output_text.delta", "delta": "12345"},
        separators=(",", ":"),
    ).encode("utf-8")
    with pytest.raises(_SseResponseTooLarge):
        parser.feed(b"data: " + event + b"\n\n")


def test_responses_sse_metadata_can_exceed_final_text_limit() -> None:
    """中转站事件开销不能被误算成最终模型文本。"""

    parser = _ResponsesSseAccumulator(max_response_bytes=128)
    parser.feed(b": relay-metadata " + b"x" * 1024 + b"\n\n")
    parser.feed(
        b'data: {"type":"response.output_text.delta","delta":"{}"}\n\n'
        b'data: {"type":"response.completed","response":{"id":"resp-metadata",'
        b'"status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
    )

    payload = parser.finish()

    assert payload["id"] == "resp-metadata"
    output = payload["output"]
    assert isinstance(output, list)
    assert output[0]["content"][0]["text"] == "{}"


def test_openai_responses_streaming_falls_back_when_relay_rejects_stream() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if body.get("stream") is True:
            return httpx.Response(
                400,
                json={"error": {"message": "stream is not supported"}},
            )
        return httpx.Response(
            200,
            json={
                "id": "resp_sync_fallback",
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
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        )

    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=client,
    )

    result = reviewer.review(make_model_input())

    assert len(requests) == 2
    assert requests[0]["stream"] is True
    assert "stream" not in requests[1]
    assert result.status is ModelCallStatus.SUCCEEDED
    assert result.provider_response_id == "resp_sync_fallback"
    client.close()


def test_live_provider_rejects_legacy_findings_only_output() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "id": "resp_legacy_output",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps({"findings": []}),
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=client,
    )

    with pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert calls == 2
    assert captured.value.error.code is ErrorCode.MODEL_INVALID_RESPONSE
    assert captured.value.error.details["format_repair_attempted"] is True
    assert captured.value.error.details["failed_input_tokens"] == 2
    assert {
        item["path"]
        for item in captured.value.error.details["validation_issues"]
    } == {"verdict", "summary", "checked_areas"}
    client.close()


def test_invalid_contract_is_repaired_once_and_usage_is_accumulated() -> None:
    requests: list[dict[str, object]] = []
    accountant = RecordingBudgetAccountant()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        repaired = len(requests) == 2
        if repaired:
            repair_text = body["input"][-1]["content"][0]["text"]
            assert "verdict、summary、checked_areas、findings" in repair_text
            assert "verdict, summary, checked_areas" in repair_text
        return httpx.Response(
            200,
            headers={"x-request-id": f"req_repair_{len(requests)}"},
            json={
                "id": f"resp_repair_{len(requests)}",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    make_output().model_dump_json()
                                    if repaired
                                    else json.dumps({"findings": []})
                                ),
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 12 if repaired else 10,
                    "output_tokens": 3 if repaired else 2,
                },
            },
        )

    ticks = iter((10.0, 11.0, 20.0, 22.0))
    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=client,
        monotonic=lambda: next(ticks),
    )

    with model_budget_scope(accountant):
        result = reviewer.review(make_model_input())

    assert len(requests) == 2
    assert result.status is ModelCallStatus.SUCCEEDED
    assert result.provider_response_id == "resp_repair_2"
    assert result.provider_request_id == "req_repair_1,req_repair_2"
    assert result.duration_ms == 3000
    assert result.usage.input_tokens == 22
    assert result.usage.output_tokens == 5
    assert result.output == make_output()
    assert len(accountant.requests) == 2
    assert accountant.settlements == [
        {
            "reservation_id": "reservation-1",
            "input_tokens": 10,
            "output_tokens": 2,
            "estimated_cost_microusd": None,
            "response_status": 200,
            "duration_ms": 1000,
            "uncertain": False,
        },
        {
            "reservation_id": "reservation-2",
            "input_tokens": 12,
            "output_tokens": 3,
            "estimated_cost_microusd": None,
            "response_status": 200,
            "duration_ms": 2000,
            "uncertain": False,
        },
    ]
    client.close()


def test_contract_repair_parse_error_settles_retry_reservation() -> None:
    """格式纠正的响应用量损坏时，第二次 reservation 也必须结算。"""

    requests: list[dict[str, object]] = []
    accountant = RecordingBudgetAccountant()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        repaired = len(requests) == 2
        return httpx.Response(
            200,
            headers={"x-request-id": f"req_repair_parse_{len(requests)}"},
            json={
                "id": f"resp_repair_parse_{len(requests)}",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    make_output().model_dump_json()
                                    if repaired
                                    else json.dumps({"findings": []})
                                ),
                            }
                        ],
                    }
                ],
                "usage": (
                    {"input_tokens": "bad", "output_tokens": 3}
                    if repaired
                    else {"input_tokens": 10, "output_tokens": 2}
                ),
            },
        )

    ticks = iter((10.0, 11.0, 20.0, 22.0))
    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=client,
        monotonic=lambda: next(ticks),
    )

    with model_budget_scope(accountant), pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    error = captured.value.error
    assert len(requests) == 2
    assert error.code is ErrorCode.MODEL_INVALID_RESPONSE
    assert error.details["format_repair_attempted"] is True
    assert error.details["provider_request_id"] == (
        "req_repair_parse_1,req_repair_parse_2"
    )
    assert error.details["status_code"] == 200
    assert error.details["duration_ms"] == 3_000
    assert len(accountant.settlements) == 2
    assert accountant.settlements[0]["uncertain"] is False
    assert accountant.settlements[0]["input_tokens"] == 10
    assert accountant.settlements[0]["output_tokens"] == 2
    assert accountant.settlements[1] == {
        "reservation_id": "reservation-2",
        "input_tokens": None,
        "output_tokens": None,
        "estimated_cost_microusd": None,
        "response_status": 200,
        "duration_ms": 2_000,
        "uncertain": True,
    }
    assert {
        item["reservation_id"] for item in accountant.settlements
    } == {"reservation-1", "reservation-2"}
    client.close()


def test_openai_chat_completions_request_and_usage_are_normalized() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-only-api-key"
        assert body["store"] is False
        assert body["reasoning_effort"] == "high"
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
            reasoning_effort=ModelReasoningEffort.HIGH,
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
        assert body["output_config"]["effort"] == "max"
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
            reasoning_effort=ModelReasoningEffort.MAX,
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


def test_optional_reasoning_parameter_is_omitted_for_relay_compatibility() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "reasoning" not in body
        return httpx.Response(
            200,
            json={
                "id": "resp_without_reasoning",
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
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=client,
    )

    assert reviewer.review(make_model_input()).status is ModelCallStatus.SUCCEEDED
    client.close()


def test_chat_retries_once_when_relay_rejects_reasoning_effort() -> None:
    requests: list[dict[str, object]] = []
    accountant = RecordingBudgetAccountant()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "param": "reasoning_effort",
                        "message": "Unsupported parameter: reasoning_effort",
                    }
                },
            )
        assert "reasoning_effort" not in body
        return httpx.Response(
            200,
            json={
                "id": "chat-fallback-reasoning",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": make_output().model_dump_json(),
                            "refusal": None,
                        },
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    client = httpx.Client(
        base_url="https://relay.example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
            api_base_url="https://relay.example.test/v1",
            reasoning_effort=ModelReasoningEffort.HIGH,
        ),
        client=client,
    )

    with model_budget_scope(accountant):
        result = reviewer.review(make_model_input())

    assert result.status is ModelCallStatus.SUCCEEDED
    assert len(requests) == 2
    assert len(accountant.requests) == 2
    assert [item["input_tokens"] for item in accountant.settlements] == [0, 2]
    assert [item["output_tokens"] for item in accountant.settlements] == [0, 1]
    assert all(item["uncertain"] is False for item in accountant.settlements)
    client.close()


def test_chat_retries_with_legacy_max_tokens_only_when_explicitly_rejected() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert "max_completion_tokens" in body
            return httpx.Response(
                422,
                json={
                    "error": {
                        "code": "unsupported_parameter",
                        "param": "max_completion_tokens",
                        "message": "This relay does not support max_completion_tokens",
                    }
                },
            )
        assert body["max_tokens"] == 8192
        assert "max_completion_tokens" not in body
        return httpx.Response(
            200,
            json={
                "id": "chat-fallback-max-tokens",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": make_output().model_dump_json(),
                            "refusal": None,
                        },
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    client = httpx.Client(
        base_url="https://relay.example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
            api_base_url="https://relay.example.test/v1",
        ),
        client=client,
    )

    assert reviewer.review(make_model_input()).status is ModelCallStatus.SUCCEEDED
    assert len(requests) == 2
    client.close()


def test_custom_chat_relay_omits_store_and_recovers_5xx_parameter_rejection() -> None:
    """兼容端即使把参数校验错误包装成 5xx，也应有界降级一次。"""

    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        # ``store`` 与审查语义无关，且不少中转站的请求模型不接受它。
        assert "store" not in body
        if len(requests) == 1:
            return httpx.Response(
                502,
                json={
                    "error": {
                        "param": "max_completion_tokens",
                        "message": "invalid parameter max_completion_tokens",
                    }
                },
            )
        assert "max_completion_tokens" not in body
        assert body["max_tokens"] == 8192
        return httpx.Response(
            200,
            json={
                "id": "chat-5xx-fallback",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": make_output().model_dump_json(),
                            "refusal": None,
                        },
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    base_url = "https://relay.example.test/v1"
    client = httpx.Client(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
            api_base_url=base_url,
        ),
        client=client,
    )

    result = reviewer.review(make_model_input())

    assert result.status is ModelCallStatus.SUCCEEDED
    assert len(requests) == 2
    client.close()


def test_chat_downgrades_json_schema_to_json_object_and_still_validates_locally() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body["response_format"]["type"] == "json_schema"
            return httpx.Response(
                400,
                json={
                    "error": {
                        "param": "response_format.json_schema",
                        "message": "json_schema is not supported by this relay",
                    }
                },
            )
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "id": "chat-fallback-json-object",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": make_output().model_dump_json(),
                            "refusal": None,
                        },
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    client = httpx.Client(
        base_url="https://relay.example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
            api_base_url="https://relay.example.test/v1",
        ),
        client=client,
    )

    result = reviewer.review(make_model_input())

    assert result.status is ModelCallStatus.SUCCEEDED
    assert len(result.output.findings) == 1
    assert len(requests) == 2
    client.close()


@pytest.mark.parametrize(
    ("protocol", "supported_phrase"),
    [
        (ModelApiProtocol.CHAT_COMPLETIONS, "Supported values are"),
        (ModelApiProtocol.RESPONSES, "Allowed values are"),
        (ModelApiProtocol.CHAT_COMPLETIONS, "Expected one of"),
    ],
)
def test_relay_invalid_value_supported_values_triggers_safe_schema_fallback(
    protocol: ModelApiProtocol,
    supported_phrase: str,
) -> None:
    """兼容端常见的值域文案也应触发一次安全 Schema 降级。"""

    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            if protocol is ModelApiProtocol.CHAT_COMPLETIONS:
                assert body["response_format"]["type"] == "json_schema"
                error = {
                    "error": {
                        "param": "response_format",
                        "message": (
                            f"Invalid value: 'json_schema'. {supported_phrase}: "
                            "'text', 'json_object'."
                        ),
                    }
                }
            else:
                assert body["text"]["format"]["type"] == "json_schema"
                error = {
                    "error": {
                        "param": "text.format",
                        "message": (
                            f"Invalid value: 'json_schema'. {supported_phrase}: "
                            "'text', 'json_object'."
                        ),
                    }
                }
            return httpx.Response(400, json=error)

        if protocol is ModelApiProtocol.CHAT_COMPLETIONS:
            assert body["response_format"] == {"type": "json_object"}
            payload = {
                "id": "chat-supported-values-fallback",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": make_output().model_dump_json(),
                            "refusal": None,
                        },
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            }
        else:
            assert body["text"] == {"format": {"type": "json_object"}}
            payload = {
                "id": "responses-supported-values-fallback",
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
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }
        return httpx.Response(200, json=payload)

    base_url = "https://relay.example.test/v1"
    client = httpx.Client(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=protocol,
            api_base_url=base_url,
        ),
        client=client,
    )

    result = reviewer.review(make_model_input())

    assert result.status is ModelCallStatus.SUCCEEDED
    assert len(requests) == 2
    client.close()


def test_invalid_supported_values_for_unknown_parameter_do_not_retry() -> None:
    """值域措辞本身不足以触发重试，未知参数必须保持原始拒绝。"""

    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            400,
            json={
                "error": {
                    "param": "temperature",
                    "message": (
                        "Invalid value: 3. Supported values are between 0 and 2."
                    ),
                }
            },
        )

    client = httpx.Client(
        base_url="https://relay.example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
            api_base_url="https://relay.example.test/v1",
        ),
        client=client,
    )

    with pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert requests == 1
    assert captured.value.error.code is ErrorCode.MODEL_REQUEST_REJECTED
    assert "unsupported_parameters" not in captured.value.error.details
    client.close()


def test_invalid_value_without_supported_list_for_known_parameter_does_not_retry() -> None:
    """仅有 Invalid value 文案时，即使字段已知也不能猜测兼容降级。"""

    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            422,
            json={
                "error": {
                    "param": "response_format",
                    "message": "Invalid value for response_format.",
                }
            },
        )

    client = httpx.Client(
        base_url="https://relay.example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
            api_base_url="https://relay.example.test/v1",
        ),
        client=client,
    )

    with pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert requests == 1
    assert captured.value.error.code is ErrorCode.MODEL_REQUEST_REJECTED
    assert "unsupported_parameters" not in captured.value.error.details
    client.close()


def test_generic_bad_request_is_not_retried_or_exposed() -> None:
    calls = 0
    secret = "sk-hidden-relay-error-123456789"

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            400,
            json={"error": {"message": f"invalid repository: {secret}"}},
        )

    client = httpx.Client(
        base_url="https://relay.example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
            api_base_url="https://relay.example.test/v1",
        ),
        client=client,
    )

    with pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert calls == 1
    assert secret not in str(captured.value)
    assert secret not in str(captured.value.error.details)
    client.close()


@pytest.mark.parametrize("status_code", [408, 429, 500, 529])
def test_retryable_provider_errors_keep_budget_reservation_uncertain(
    status_code: int,
) -> None:
    accountant = RecordingBudgetAccountant()
    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status_code, json={"error": {}})
        ),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        ),
        client=client,
    )

    with model_budget_scope(accountant), pytest.raises(SafeApplicationError):
        reviewer.review(make_model_input())

    assert len(accountant.settlements) == 1
    assert accountant.settlements[0]["input_tokens"] is None
    assert accountant.settlements[0]["output_tokens"] is None
    assert accountant.settlements[0]["uncertain"] is True
    client.close()


def test_budget_settlement_exception_does_not_trigger_a_second_settlement() -> None:
    """结算已落库但抛出预算错误时，异常分支不能重复调用结算。"""

    class CommitThenRaiseAccountant(RecordingBudgetAccountant):
        def settle(self, *args: object, **kwargs: object) -> None:
            super().settle(*args, **kwargs)  # type: ignore[arg-type]
            raise ModelBudgetExceededError("http_calls")

    accountant = CommitThenRaiseAccountant()
    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(503, json={"error": {}})
        ),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=client,
    )

    with model_budget_scope(accountant), pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert captured.value.error.code is ErrorCode.MODEL_BUDGET_EXCEEDED
    assert len(accountant.settlements) == 1
    client.close()


def test_transient_budget_settlement_error_is_retried_idempotently() -> None:
    """结算暂时失败时，必须补偿一次，避免 reservation 永久保持 reserved。"""

    class RetryOnceAccountant(RecordingBudgetAccountant):
        def __init__(self) -> None:
            super().__init__()
            self.settle_attempts = 0

        def settle(self, *args: object, **kwargs: object) -> None:
            self.settle_attempts += 1
            if self.settle_attempts == 1:
                raise TaskQueueError("temporary settlement failure")
            super().settle(*args, **kwargs)  # type: ignore[arg-type]

    accountant = RetryOnceAccountant()
    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(400, json={"error": {}})
        ),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=client,
        monotonic=lambda: 100.0,
    )

    with model_budget_scope(accountant), pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert captured.value.error.code is ErrorCode.MODEL_REQUEST_REJECTED
    assert accountant.settle_attempts == 2
    assert len(accountant.settlements) == 1
    assert accountant.settlements[0] == {
        "reservation_id": "reservation-1",
        "input_tokens": None,
        "output_tokens": None,
        "estimated_cost_microusd": None,
        "response_status": 400,
        "duration_ms": 0,
        "uncertain": True,
    }
    client.close()


def test_telemetry_failure_cannot_leave_a_successful_budget_reserved() -> None:
    """指标旁路异常不能阻断响应解析和预算结算。"""

    class ExplodingTelemetry:
        def observe_external(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("telemetry backend unavailable")

    accountant = RecordingBudgetAccountant()
    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "id": "resp_telemetry_failure",
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
                    "usage": {"input_tokens": 12, "output_tokens": 8},
                },
            )
        ),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=client,
        telemetry=ExplodingTelemetry(),  # type: ignore[arg-type]
    )

    with model_budget_scope(accountant):
        result = reviewer.review(make_model_input())

    assert result.status is ModelCallStatus.SUCCEEDED
    assert len(accountant.settlements) == 1
    assert accountant.settlements[0]["uncertain"] is False
    assert accountant.settlements[0]["input_tokens"] == 12
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


def test_relay_gateway_timeout_is_explained_without_exposing_body() -> None:
    client = httpx.Client(
        base_url="https://relay.example.test/v1",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                524,
                headers={"retry-after": "120"},
                content=b"origin response timed out; secret=must-not-escape",
            )
        ),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.RESPONSES,
            api_base_url="https://relay.example.test/v1",
        ),
        client=client,
    )

    with pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    error = captured.value.error
    assert error.code is ErrorCode.MODEL_SERVER_ERROR
    assert error.safe_message == "模型中转站网关等待超时（524）"
    assert error.retryable is True
    assert error.details["upstream_timeout"] is True
    assert error.details["retry_after_seconds"] == 120
    assert "secret=must-not-escape" not in str(error.details)
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


def test_responses_truncation_retry_succeeds_with_compact_request_and_usage() -> None:
    requests: list[dict[str, object]] = []
    accountant = RecordingBudgetAccountant()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body["max_output_tokens"] == 16_384
            return httpx.Response(
                200,
                headers={"x-request-id": "req_truncated_1"},
                json={
                    "id": "resp_truncated_1",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 8,
                        "input_tokens_details": {"cached_tokens": 2},
                        "output_tokens_details": {"reasoning_tokens": 3},
                    },
                },
            )
        assert body["max_output_tokens"] == 8_192
        assert "reasoning" not in body
        assert "最多 8 条" in body["input"][-1]["content"][0]["text"]
        return httpx.Response(
            200,
            headers={"x-request-id": "req_truncated_2"},
            json={
                "id": "resp_truncated_2",
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
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        )

    ticks = iter((10.0, 11.0, 20.0, 22.0))
    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.RESPONSES,
            reasoning_effort=ModelReasoningEffort.MEDIUM,
            max_output_tokens=16_384,
        ),
        client=client,
        monotonic=lambda: next(ticks),
    )

    with model_budget_scope(accountant):
        result = reviewer.review(make_model_input())

    assert len(requests) == 2
    assert result.provider_response_id == "resp_truncated_2"
    assert result.provider_request_id == "req_truncated_1,req_truncated_2"
    assert result.response_status == 200
    assert result.duration_ms == 3_000
    assert result.usage.input_tokens == 20
    assert result.usage.cache_read_input_tokens == 2
    assert result.usage.output_tokens == 11
    assert result.usage.reasoning_output_tokens == 3
    assert accountant.settlements == [
        {
            "reservation_id": "reservation-1",
            "input_tokens": 10,
            "output_tokens": 8,
            "estimated_cost_microusd": None,
            "response_status": 200,
            "duration_ms": 1_000,
            "uncertain": False,
        },
        {
            "reservation_id": "reservation-2",
            "input_tokens": 12,
            "output_tokens": 3,
            "estimated_cost_microusd": None,
            "response_status": 200,
            "duration_ms": 2_000,
            "uncertain": False,
        },
    ]
    client.close()


def test_responses_truncation_retry_failure_preserves_usage_and_audit() -> None:
    requests = 0
    accountant = RecordingBudgetAccountant()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            headers={"x-request-id": f"req_fail_{requests}"},
            json={
                "id": f"resp_fail_{requests}",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
                "usage": {
                    "input_tokens": 7 + requests,
                    "output_tokens": 5 + requests,
                },
            },
        )

    ticks = iter((30.0, 31.0, 40.0, 42.0))
    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.RESPONSES,
            max_output_tokens=16_384,
        ),
        client=client,
        monotonic=lambda: next(ticks),
    )

    with model_budget_scope(accountant), pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    error = captured.value.error
    assert requests == 2
    assert error.code is ErrorCode.MODEL_OUTPUT_TRUNCATED
    assert error.retryable is False
    assert error.details["compact_retry_attempted"] is True
    assert error.details["incomplete_reason"] == "max_output_tokens"
    assert error.details["initial_incomplete_reason"] == "max_output_tokens"
    assert error.details["initial_status_code"] == 200
    assert error.details["status_code"] == 200
    assert error.details["initial_duration_ms"] == 1_000
    assert error.details["duration_ms"] == 3_000
    assert error.details["provider_request_id"] == "req_fail_1,req_fail_2"
    assert error.details["failed_input_tokens"] == 17
    assert error.details["failed_output_tokens"] == 13
    assert [item["uncertain"] for item in accountant.settlements] == [False, False]
    client.close()


def test_truncation_retry_parse_error_settles_retry_reservation() -> None:
    """截断后的精简响应用量损坏时，不得遗留第二次 reservation。"""

    requests: list[dict[str, object]] = []
    accountant = RecordingBudgetAccountant()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        retried = len(requests) == 2
        if not retried:
            payload = {
                "id": "resp_compact_parse_1",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
                "usage": {"input_tokens": 9, "output_tokens": 6},
            }
        else:
            payload = {
                "id": "resp_compact_parse_2",
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
                "usage": {"input_tokens": "bad", "output_tokens": 4},
            }
        return httpx.Response(
            200,
            headers={"x-request-id": f"req_compact_parse_{len(requests)}"},
            json=payload,
        )

    ticks = iter((30.0, 31.0, 40.0, 42.0))
    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.RESPONSES,
            max_output_tokens=16_384,
        ),
        client=client,
        monotonic=lambda: next(ticks),
    )

    with model_budget_scope(accountant), pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    error = captured.value.error
    assert len(requests) == 2
    assert error.code is ErrorCode.MODEL_INVALID_RESPONSE
    assert error.details["compact_retry_attempted"] is True
    assert error.details["initial_error_code"] == ErrorCode.MODEL_OUTPUT_TRUNCATED.value
    assert error.details["initial_incomplete_reason"] == "max_output_tokens"
    assert error.details["provider_request_id"] == (
        "req_compact_parse_1,req_compact_parse_2"
    )
    assert error.details["status_code"] == 200
    assert error.details["duration_ms"] == 3_000
    assert error.details["failed_input_tokens"] == 9
    assert error.details["failed_output_tokens"] == 6
    assert len(accountant.settlements) == 2
    assert accountant.settlements[0]["uncertain"] is False
    assert accountant.settlements[1] == {
        "reservation_id": "reservation-2",
        "input_tokens": None,
        "output_tokens": None,
        "estimated_cost_microusd": None,
        "response_status": 200,
        "duration_ms": 2_000,
        "uncertain": True,
    }
    assert {
        item["reservation_id"] for item in accountant.settlements
    } == {"reservation-1", "reservation-2"}
    client.close()


def test_responses_content_filter_is_refusal_and_does_not_retry() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={
                "id": "resp_filter",
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
                "output": [],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )

    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI, api_protocol=ModelApiProtocol.RESPONSES),
        client=client,
    )

    with pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert requests == 1
    assert captured.value.error.code is ErrorCode.MODEL_OUTPUT_REFUSED
    assert captured.value.error.details["incomplete_reason"] == "content_filter"
    assert captured.value.error.details["failed_input_tokens"] == 4
    assert captured.value.error.details["failed_output_tokens"] == 2
    client.close()


@pytest.mark.parametrize("reason", [None, "server_error", ["max_output_tokens"]])
def test_responses_unknown_incomplete_reason_does_not_retry(reason: object) -> None:
    """只有明确的长度原因才允许精简重试，未知原因不能额外消耗预算。"""

    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        incomplete_details = {} if reason is None else {"reason": reason}
        return httpx.Response(
            200,
            json={
                "id": "resp_unknown_incomplete",
                "status": "incomplete",
                "incomplete_details": incomplete_details,
                "output": [],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )

    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI, api_protocol=ModelApiProtocol.RESPONSES),
        client=client,
    )

    with pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert requests == 1
    assert captured.value.error.code is ErrorCode.MODEL_OUTPUT_TRUNCATED
    assert captured.value.error.details.get("incomplete_reason") == reason
    client.close()


@pytest.mark.parametrize(
    ("provider", "protocol", "limit_field"),
    [
        (
            ModelProvider.OPENAI,
            ModelApiProtocol.CHAT_COMPLETIONS,
            "max_completion_tokens",
        ),
        (ModelProvider.ANTHROPIC, ModelApiProtocol.MESSAGES, "max_tokens"),
    ],
)
def test_non_responses_truncation_retry_uses_compact_limit(
    provider: ModelProvider,
    protocol: ModelApiProtocol,
    limit_field: str,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body[limit_field] == 16_384
            return httpx.Response(
                200,
                json=(
                    {
                        "id": "chat_truncated",
                        "choices": [
                            {"finish_reason": "length", "message": {"content": ""}}
                        ],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                    }
                    if protocol is ModelApiProtocol.CHAT_COMPLETIONS
                    else {
                        "id": "message_truncated",
                        "stop_reason": "max_tokens",
                        "content": [],
                        "usage": {"input_tokens": 2, "output_tokens": 3},
                    }
                ),
            )
        assert body[limit_field] == 8_192
        if protocol is ModelApiProtocol.CHAT_COMPLETIONS:
            assert "reasoning_effort" not in body
            assert "最多 8 条" in body["messages"][-1]["content"]
            payload = {
                "id": "chat_retried",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": make_output().model_dump_json(),
                            "refusal": None,
                        },
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 5},
            }
        else:
            assert "effort" not in body["output_config"]
            assert "最多 8 条" in body["messages"][-1]["content"]
            payload = {
                "id": "message_retried",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": make_output().model_dump_json()}],
                "usage": {"input_tokens": 4, "output_tokens": 5},
            }
        return httpx.Response(200, json=payload)

    base_url = (
        "https://api.openai.test/v1"
        if protocol is ModelApiProtocol.CHAT_COMPLETIONS
        else "https://api.anthropic.test"
    )
    client = httpx.Client(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            provider,
            api_protocol=protocol,
            api_base_url=base_url,
            max_output_tokens=16_384,
            reasoning_effort=ModelReasoningEffort.HIGH,
        ),
        client=client,
    )

    result = reviewer.review(make_model_input())

    assert result.status is ModelCallStatus.SUCCEEDED
    assert len(requests) == 2
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


def test_summary_context_without_units_still_calls_model() -> None:
    """汇总阶段只携带前三路结论时不能被普通空计划捷径跳过。"""

    review_input = make_model_input().model_copy(
        update={
            "rules": (),
            "units": (),
            "total_estimated_input_bytes": 0,
            "review_agent": ReviewAgent.SUMMARY,
            "prior_agent_results": ('{"agent":"security","findings":[]}',),
        }
    )
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "id": "summary-response",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": make_output().model_dump_json()},
                }],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            },
        )

    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        ),
        client=client,
    )

    result = reviewer.review(review_input)

    assert calls == 1
    assert result.status is ModelCallStatus.SUCCEEDED
    assert result.output.findings
    client.close()


def test_non_summary_context_without_units_still_skips_http() -> None:
    """临时上下文不能把普通 Agent 的空计划误变成真实模型请求。"""

    review_input = make_model_input().model_copy(
        update={
            "rules": (),
            "units": (),
            "total_estimated_input_bytes": 0,
            "review_agent": ReviewAgent.SECURITY,
            "prior_agent_results": ('{"agent":"summary","findings":[]}',),
        }
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
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        ),
        client=client,
    )

    result = reviewer.review(review_input)

    assert calls == 0
    assert result.status is ModelCallStatus.SKIPPED
    client.close()


def test_summary_provider_compacts_direct_large_input_before_http() -> None:
    """即使绕过编排器直接调用，汇总适配器也不能发送完整补丁。"""

    source = make_large_v2_input()
    review_input = source.model_copy(
        update={
            "review_agent": ReviewAgent.SUMMARY,
            "prior_agent_results": ('{"agent":"security","findings":[]}',),
        }
    )
    request_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_sizes.append(len(request.content))
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        assert payload["review_units"] == []
        assert payload["repository_rules"] == []
        return httpx.Response(
            200,
            json={
                "id": "summary-compact",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": make_output().model_dump_json()},
                }],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            },
        )

    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.OPENAI,
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        ),
        client=client,
    )

    result = reviewer.review(review_input)

    assert result.status is ModelCallStatus.SUCCEEDED
    assert request_sizes and request_sizes[0] < 100_000
    client.close()


def test_timeout_keeps_the_full_budget_reservation_once() -> None:
    accountant = RecordingBudgetAccountant()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("test timeout", request=request)

    ticks = iter((10.0, 12.0))
    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=client,
        monotonic=lambda: next(ticks),
    )

    with model_budget_scope(accountant), pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert captured.value.error.code is ErrorCode.MODEL_TIMEOUT
    assert len(accountant.requests) == 1
    assert accountant.requests[0].cost_upper_bound_microusd is None
    assert accountant.settlements == [
        {
            "reservation_id": "reservation-1",
            "input_tokens": None,
            "output_tokens": None,
            "estimated_cost_microusd": None,
            "response_status": None,
            "duration_ms": 2000,
            "uncertain": True,
        }
    ]
    client.close()


def test_unexpected_client_exception_settles_budget_reservation() -> None:
    """非 httpx 异常也不能让预算行停留在 reserved。"""

    class ExplodingClient:
        def stream(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("transport secret must not escape")

        def close(self) -> None:
            return None

    accountant = RecordingBudgetAccountant()
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=ExplodingClient(),  # type: ignore[arg-type]
    )

    with model_budget_scope(accountant), pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert captured.value.error.code is ErrorCode.MODEL_SERVER_ERROR
    assert captured.value.error.retryable is True
    assert "transport secret" not in str(captured.value.error.details)
    assert len(accountant.requests) == 1
    assert accountant.settlements == [
        {
            "reservation_id": "reservation-1",
            "input_tokens": None,
            "output_tokens": None,
            "estimated_cost_microusd": None,
            "response_status": None,
            "duration_ms": 0,
            "uncertain": True,
        }
    ]


def test_unexpected_response_iterator_exception_preserves_http_audit() -> None:
    class ExplodingResponse:
        status_code = 200
        headers = {"x-request-id": "request-before-iterator-error"}

        def __enter__(self) -> "ExplodingResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def iter_bytes(self) -> object:
            raise RuntimeError("response iterator failed")

    class ExplodingClient:
        def stream(self, *_args: object, **_kwargs: object) -> ExplodingResponse:
            return ExplodingResponse()

        def close(self) -> None:
            return None

    accountant = RecordingBudgetAccountant()
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=ExplodingClient(),  # type: ignore[arg-type]
    )

    with model_budget_scope(accountant), pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    error = captured.value.error
    assert error.code is ErrorCode.MODEL_SERVER_ERROR
    assert error.details["status_code"] == 200
    assert error.details["provider_request_id"] == "request-before-iterator-error"
    assert len(accountant.settlements) == 1
    assert accountant.settlements[0]["uncertain"] is True


def test_timeout_response_iterator_preserves_http_audit() -> None:
    """响应头已到达后读超时，不能丢失状态码和供应商请求 ID。"""

    class TimeoutResponse:
        status_code = 200
        headers = {"x-request-id": "request-before-timeout"}

        def __enter__(self) -> "TimeoutResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def iter_bytes(self) -> object:
            raise httpx.ReadTimeout(
                "response body timed out",
                request=httpx.Request(
                    "POST",
                    "https://api.openai.test/v1/responses",
                ),
            )

    class TimeoutClient:
        def stream(self, *_args: object, **_kwargs: object) -> TimeoutResponse:
            return TimeoutResponse()

        def close(self) -> None:
            return None

    accountant = RecordingBudgetAccountant()
    # _post_json samples the clock at request start and when the body read
    # raises; the latter is the complete elapsed duration.
    ticks = iter((10.0, 12.5))
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=TimeoutClient(),  # type: ignore[arg-type]
        monotonic=lambda: next(ticks),
    )

    with model_budget_scope(accountant), pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    error = captured.value.error
    assert error.code is ErrorCode.MODEL_TIMEOUT
    assert error.details["status_code"] == 200
    assert error.details["provider_request_id"] == "request-before-timeout"
    assert accountant.settlements == [
        {
            "reservation_id": "reservation-1",
            "input_tokens": None,
            "output_tokens": None,
            "estimated_cost_microusd": None,
            "response_status": 200,
            "duration_ms": 2_500,
            "uncertain": True,
        }
    ]


def test_http_classification_iterator_exception_does_not_settle_twice() -> None:
    """HTTP 错误分类异常时，已结算的 reservation 不能再次结算。"""

    class ClassificationErrorResponse:
        status_code = 400
        headers = {"x-request-id": "request-before-classification-error"}

        def __enter__(self) -> "ClassificationErrorResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def iter_bytes(self) -> object:
            # _unsupported_parameters_from_response 只吞掉明确的 HTTP/运行时/
            # 值错误；用 KeyError 模拟第三方响应迭代器的未预期异常。
            raise KeyError("iterator failed")

    class ClassificationErrorClient:
        def stream(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> ClassificationErrorResponse:
            return ClassificationErrorResponse()

        def close(self) -> None:
            return None

    accountant = RecordingBudgetAccountant()
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=ClassificationErrorClient(),  # type: ignore[arg-type]
    )

    with model_budget_scope(accountant), pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    error = captured.value.error
    assert error.code is ErrorCode.MODEL_SERVER_ERROR
    assert error.details["status_code"] == 400
    assert error.details["provider_request_id"] == (
        "request-before-classification-error"
    )
    assert len(accountant.settlements) == 1
    assert accountant.settlements[0]["uncertain"] is False
    assert accountant.settlements[0]["response_status"] == 400


def test_invalid_json_keeps_the_full_budget_reservation_once() -> None:
    accountant = RecordingBudgetAccountant()
    telemetry = TelemetryRegistry()
    ticks = iter((10.0, 13.0))
    client = httpx.Client(
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"not-json")
        ),
    )
    reviewer = create_model_reviewer(
        _settings(ModelProvider.OPENAI),
        client=client,
        monotonic=lambda: next(ticks),
        telemetry=telemetry,
    )

    with model_budget_scope(accountant), pytest.raises(SafeApplicationError) as captured:
        reviewer.review(make_model_input())

    assert captured.value.error.code is ErrorCode.MODEL_INVALID_RESPONSE
    assert captured.value.error.details["duration_ms"] == 3_000
    assert len(accountant.requests) == 1
    assert len(accountant.settlements) == 1
    assert accountant.settlements[0]["uncertain"] is True
    assert accountant.settlements[0]["input_tokens"] is None
    assert accountant.settlements[0]["duration_ms"] == 3_000
    assert (
        'openreviewer_external_http_request_duration_seconds_sum{'
        'service="model_openai",outcome="invalid_response"} 3.000000'
        in telemetry.render()
    )
    client.close()


def test_unknown_cache_price_keeps_cost_reservation_conservatively() -> None:
    accountant = RecordingBudgetAccountant()
    client = httpx.Client(
        base_url="https://api.anthropic.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "id": "msg_unknown_cache_price",
                    "stop_reason": "end_turn",
                    "content": [
                        {"type": "text", "text": make_output().model_dump_json()}
                    ],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 30,
                    },
                },
            )
        ),
    )
    reviewer = create_model_reviewer(
        _settings(
            ModelProvider.ANTHROPIC,
            ModelPricing(
                input_usd_per_million=Decimal("3"),
                output_usd_per_million=Decimal("15"),
            ),
        ),
        client=client,
    )

    with model_budget_scope(accountant):
        result = reviewer.review(make_model_input())

    assert result.estimated_cost_microusd is None
    assert len(accountant.settlements) == 1
    assert accountant.settlements[0]["uncertain"] is True
    assert accountant.settlements[0]["estimated_cost_microusd"] is None
    client.close()
