"""进程内低基数 Prometheus 指标与 Worker 内网导出端点。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
)


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(
    names: Sequence[str],
    values: Sequence[str],
    *,
    extra: tuple[str, str] | None = None,
) -> str:
    pairs = [
        f'{name}="{_escape_label(value)}"'
        for name, value in zip(names, values, strict=True)
    ]
    if extra is not None:
        name, value = extra
        pairs.append(f'{name}="{_escape_label(value)}"')
    return "{" + ",".join(pairs) + "}"


class _Histogram:
    def __init__(self, label_names: tuple[str, ...]) -> None:
        self.label_names = label_names
        self.counts: dict[tuple[str, ...], int] = defaultdict(int)
        self.sums: dict[tuple[str, ...], float] = defaultdict(float)
        self.buckets: dict[tuple[str, ...], list[int]] = {}

    def observe(self, labels: tuple[str, ...], value: float) -> None:
        if labels not in self.buckets:
            self.buckets[labels] = [0] * len(_DURATION_BUCKETS)
        self.counts[labels] += 1
        self.sums[labels] += value
        for index, boundary in enumerate(_DURATION_BUCKETS):
            if value <= boundary:
                self.buckets[labels][index] += 1

    def render(self, name: str, help_text: str) -> list[str]:
        lines = [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
        for label_values in sorted(self.counts):
            for boundary, count in zip(
                _DURATION_BUCKETS,
                self.buckets[label_values],
                strict=True,
            ):
                boundary_label = str(boundary).rstrip("0").rstrip(".")
                lines.append(
                    f"{name}_bucket"
                    f"{_labels(self.label_names, label_values, extra=('le', boundary_label))} "
                    f"{count}"
                )
            count = self.counts[label_values]
            lines.append(
                f"{name}_bucket"
                f"{_labels(self.label_names, label_values, extra=('le', '+Inf'))} "
                f"{count}"
            )
            labels = _labels(self.label_names, label_values)
            lines.append(f"{name}_sum{labels} {self.sums[label_values]:.6f}")
            lines.append(f"{name}_count{labels} {count}")
        return lines


class TelemetryRegistry:
    """只接受固定标签维度的计数和耗时，避免业务主键造成时序爆炸。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._http = _Histogram(("method", "route", "status_class"))
        self._external = _Histogram(("service", "outcome"))

    def observe_http(
        self,
        method: str,
        route: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        normalized_method = method if method in {"GET", "POST", "PUT", "PATCH", "DELETE"} else "OTHER"
        normalized_route = route if route.startswith("/") and len(route) <= 200 else "unmatched"
        status_class = f"{status_code // 100}xx" if 100 <= status_code <= 599 else "unknown"
        with self._lock:
            self._http.observe(
                (normalized_method, normalized_route, status_class),
                max(0.0, duration_seconds),
            )

    def observe_external(
        self,
        service: str,
        duration_seconds: float,
        *,
        status_code: int | None = None,
        outcome: str | None = None,
    ) -> None:
        allowed_services = {"github", "model_openai", "model_anthropic"}
        normalized_service = service if service in allowed_services else "other"
        if outcome is None:
            if status_code is not None and 200 <= status_code < 300:
                outcome = "success"
            elif status_code == 429:
                outcome = "rate_limited"
            elif status_code is not None and status_code >= 500:
                outcome = "server_error"
            else:
                outcome = "client_error"
        allowed_outcomes = {
            "success",
            "client_error",
            "server_error",
            "rate_limited",
            "timeout",
            "network_error",
            "invalid_response",
        }
        normalized_outcome = outcome if outcome in allowed_outcomes else "client_error"
        with self._lock:
            self._external.observe(
                (normalized_service, normalized_outcome),
                max(0.0, duration_seconds),
            )

    def render(self) -> str:
        with self._lock:
            lines = self._http.render(
                "openreviewer_http_server_request_duration_seconds",
                "Duration of API requests by route template and status class.",
            )
            lines.extend(
                self._external.render(
                    "openreviewer_external_http_request_duration_seconds",
                    "Duration of bounded external HTTP requests by service and outcome.",
                )
            )
        return "\n".join(lines) + "\n"


GLOBAL_TELEMETRY = TelemetryRegistry()


class TelemetryHttpServer:
    """为无 ASGI 入口的 Worker 提供仅内部网络使用的指标端点。"""

    def __init__(
        self,
        host: str,
        port: int,
        registry: TelemetryRegistry = GLOBAL_TELEMETRY,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("telemetry port must be between 0 and 65535")
        self._registry = registry

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/healthz":
                    payload = b'{"status":"ok","service":"openreviewer-worker"}\n'
                    content_type = "application/json"
                    status = 200
                elif self.path == "/metrics":
                    payload = outer._registry.render().encode("utf-8")
                    content_type = "text/plain; version=0.0.4"
                    status = 200
                else:
                    payload = b"not found\n"
                    content_type = "text/plain"
                    status = 404
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = Thread(
            target=self._server.serve_forever,
            name="openreviewer-worker-metrics",
            daemon=True,
        )
        self._started = False

    @property
    def port(self) -> int:
        """返回实际监听端口；传入 0 时由操作系统分配，便于隔离测试。"""

        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._started:
            raise RuntimeError("telemetry server is already running")
        self._thread.start()
        self._started = True

    def close(self) -> None:
        if self._started:
            self._server.shutdown()
        self._server.server_close()
        if self._started:
            self._thread.join(timeout=5)
            self._started = False
