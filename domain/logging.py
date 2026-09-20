"""只输出排障所需标量字段，正文和异常文本不进入应用日志。"""

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from json import dumps
from uuid import uuid4

from domain.security import install_redacting_log_filters, redact_sensitive

_CONTEXT: ContextVar[dict[str, object] | None] = ContextVar("log_context", default=None)
_FIELDS = frozenset({"request_id", "review_run_id", "review_task_id", "agent", "batch_number",
    "split_depth", "usage_request_id", "attempt_kind", "attempt_count", "model_attempt_count",
    "status", "response_status", "duration_ms", "error_code", "method", "route", "action", "purpose"})
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def normalize_request_id(value: str | None) -> str:
    return value if value and _REQUEST_ID.fullmatch(value) else uuid4().hex


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    token = _CONTEXT.set({**(_CONTEXT.get() or {}), **fields})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def log_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    logging.getLogger("openreviewer").log(level, event, extra={"event": event, **(_CONTEXT.get() or {}), **fields})


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        values = {**(_CONTEXT.get() or {}), **record.__dict__}
        payload = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": values.get("event", "application_log"),
            **{key: value for key, value in values.items() if key in _FIELDS
               and (value is None or isinstance(value, (str, int, float, bool)))},
        }
        # 不序列化任意消息、参数、extra、嵌套对象、exc_text 或异常文本。
        # 老日志保留代码位置，明确事件通过 log_event 的固定字段表达。
        payload["source"] = f"{record.module}.{record.funcName}:{record.lineno}"
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        return dumps(redact_sensitive(payload), ensure_ascii=False, separators=(",", ":"))


def configure_json_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level)
    root = logging.getLogger()
    root.setLevel(level)
    handlers = list(root.handlers)
    for logger in logging.root.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            handlers.extend(logger.handlers)
    for handler in dict.fromkeys(handlers):
        handler.setFormatter(JsonLogFormatter())
    install_redacting_log_filters()
