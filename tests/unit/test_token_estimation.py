import json

import pytest

from domain.enums import ModelApiProtocol, ModelProvider
from services.token_estimation import (
    estimate_model_request_tokens,
    estimate_prompt_input_tokens,
)


@pytest.mark.parametrize(
    ("provider", "protocol", "body"),
    (
        (
            ModelProvider.OPENAI,
            ModelApiProtocol.RESPONSES,
            {
                "model": "gpt-test",
                "input": [
                    {"role": "system", "content": [{"type": "input_text", "text": "规则"}]},
                    {"role": "user", "content": [{"type": "input_text", "text": "补丁"}]},
                ],
            },
        ),
        (
            ModelProvider.OPENAI,
            ModelApiProtocol.CHAT_COMPLETIONS,
            {
                "model": "gateway-model",
                "messages": [
                    {"role": "system", "content": "rules"},
                    {"role": "user", "content": "patch"},
                ],
            },
        ),
        (
            ModelProvider.ANTHROPIC,
            ModelApiProtocol.MESSAGES,
            {
                "model": "claude-test",
                "system": "rules",
                "messages": [{"role": "user", "content": "patch"}],
            },
        ),
    ),
)
def test_supported_protocols_return_dual_token_estimates(
    provider: ModelProvider,
    protocol: ModelApiProtocol,
    body: dict[str, object],
) -> None:
    serialized = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()

    result = estimate_model_request_tokens(
        body,
        serialized,
        provider=provider,
        protocol=protocol,
        model=str(body["model"]),
    )

    assert 0 < result.estimated_tokens <= result.upper_bound_tokens
    assert result.serialized_bytes == len(serialized)
    assert not result.used_fallback
    assert result.estimated_tokens <= result.reservation_tokens


def test_valid_request_reservation_uses_estimate_with_bounded_margin() -> None:
    """正常请求不能按原始 UTF-8 字节数重复占用总预算。"""

    body = {
        "model": "gateway-model",
        "messages": [
            {"role": "system", "content": "规则" * 2_000},
            {"role": "user", "content": "补丁" * 2_000},
        ],
    }
    serialized = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()

    result = estimate_model_request_tokens(
        body,
        serialized,
        provider=ModelProvider.OPENAI,
        protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        model="gateway-model",
    )

    expected_margin = max(4_096, (result.estimated_tokens * 5 + 99) // 100)
    assert result.reservation_tokens == min(
        result.upper_bound_tokens,
        result.estimated_tokens + expected_margin,
    )
    assert result.reservation_tokens < result.upper_bound_tokens
    # 这里的差距接近两倍；预留应保持在估算值附近，而不是回到字节上界。
    assert result.reservation_tokens < result.upper_bound_tokens * 3 // 4


def test_escape_dense_json_does_not_charge_escape_bytes_as_prompt_tokens() -> None:
    prompt = ('"\\\n' * 2_000) + "审查"
    body = {
        "model": "gateway-model",
        "messages": [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": prompt},
        ],
    }
    serialized = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()

    result = estimate_model_request_tokens(
        body,
        serialized,
        provider=ModelProvider.OPENAI,
        protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        model="unknown-gateway-model",
    )

    assert result.upper_bound_tokens < len(serialized)
    assert result.semantic_bytes < len(serialized)
    assert not result.used_fallback


def test_malformed_request_falls_back_to_serialized_byte_count() -> None:
    serialized = b'{"unexpected":true}'

    result = estimate_model_request_tokens(
        object(),
        serialized,
        provider=ModelProvider.OPENAI,
        protocol=ModelApiProtocol.RESPONSES,
        model="gpt-test",
    )

    assert result.estimated_tokens == len(serialized)
    assert result.upper_bound_tokens == len(serialized)
    assert result.reservation_tokens == len(serialized)
    assert result.used_fallback


def test_prompt_and_request_use_the_same_estimation_profile() -> None:
    result = estimate_prompt_input_tokens(
        "system rules",
        "用户补丁",
        provider=ModelProvider.OPENAI,
        protocol=ModelApiProtocol.RESPONSES,
        model="deepseek-v4-flash",
    )

    assert 0 < result.estimated_tokens <= result.upper_bound_tokens
