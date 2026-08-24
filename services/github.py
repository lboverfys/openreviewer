"""带安全错误分类和审计数据的有界 GitHub REST 客户端。"""

from dataclasses import dataclass
import json
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx

from domain.security import ErrorCode, SafeApplicationError, SafeError


@dataclass(frozen=True, slots=True)
class GitHubClientSettings:
    api_base_url: str = "https://api.github.com"
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 20.0
    write_timeout_seconds: float = 10.0
    pool_timeout_seconds: float = 5.0
    max_response_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        parsed = urlsplit(self.api_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("GitHub API base URL must be an absolute HTTPS URL")
        timeouts = (
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
            self.write_timeout_seconds,
            self.pool_timeout_seconds,
        )
        if any(value <= 0 for value in timeouts):
            raise ValueError("GitHub API timeouts must be positive")
        if not 1024 <= self.max_response_bytes <= 10 * 1024 * 1024:
            raise ValueError(
                "GitHub API response limit must be between 1 KiB and 10 MiB"
            )

    @property
    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout_seconds,
            read=self.read_timeout_seconds,
            write=self.write_timeout_seconds,
            pool=self.pool_timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class GitHubCallAudit:
    request_method: str
    request_path: str
    response_status: int | None
    github_request_id: str | None
    duration_ms: int
    rate_limit_remaining: int | None


@dataclass(frozen=True, slots=True)
class GitHubApiResult:
    payload: object
    audit: GitHubCallAudit


class GitHubApiClient:
    """执行一次有界请求；持久化任务重试由客户端外部负责。"""

    def __init__(
        self,
        settings: GitHubClientSettings | None = None,
        *,
        client: httpx.Client | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings or GitHubClientSettings()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.settings.api_base_url,
            timeout=self.settings.timeout,
            follow_redirects=False,
        )
        self._monotonic = monotonic or time.monotonic

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def request_json(
        self,
        method: str,
        path: str,
        *,
        installation_token: str,
        params: dict[str, str | int] | None = None,
    ) -> GitHubApiResult:
        normalized_method = method.strip().upper()
        parsed_path = urlsplit(path)
        if (
            normalized_method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}
            or not path.startswith("/")
            or path.startswith("//")
            or parsed_path.scheme
            or parsed_path.netloc
            or parsed_path.query
            or parsed_path.fragment
            or len(path) > 1000
        ):
            raise ValueError("GitHub request must use a supported method and relative path")
        if not installation_token:
            raise ValueError("GitHub installation token must not be empty")

        started = self._monotonic()
        try:
            with self._client.stream(
                normalized_method,
                path,
                params=params,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {installation_token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=self.settings.timeout,
            ) as response:
                if not 200 <= response.status_code < 300:
                    audit = self._audit(normalized_method, path, response, started)
                    raise SafeApplicationError(
                        self._classify_response(response, audit)
                    )
                response_status = response.status_code
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > self.settings.max_response_bytes:
                        audit = self._audit(
                            normalized_method, path, response, started
                        )
                        raise SafeApplicationError(
                            SafeError(
                                code=ErrorCode.GITHUB_INVALID_RESPONSE,
                                safe_message="GitHub API 响应超过允许大小",
                                retryable=False,
                                details=self._audit_details(audit),
                            )
                        )
                    content.extend(chunk)
                audit = self._audit(normalized_method, path, response, started)
        except SafeApplicationError:
            raise
        except httpx.TimeoutException as exc:
            raise SafeApplicationError(
                SafeError(
                    code=ErrorCode.GITHUB_TIMEOUT,
                    safe_message="GitHub API 请求超时",
                    retryable=True,
                    details={
                        "method": normalized_method,
                        "path": path,
                    },
                )
            ) from exc
        except httpx.RequestError as exc:
            raise SafeApplicationError(
                SafeError(
                    code=ErrorCode.GITHUB_SERVER_ERROR,
                    safe_message="GitHub API 暂时无法访问",
                    retryable=True,
                    details={
                        "method": normalized_method,
                        "path": path,
                        "exception_type": type(exc).__name__,
                    },
                )
            ) from exc

        if response_status == 204 or not content:
            payload: object = None
        else:
            try:
                payload = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SafeApplicationError(
                    SafeError(
                        code=ErrorCode.GITHUB_INVALID_RESPONSE,
                        safe_message="GitHub API 返回了无法解析的 JSON",
                        retryable=False,
                        details=self._audit_details(audit),
                    )
                ) from exc
        return GitHubApiResult(payload=payload, audit=audit)

    def _audit(
        self,
        method: str,
        path: str,
        response: httpx.Response,
        started: float,
    ) -> GitHubCallAudit:
        remaining_value = response.headers.get("x-ratelimit-remaining")
        try:
            remaining = int(remaining_value) if remaining_value is not None else None
        except ValueError:
            remaining = None
        return GitHubCallAudit(
            request_method=method,
            request_path=path,
            response_status=response.status_code,
            github_request_id=response.headers.get("x-github-request-id"),
            duration_ms=max(0, int((self._monotonic() - started) * 1000)),
            rate_limit_remaining=remaining,
        )

    def _classify_response(
        self,
        response: httpx.Response,
        audit: GitHubCallAudit,
    ) -> SafeError:
        status_code = response.status_code
        if status_code == 401:
            code = ErrorCode.GITHUB_AUTHENTICATION_FAILED
            message = "GitHub installation token 无效或已过期"
            retryable = False
        elif status_code == 429 or (
            status_code == 403
            and (
                audit.rate_limit_remaining == 0
                or response.headers.get("retry-after") is not None
            )
        ):
            code = ErrorCode.GITHUB_RATE_LIMITED
            message = "GitHub API 已触发限流"
            retryable = True
        elif status_code == 403:
            code = ErrorCode.GITHUB_PERMISSION_DENIED
            message = "GitHub App 权限不足"
            retryable = False
        elif status_code == 404:
            code = ErrorCode.GITHUB_NOT_FOUND
            message = "GitHub 资源不存在或当前安装不可见"
            retryable = False
        elif status_code >= 500:
            code = ErrorCode.GITHUB_SERVER_ERROR
            message = "GitHub API 服务端暂时不可用"
            retryable = True
        else:
            code = ErrorCode.GITHUB_REQUEST_REJECTED
            message = "GitHub API 拒绝了当前请求"
            retryable = False
        details = self._audit_details(audit)
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            details["retry_after_seconds"] = int(retry_after)
        return SafeError(
            code=code,
            safe_message=message,
            retryable=retryable,
            details=details,
        )

    @staticmethod
    def _audit_details(audit: GitHubCallAudit) -> dict[str, object]:
        return {
            "method": audit.request_method,
            "path": audit.request_path,
            "status_code": audit.response_status,
            "github_request_id": audit.github_request_id,
            "duration_ms": audit.duration_ms,
            "rate_limit_remaining": audit.rate_limit_remaining,
        }
