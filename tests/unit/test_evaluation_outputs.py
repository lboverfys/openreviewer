"""供应商边界输出捕获与结算隔离，全部使用内存传输。"""

import json
from dataclasses import replace
from hashlib import sha256

import httpx
import pytest

from domain.enums import ModelApiProtocol, ModelProvider
from domain.evaluation_outputs import MAX_OUTPUT_BYTES, capture_output_text
from services.model_budget import model_budget_scope
from services.model_providers import create_model_reviewer
from tests.unit.test_model_providers import RecordingBudgetAccountant, _settings
from tests.unit.test_model_review import make_model_input, make_output


class CapturingAccountant(RecordingBudgetAccountant):
    def __init__(self, *, enabled=True, failing=False):
        super().__init__()
        self.enabled, self.failing = enabled, failing
        self.outputs = []

    def reserve(self, request):
        reservation = super().reserve(request)
        return replace(reservation, capture_output=self.enabled and request.output_source is not None)

    def record_output(self, reservation, output):
        if self.failing:
            raise OSError("模拟证据存储不可用")
        self.outputs.append((reservation.id, output))


def payload(protocol, text):
    if protocol == "chat_completions":
        return {"id":"reply", "choices":[{"finish_reason":"stop", "message":{"content":text, "reasoning_content":"不得采集的推理"}}],
                "usage":{"prompt_tokens":100, "completion_tokens":20}}
    if protocol == "responses":
        return {"id":"reply", "status":"completed", "output":[{"type":"reasoning", "summary":[{"text":"不得采集的推理"}]},
            {"type":"message", "content":[{"type":"output_text", "text":text}]}], "usage":{"input_tokens":100, "output_tokens":20}}
    return {"id":"reply", "type":"message", "role":"assistant", "model":"test-model", "stop_reason":"end_turn",
            "content":[{"type":"thinking", "thinking":"不得采集的推理"}, {"type":"text", "text":text}],
            "usage":{"input_tokens":100, "output_tokens":20}}


@pytest.mark.parametrize("protocol", list(ModelApiProtocol))
def test_three_protocols_capture_text_before_materialization_without_reasoning(protocol):
    accountant = CapturingAccountant()
    provider = ModelProvider.ANTHROPIC if protocol is ModelApiProtocol.MESSAGES else ModelProvider.OPENAI
    raw = make_output().model_dump_json()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload(protocol.value, raw)))) as client:
        with model_budget_scope(accountant):
            result = create_model_reviewer(_settings(provider, api_protocol=protocol), client=client).review(make_model_input())
    assert result.status.value == "succeeded"
    assert len(accountant.outputs) == len(accountant.settlements) == 1
    identifier, captured = accountant.outputs[0]
    assert identifier == accountant.settlements[0]["reservation_id"]
    assert captured.status == "captured" and captured.text == raw
    assert "不得采集的推理" not in captured.text
    assert captured.byte_size == len(raw.encode())
    assert captured.sha256 == sha256(raw.encode()).hexdigest()
    assert accountant.requests[0].output_source.head_sha == make_model_input().head_sha
    assert len(accountant.requests[0].request_sha256) == 64


@pytest.mark.parametrize("enabled,failing", [(False, False), (True, True)])
def test_disabled_or_failed_capture_does_not_add_requests_or_skip_settlement(enabled, failing):
    accountant = CapturingAccountant(enabled=enabled, failing=failing)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload("chat_completions", make_output().model_dump_json())))) as client:
        with model_budget_scope(accountant):
            result = create_model_reviewer(_settings(ModelProvider.OPENAI, api_protocol=ModelApiProtocol.CHAT_COMPLETIONS), client=client).review(make_model_input())
    assert result.status.value == "succeeded"
    assert len(accountant.requests) == len(accountant.settlements) == 1
    assert not accountant.outputs


def test_parse_failure_and_repair_keep_two_distinct_attempts_and_both_outputs():
    accountant = CapturingAccountant()
    replies = iter(("{}", make_output().model_dump_json()))
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload("chat_completions", next(replies))))) as client:
        with model_budget_scope(accountant):
            create_model_reviewer(_settings(ModelProvider.OPENAI, api_protocol=ModelApiProtocol.CHAT_COMPLETIONS), client=client).review(make_model_input())
    assert [request.attempt_kind for request in accountant.requests] == ["initial", "repair"]
    assert len(accountant.settlements) == 2
    assert [(key, value.status) for key, value in accountant.outputs] == [
        ("reservation-1", "captured"), ("reservation-1", "parse_failed"), ("reservation-2", "captured"),
    ]


def test_stream_is_labeled_as_reassembled_output_and_connection_test_has_no_capture():
    response = payload("responses", make_output().model_dump_json())
    event = "data: " + json.dumps({"type":"response.completed", "response":response}) + "\n\n"
    accountant = CapturingAccountant()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=event, headers={"Content-Type":"text/event-stream"}))) as client:
        reviewer = create_model_reviewer(_settings(ModelProvider.OPENAI, api_protocol=ModelApiProtocol.RESPONSES), client=client)
        with model_budget_scope(accountant):
            reviewer.review(make_model_input())
            reviewer.review(make_model_input().model_copy(update={"connection_test":True}))
    assert len(accountant.outputs) == 1
    assert accountant.outputs[0][1].format == "stream_reassembled_output_text"
    assert accountant.requests[1].output_source is None


def test_output_limit_counts_utf8_bytes_and_hash_is_calculated_after_redaction():
    raw = "令牌 sk-" + "x" * 40
    output = capture_output_text(payload("messages", raw), "messages", streamed=False)
    assert "sk-" + "x" * 40 not in output.text
    assert output.sha256 == sha256(output.text.encode()).hexdigest()
    oversized = capture_output_text(payload("messages", "喵" * (MAX_OUTPUT_BYTES // 3 + 1)), "messages", streamed=False)
    assert oversized.status == "oversized" and oversized.text is None and oversized.sha256 is None
