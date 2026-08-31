"""模型输入 Token 的离线估算与安全预算上界。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from domain.enums import ModelApiProtocol, ModelProvider

_MAX_SEMANTIC_NODES = 100_000
_ESTIMATED_UTF8_BYTES_PER_TOKEN = 2
# 规划阶段已经以 ``2 UTF-8 字节/Token`` 计算批次。发送前预算只需要一
# 个有限余量来覆盖 HTTP 信封字段、协议差异和不同供应商 tokenizer 的小
# 偏差；直接使用 UTF-8 字节数会让有效请求的预留接近翻倍，固定四路
# Agent 在重试时很容易被错误地判定为耗尽输入预算。
_RESERVATION_MARGIN_PERCENT = 5
# Keep the send-time reservation aligned with the planner's documented
# context safety margin.  A fixed floor matters for small requests where a
# percentage-only margin would not cover protocol envelopes or tokenizer
# variance.
_MIN_RESERVATION_MARGIN_TOKENS = 4_096
_PROTOCOL_BASE_TOKENS = {
    ModelApiProtocol.RESPONSES: 96,
    ModelApiProtocol.CHAT_COMPLETIONS: 80,
    ModelApiProtocol.MESSAGES: 72,
}


@dataclass(frozen=True, slots=True)
class ModelInputTokenEstimate:
    """同一请求的常规估算值和发送前预算边界。"""

    estimated_tokens: int
    upper_bound_tokens: int
    semantic_bytes: int
    serialized_bytes: int
    used_fallback: bool = False

    @property
    def reservation_tokens(self) -> int:
        """返回资源统计或兼容硬限制所需的输入 Token 上界。

        对可识别的请求，规划和实际预留使用同一估算口径，并增加有限的
        5%（至少 4096 Token）余量。``upper_bound_tokens`` 仍保留为结构化
        诊断上界，但不再让正常请求的预留量接近原始 UTF-8 字节数。结构
        无法识别时则必须使用完整序列化字节数这一保守退路，避免异常请求
        绕过累计资源阈值。
        """

        if self.used_fallback:
            return self.upper_bound_tokens
        margin = max(
            _MIN_RESERVATION_MARGIN_TOKENS,
            _ceil_div(
                self.estimated_tokens * _RESERVATION_MARGIN_PERCENT,
                100,
            ),
        )
        return max(
            self.estimated_tokens,
            min(
                self.upper_bound_tokens,
                self.estimated_tokens + margin,
            ),
        )


@dataclass(frozen=True, slots=True)
class _SemanticSize:
    bytes: int
    strings: int
    scalars: int
    containers: int


def estimated_utf8_bytes_per_token(
    provider: ModelProvider,
    protocol: ModelApiProtocol,
    model: str,
) -> int:
    """返回批次规划使用的保守 UTF-8 字节/Token 档位。"""

    _validate_identity(provider, protocol, model)
    return _ESTIMATED_UTF8_BYTES_PER_TOKEN


def estimate_prompt_input_tokens(
    system: str,
    user: str,
    *,
    provider: ModelProvider,
    protocol: ModelApiProtocol,
    model: str,
) -> ModelInputTokenEstimate:
    """估算尚未套入 HTTP JSON 信封的 system/user Prompt。"""

    _validate_identity(provider, protocol, model)
    semantic_bytes = len(system.encode("utf-8")) + len(user.encode("utf-8"))
    overhead = _protocol_overhead(protocol, message_count=2, block_count=2)
    estimated = _ceil_div(
        semantic_bytes,
        estimated_utf8_bytes_per_token(provider, protocol, model),
    ) + overhead
    upper_bound = semantic_bytes + 4 + overhead
    return ModelInputTokenEstimate(
        estimated_tokens=estimated,
        upper_bound_tokens=max(estimated, upper_bound),
        semantic_bytes=semantic_bytes,
        serialized_bytes=semantic_bytes,
    )


def estimate_model_request_tokens(
    body: object,
    serialized_request: bytes,
    *,
    provider: ModelProvider,
    protocol: ModelApiProtocol,
    model: str,
) -> ModelInputTokenEstimate:
    """按解码后的请求语义估算 Token，并为异常结构保留安全回退。"""

    serialized_bytes = len(serialized_request)
    if serialized_bytes == 0:
        return ModelInputTokenEstimate(0, 0, 0, 0, used_fallback=True)
    try:
        _validate_identity(provider, protocol, model)
        if not isinstance(body, Mapping):
            raise ValueError("model request body must be an object")
        semantic = _semantic_size(body)
        message_count, block_count = _protocol_shape(body, protocol)
        overhead = _protocol_overhead(
            protocol,
            message_count=message_count,
            block_count=block_count,
        )
        estimated = _ceil_div(
            semantic.bytes,
            estimated_utf8_bytes_per_token(provider, protocol, model),
        ) + overhead
        structural_upper_bound = (
            semantic.bytes
            + semantic.strings
            + semantic.scalars
            + semantic.containers * 4
            + overhead
        )
        return ModelInputTokenEstimate(
            estimated_tokens=estimated,
            upper_bound_tokens=max(estimated, structural_upper_bound),
            semantic_bytes=semantic.bytes,
            serialized_bytes=serialized_bytes,
        )
    except (TypeError, ValueError, UnicodeError, OverflowError):
        # 完整序列化字节数是一条与供应商 tokenizer 无关的保守退路。
        return ModelInputTokenEstimate(
            estimated_tokens=serialized_bytes,
            upper_bound_tokens=serialized_bytes,
            semantic_bytes=serialized_bytes,
            serialized_bytes=serialized_bytes,
            used_fallback=True,
        )


def _semantic_size(value: object) -> _SemanticSize:
    byte_count = 0
    string_count = 0
    scalar_count = 0
    container_count = 0
    visited = 0
    stack = [value]
    while stack:
        current = stack.pop()
        visited += 1
        if visited > _MAX_SEMANTIC_NODES:
            raise ValueError("model request structure is too large")
        if isinstance(current, str):
            byte_count += len(current.encode("utf-8"))
            string_count += 1
            continue
        if current is None:
            byte_count += 4
            scalar_count += 1
            continue
        if isinstance(current, bool):
            byte_count += 4 if current else 5
            scalar_count += 1
            continue
        if isinstance(current, int):
            byte_count += len(str(current))
            scalar_count += 1
            continue
        if isinstance(current, float):
            if current != current or current in {float("inf"), float("-inf")}:
                raise ValueError("model request contains a non-finite number")
            byte_count += len(repr(current))
            scalar_count += 1
            continue
        if isinstance(current, Mapping):
            container_count += 1
            for key, item in current.items():
                if not isinstance(key, str):
                    raise ValueError("model request keys must be strings")
                stack.append(item)
                stack.append(key)
            continue
        if isinstance(current, Sequence) and not isinstance(
            current,
            (bytes, bytearray, memoryview),
        ):
            container_count += 1
            stack.extend(reversed(current))
            continue
        raise TypeError("model request contains an unsupported value")
    return _SemanticSize(
        bytes=byte_count,
        strings=string_count,
        scalars=scalar_count,
        containers=container_count,
    )


def _protocol_shape(
    body: Mapping[object, object],
    protocol: ModelApiProtocol,
) -> tuple[int, int]:
    raw_messages: object
    if protocol is ModelApiProtocol.RESPONSES:
        raw_messages = body.get("input")
    else:
        raw_messages = body.get("messages")
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, str):
        raise ValueError("model request messages are malformed")
    message_count = len(raw_messages)
    block_count = 0
    for message in raw_messages:
        if not isinstance(message, Mapping):
            raise ValueError("model request message is malformed")
        content = message.get("content")
        if isinstance(content, str):
            block_count += 1
        elif isinstance(content, Sequence):
            block_count += len(content)
        else:
            raise ValueError("model request content is malformed")
    return message_count, block_count


def _protocol_overhead(
    protocol: ModelApiProtocol,
    *,
    message_count: int,
    block_count: int,
) -> int:
    return _PROTOCOL_BASE_TOKENS[protocol] + message_count * 12 + block_count * 6


def _validate_identity(
    provider: ModelProvider,
    protocol: ModelApiProtocol,
    model: str,
) -> None:
    if not isinstance(provider, ModelProvider) or not isinstance(
        protocol,
        ModelApiProtocol,
    ):
        raise ValueError("model token estimation identity is invalid")
    if not model or model != model.strip() or len(model) > 200:
        raise ValueError("model token estimation model is invalid")


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor
