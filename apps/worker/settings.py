"""Worker 进程与健康检查共享的环境配置边界。"""

import os
import socket
from dataclasses import dataclass
from datetime import timedelta


def _positive_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _bounded_integer(
    value: str,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    worker_id: str
    poll_interval: timedelta
    lease_duration: timedelta
    ci_poll_interval: timedelta = timedelta(seconds=30)
    ci_wait_timeout: timedelta = timedelta(hours=1)
    github_context_lease_duration: timedelta = timedelta(minutes=10)
    model_review_lease_duration: timedelta = timedelta(minutes=10)
    telemetry_host: str = "127.0.0.1"
    telemetry_port: int = 18091

    def __post_init__(self) -> None:
        if not self.worker_id or len(self.worker_id) > 200:
            raise ValueError("worker ID must contain 1 to 200 characters")
        if self.poll_interval.total_seconds() <= 0:
            raise ValueError("worker poll interval must be positive")
        if self.lease_duration <= self.poll_interval * 2:
            raise ValueError("worker lease duration must exceed twice the poll interval")
        if self.ci_poll_interval.total_seconds() <= 0:
            raise ValueError("CI poll interval must be positive")
        if self.ci_wait_timeout <= self.ci_poll_interval:
            raise ValueError("CI wait timeout must exceed the poll interval")
        if self.github_context_lease_duration <= self.lease_duration:
            raise ValueError("GitHub context lease must exceed the normal lease")
        if self.model_review_lease_duration <= self.lease_duration:
            raise ValueError("model review lease must exceed the normal lease")
        if (
            not self.telemetry_host
            or len(self.telemetry_host) > 255
            or any(character.isspace() for character in self.telemetry_host)
        ):
            raise ValueError("telemetry host must contain 1 to 255 non-space characters")
        if not 1 <= self.telemetry_port <= 65535:
            raise ValueError("telemetry port must be between 1 and 65535")

    @classmethod
    def from_environment(cls) -> "WorkerSettings":
        """读取并校验 Worker 身份、租约、CI 等待和指标监听配置。"""

        worker_id = os.environ.get(
            "OPENREVIEWER_WORKER_ID",
            socket.gethostname(),
        ).strip()
        if not worker_id or len(worker_id) > 200:
            raise ValueError("OPENREVIEWER_WORKER_ID must contain 1 to 200 characters")
        poll_seconds = _positive_float(
            os.environ.get("OPENREVIEWER_WORKER_POLL_SECONDS", "2"),
            "OPENREVIEWER_WORKER_POLL_SECONDS",
        )
        lease_seconds = _positive_float(
            os.environ.get("OPENREVIEWER_WORKER_LEASE_SECONDS", "30"),
            "OPENREVIEWER_WORKER_LEASE_SECONDS",
        )
        if lease_seconds <= poll_seconds * 2:
            raise ValueError("worker lease duration must exceed twice the poll interval")
        ci_poll_seconds = _positive_float(
            os.environ.get("OPENREVIEWER_CI_POLL_SECONDS", "30"),
            "OPENREVIEWER_CI_POLL_SECONDS",
        )
        ci_wait_seconds = _positive_float(
            os.environ.get("OPENREVIEWER_CI_WAIT_TIMEOUT_SECONDS", "3600"),
            "OPENREVIEWER_CI_WAIT_TIMEOUT_SECONDS",
        )
        if ci_wait_seconds <= ci_poll_seconds:
            raise ValueError("CI wait timeout must exceed the poll interval")
        context_lease_seconds = _positive_float(
            os.environ.get("OPENREVIEWER_GITHUB_CONTEXT_LEASE_SECONDS", "600"),
            "OPENREVIEWER_GITHUB_CONTEXT_LEASE_SECONDS",
        )
        if context_lease_seconds <= lease_seconds:
            raise ValueError("GitHub context lease must exceed the normal lease")
        model_lease_seconds = _positive_float(
            os.environ.get("OPENREVIEWER_MODEL_REVIEW_LEASE_SECONDS", "600"),
            "OPENREVIEWER_MODEL_REVIEW_LEASE_SECONDS",
        )
        if model_lease_seconds <= lease_seconds:
            raise ValueError("model review lease must exceed the normal lease")
        telemetry_host = os.environ.get(
            "OPENREVIEWER_WORKER_METRICS_HOST",
            "127.0.0.1",
        ).strip()
        telemetry_port = _bounded_integer(
            os.environ.get("OPENREVIEWER_WORKER_METRICS_PORT", "18091"),
            "OPENREVIEWER_WORKER_METRICS_PORT",
            minimum=1,
            maximum=65535,
        )
        return cls(
            worker_id=worker_id,
            poll_interval=timedelta(seconds=poll_seconds),
            lease_duration=timedelta(seconds=lease_seconds),
            ci_poll_interval=timedelta(seconds=ci_poll_seconds),
            ci_wait_timeout=timedelta(seconds=ci_wait_seconds),
            github_context_lease_duration=timedelta(seconds=context_lease_seconds),
            model_review_lease_duration=timedelta(seconds=model_lease_seconds),
            telemetry_host=telemetry_host,
            telemetry_port=telemetry_port,
        )
