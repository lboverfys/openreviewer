"""ASGI 请求关联覆盖流结束与异常，日志不包含实际 URL 或请求正文。"""

import logging
from time import monotonic

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domain.logging import log_context, log_event, normalize_request_id
from domain.security import SafeError


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = normalize_request_id(Headers(scope=scope).get("x-request-id"))
        scope.setdefault("state", {})["request_id"] = request_id
        started, response_status, response_started = monotonic(), 500, False
        error_code = None

        async def send_response(message: Message) -> None:
            nonlocal response_status, response_started
            if message["type"] == "http.response.start":
                response_status, response_started = message["status"], True
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        with log_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_response)
            except Exception as exc:
                error_code = SafeError.from_exception(exc).code.value
                if not response_started:
                    await PlainTextResponse("Internal Server Error", status_code=500)(scope, receive, send_response)
                raise
            finally:
                log_event("http_request_completed", method=scope["method"],
                    route=getattr(scope.get("route"), "path", "unmatched"),
                    response_status=response_status, error_code=error_code,
                    duration_ms=max(0, int((monotonic() - started) * 1000)),
                    level=logging.ERROR if error_code else logging.INFO)
