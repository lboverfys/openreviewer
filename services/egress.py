"""统一模型外发检查；检查发生在计费预占和 HTTP 之前。"""

import json
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlsplit

from domain.egress import EgressPolicy
from domain.security import ErrorCode, SafeApplicationError, SafeError

# 只拦截高置信凭据形状；普通 token 变量和示例占位符不能当成凭据。
_CREDENTIAL = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})\b"
    r"|\b[a-z][a-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@"
    r"|(?i:\b(?:password|api[_-]?key|client[_-]?secret|private[_-]?key)\s*[:=]\s*[\"'][^\"'\s]{12,}[\"'])"
)


@dataclass(frozen=True)
class EgressContext:
    policy: EgressPolicy
    record: Callable[[dict[str, object]], None] | None = None


_CONTEXT: ContextVar[EgressContext | None] = ContextVar("model_egress", default=None)


@contextmanager
def egress_scope(policy: EgressPolicy, record: Callable[[dict[str, object]], None] | None = None) -> Iterator[None]:
    token = _CONTEXT.set(EgressContext(policy, record))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


@contextmanager
def review_egress(policy: EgressPolicy) -> Iterator[None]:
    if _CONTEXT.get() is not None:
        yield
    else:
        with egress_scope(policy):
            yield


def _deny(reason: str, digest: str | None = None) -> None:
    context = _CONTEXT.get()
    details: dict[str, object] = {"egress_reason": reason}
    if digest:
        details["input_sha256"] = digest
    if context and context.record:
        context.record(details)
    raise SafeApplicationError(SafeError(
        code=ErrorCode.MODEL_EGRESS_DENIED, safe_message="模型外发被仓库策略阻止，请检查外发设置和源代码",
        retryable=False, details=details,
    ))


def check_paths(paths: Sequence[str]) -> None:
    context = _CONTEXT.get()
    if context and any(context.policy.denies_path(path) for path in paths):
        _deny("blocked_path")


def check_texts(texts: Sequence[str]) -> None:
    context = _CONTEXT.get()
    if context and context.policy.block_secrets:
        for text in texts:
            if _CREDENTIAL.search(text):
                _deny("credential_detected", sha256(text.encode()).hexdigest())


def check_payload(host_url: str, payload: dict[str, object]) -> None:
    context = _CONTEXT.get()
    if context is None:
        return
    hostname = (urlsplit(host_url).hostname or "").lower().rstrip(".")
    if context.policy.allowed_hosts and hostname not in context.policy.allowed_hosts:
        _deny("provider_not_allowed")
    # 扫描字符串叶节点，避免 JSON 转义掩盖换行或引号中的凭据。
    stack: list[object] = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
        elif isinstance(value, str):
            check_texts((value,))
            # Prompt 用户内容本身是 JSON，额外解析这一层以扫描原始补丁。
            if value.startswith("{"):
                try:
                    nested = json.loads(value)
                except (ValueError, RecursionError):
                    continue
                if isinstance(nested, dict):
                    stack.extend(v for v in nested.values() if not isinstance(v, str) or v != value)
