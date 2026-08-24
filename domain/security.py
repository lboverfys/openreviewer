"""错误、日志和持久化详情共用的安全基础组件。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import logging
import re
import traceback
from typing import Any


REDACTED = "<redacted>"
TRUNCATED = "<truncated>"
_MAX_REDACTION_DEPTH = 12
_MAX_REDACTION_NODES = 256
_MAX_REDACTION_CHARACTERS = 32 * 1024
_MAX_REDACTED_TEXT_LENGTH = 4000
_MAX_REDACTED_KEY_LENGTH = 200


class ErrorCode(str, Enum):
    """向运维人员和 API 暴露的稳定机器可读错误码。"""

    WORKER_UNEXPECTED_ERROR = "worker_unexpected_error"
    TASK_LEASE_EXPIRED = "task_lease_expired"
    TASK_LEASE_LOST = "task_lease_lost"
    TASK_QUEUE_UNAVAILABLE = "task_queue_unavailable"
    WEBHOOK_INVALID_SIGNATURE = "webhook_invalid_signature"
    WEBHOOK_INVALID_PAYLOAD = "webhook_invalid_payload"
    WEBHOOK_PAYLOAD_TOO_LARGE = "webhook_payload_too_large"
    WEBHOOK_NOT_CONFIGURED = "webhook_not_configured"
    WEBHOOK_DELIVERY_CONFLICT = "webhook_delivery_conflict"
    WEBHOOK_PERSISTENCE_UNAVAILABLE = "webhook_persistence_unavailable"
    GITHUB_TIMEOUT = "github_timeout"
    GITHUB_RATE_LIMITED = "github_rate_limited"
    GITHUB_AUTHENTICATION_FAILED = "github_authentication_failed"
    GITHUB_PERMISSION_DENIED = "github_permission_denied"
    GITHUB_NOT_FOUND = "github_not_found"
    GITHUB_SERVER_ERROR = "github_server_error"
    GITHUB_REQUEST_REJECTED = "github_request_rejected"
    GITHUB_INVALID_RESPONSE = "github_invalid_response"
    GITHUB_RESPONSE_TOO_LARGE = "github_response_too_large"
    CI_WAIT_TIMEOUT = "ci_wait_timeout"


_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "client_secret",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "private_key",
    "proxy_authorization",
    "pwd",
    "secret",
    "set_cookie",
    "token",
    "access_token",
    "refresh_token",
}
_SENSITIVE_KEY_SUFFIXES = (
    "_api_key",
    "_credential",
    "_credentials",
    "_password",
    "_private_key",
    "_secret",
    "_token",
)
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_URL_CREDENTIAL_RE = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@",
    re.IGNORECASE,
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)\b(?P<name>authorization|proxy-authorization)\s*:\s*"
    r"(?:(?:bearer|basic|token)\s+)?[^\s,;]+"
)
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?P<name>password|passwd|pwd|secret|token|access[_-]?token|"
    r"refresh[_-]?token|api[_-]?key|private[_-]?key|client[_-]?secret)"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;\]}]+)"
)
_KNOWN_TOKEN_RES = (
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(
        r"\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
        r"[A-Za-z0-9_-]{10,}\b"
    ),
)


@dataclass(slots=True)
class _RedactionBudget:
    remaining_nodes: int = _MAX_REDACTION_NODES
    remaining_characters: int = _MAX_REDACTION_CHARACTERS


def _safe_string(value: object) -> str:
    try:
        return str(value)
    except Exception:
        return f"<{type(value).__name__}>"


def _is_sensitive_key(key: object) -> bool:
    normalized = re.sub(
        r"[^a-z0-9]+",
        "_",
        _safe_string(key).casefold(),
    ).strip("_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        _SENSITIVE_KEY_SUFFIXES
    )


def redact_text(value: str) -> str:
    """从自由文本中移除常见凭据，同时保留必要上下文。"""

    redacted = _PEM_PRIVATE_KEY_RE.sub(REDACTED, value)
    redacted = _URL_CREDENTIAL_RE.sub(
        lambda match: f"{match.group('scheme')}{REDACTED}:{REDACTED}@",
        redacted,
    )
    redacted = _AUTHORIZATION_RE.sub(
        lambda match: f"{match.group('name')}: {REDACTED}",
        redacted,
    )
    redacted = _ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('name')}{match.group('separator')}{REDACTED}"
        ),
        redacted,
    )
    for pattern in _KNOWN_TOKEN_RES:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def _bounded_redact_text(
    value: str,
    budget: _RedactionBudget,
    *,
    limit: int = _MAX_REDACTED_TEXT_LENGTH,
) -> str:
    if budget.remaining_characters <= 0:
        return TRUNCATED
    input_limit = min(len(value), limit * 2)
    sanitized = redact_text(value[:input_limit])
    available = min(limit, budget.remaining_characters)
    was_truncated = len(value) > input_limit or len(sanitized) > available
    if was_truncated and available > len(TRUNCATED):
        result = sanitized[: available - len(TRUNCATED)] + TRUNCATED
    elif was_truncated:
        result = TRUNCATED[:available]
    else:
        result = sanitized[:available]
    budget.remaining_characters -= len(result)
    return result


def redact_sensitive(
    value: Any,
    *,
    _depth: int = 0,
    _budget: _RedactionBudget | None = None,
) -> Any:
    """在统一大小和深度限制下递归脱敏类 JSON 数据。"""

    budget = _budget or _RedactionBudget()
    if budget.remaining_nodes <= 0:
        return TRUNCATED
    budget.remaining_nodes -= 1
    if _depth >= _MAX_REDACTION_DEPTH:
        return "<maximum-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_redact_text(value, budget)
    if isinstance(value, bytes):
        return REDACTED
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if budget.remaining_nodes <= 0:
                result[TRUNCATED] = TRUNCATED
                break
            safe_key = _bounded_redact_text(
                _safe_string(key),
                budget,
                limit=_MAX_REDACTED_KEY_LENGTH,
            )
            if _is_sensitive_key(key):
                budget.remaining_nodes -= 1
                result[safe_key] = REDACTED
            else:
                result[safe_key] = redact_sensitive(
                    item,
                    _depth=_depth + 1,
                    _budget=budget,
                )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        result_items: list[object] = []
        for item in value:
            if budget.remaining_nodes <= 0:
                result_items.append(TRUNCATED)
                break
            result_items.append(
                redact_sensitive(
                    item,
                    _depth=_depth + 1,
                    _budget=budget,
                )
            )
        return tuple(result_items) if isinstance(value, tuple) else result_items
    return _bounded_redact_text(_safe_string(value), budget)


@dataclass(frozen=True, slots=True)
class SafeError:
    """可以安全持久化和展示的有界错误表示。"""

    code: ErrorCode
    safe_message: str
    retryable: bool
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        message = _bounded_redact_text(
            _safe_string(self.safe_message).strip(),
            _RedactionBudget(),
            limit=1000,
        )
        if not message:
            message = "任务处理失败，未提供可公开的错误说明"
        details = redact_sensitive(self.details)
        if not isinstance(details, dict):
            details = {}
        object.__setattr__(self, "safe_message", message)
        object.__setattr__(self, "details", details)

    @classmethod
    def from_exception(cls, error: BaseException) -> "SafeError":
        """分类已知安全异常，并隔离未知异常文本。"""

        if isinstance(error, SafeApplicationError):
            return error.error
        if isinstance(error, TimeoutError):
            return cls(
                code=ErrorCode.GITHUB_TIMEOUT,
                safe_message="外部服务请求超时",
                retryable=True,
                details={"exception_type": type(error).__name__},
            )
        return cls(
            code=ErrorCode.WORKER_UNEXPECTED_ERROR,
            safe_message="Worker 处理任务时发生未预期错误",
            retryable=True,
            details={
                "exception_type": type(error).__name__,
            },
        )

    def public_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code.value,
            "message": self.safe_message,
            "retryable": self.retryable,
        }
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class SafeApplicationError(RuntimeError):
    """携带已分类、已脱敏错误的异常。"""

    def __init__(self, error: SafeError) -> None:
        super().__init__(error.safe_message)
        self.error = error


class RedactingLogFilter(logging.Filter):
    """对消息、参数和调用栈应用同一套脱敏策略。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_sensitive(record.msg)
            if record.args:
                record.args = redact_sensitive(record.args)
            if record.exc_info:
                formatted = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = redact_sensitive(formatted)
                record.exc_info = None
        except Exception:
            record.msg = "日志内容脱敏失败，原始内容已丢弃"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def install_redacting_log_filters() -> None:
    """为每个已配置的日志处理器安装一个脱敏过滤器。"""

    root = logging.getLogger()
    handlers = list(root.handlers)
    for candidate in logging.root.manager.loggerDict.values():
        if isinstance(candidate, logging.Logger):
            handlers.extend(candidate.handlers)
    for handler in dict.fromkeys(handlers):
        if not any(isinstance(item, RedactingLogFilter) for item in handler.filters):
            handler.addFilter(RedactingLogFilter())
