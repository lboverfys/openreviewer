"""指定评测请求的输出证据；只保留可见答案，不收集供应商思维链。"""

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from domain.security import redact_text

MAX_OUTPUT_BYTES = 256 * 1024
MAX_RUN_OUTPUT_BYTES = 8 * 1024 * 1024
OutputStatus = Literal["pending", "captured", "parse_failed", "transport_failed", "oversized", "run_limit", "missing", "expired"]


@dataclass(frozen=True, slots=True)
class ModelOutputSource:
    review_run_id: str
    review_plan_id: str
    head_sha: str
    prompt_content_sha256: str
    application_revision: str | None
    batch_number: int | None = None
    split_depth: int = 0


@dataclass(frozen=True, slots=True)
class CapturedModelOutput:
    status: OutputStatus
    format: str = "provider_output_text"
    text: str | None = None
    sha256: str | None = None
    byte_size: int = 0
    error_code: str | None = None


def capture_output_text(payload: dict[str, object], protocol: str, *, streamed: bool) -> CapturedModelOutput:
    """按协议白名单提取回答文本，排除 reasoning/thinking、请求头和未知字段。"""
    texts: list[str] = []
    if protocol == "chat_completions":
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                message = choice.get("message") if isinstance(choice, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    texts.extend(item["text"] for item in content if isinstance(item, dict)
                                 and item.get("type") == "text" and isinstance(item.get("text"), str))
    elif protocol == "responses":
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                content = item.get("content")
                if isinstance(content, list):
                    texts.extend(part["text"] for part in content if isinstance(part, dict)
                                 and part.get("type") == "output_text" and isinstance(part.get("text"), str))
        direct_text = payload.get("output_text")
        if not texts and isinstance(direct_text, str):
            texts.append(direct_text)
    elif protocol == "messages":
        content = payload.get("content")
        if isinstance(content, list):
            texts.extend(item["text"] for item in content if isinstance(item, dict)
                         and item.get("type") == "text" and isinstance(item.get("text"), str))
    output_format = "stream_reassembled_output_text" if streamed else "provider_output_text"
    if not texts:
        return CapturedModelOutput(status="missing", format=output_format)
    # 先限制原文本的字节量，再脱敏和哈希；从不保存截断正文冒充完整输出。
    if sum(len(item.encode("utf-8")) for item in texts) + len(texts) - 1 > MAX_OUTPUT_BYTES:
        return CapturedModelOutput(status="oversized", format=output_format)
    value = redact_text("\n".join(texts))
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        return CapturedModelOutput(status="oversized", format=output_format)
    return CapturedModelOutput(status="captured", format=output_format, text=value,
                               sha256=sha256(encoded).hexdigest(), byte_size=len(encoded))
