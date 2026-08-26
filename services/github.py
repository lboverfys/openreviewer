"""带安全错误分类、大小限制和审计数据的 GitHub REST 客户端。"""

from dataclasses import dataclass
import json
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx

from domain.security import ErrorCode, SafeApplicationError, SafeError


_ALLOWED_ACCEPT_HEADERS = {
    "application/vnd.github+json",
    "application/vnd.github.v3.diff",
}
_MAX_JSON_REQUEST_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class GitHubClientSettings:
    api_base_url: str = "https://api.github.com"
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 20.0
    write_timeout_seconds: float = 10.0
    pool_timeout_seconds: float = 5.0
    max_response_bytes: int = 10 * 1024 * 1024

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


@dataclass(frozen=True, slots=True)
class GitHubBytesResult:
    payload: bytes
    audit: GitHubCallAudit


class GitHubResponseTooLargeError(SafeApplicationError):
    """GitHub 响应超过调用方允许大小，调用方可以选择降级覆盖范围。"""


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
        bearer_token: str,
        params: dict[str, str | int] | None = None,
        json_body: object | None = None,
        max_response_bytes: int | None = None,
    ) -> GitHubApiResult:
        result = self.request_bytes(
            method,
            path,
            bearer_token=bearer_token,
            params=params,
            json_body=json_body,
            accept="application/vnd.github+json",
            max_response_bytes=max_response_bytes,
        )
        if result.audit.response_status == 204 or not result.payload:
            payload: object = None
        else:
            try:
                payload = json.loads(result.payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SafeApplicationError(
                    SafeError(
                        code=ErrorCode.GITHUB_INVALID_RESPONSE,
                        safe_message="GitHub API 返回了无法解析的 JSON",
                        retryable=False,
                        details=self._audit_details(result.audit),
                    )
                ) from exc
        return GitHubApiResult(payload=payload, audit=result.audit)

    def request_text(
        self,
        method: str,
        path: str,
        *,
        bearer_token: str,
        accept: str,
        params: dict[str, str | int] | None = None,
        max_response_bytes: int | None = None,
    ) -> tuple[str, GitHubCallAudit]:
        result = self.request_bytes(
            method,
            path,
            bearer_token=bearer_token,
            params=params,
            accept=accept,
            max_response_bytes=max_response_bytes,
        )
        try:
            return result.payload.decode("utf-8"), result.audit
        except UnicodeDecodeError as exc:
            raise SafeApplicationError(
                SafeError(
                    code=ErrorCode.GITHUB_INVALID_RESPONSE,
                    safe_message="GitHub API 返回了无法解码的文本",
                    retryable=False,
                    details=self._audit_details(result.audit),
                )
            ) from exc

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        bearer_token: str,
        params: dict[str, str | int] | None = None,
        json_body: object | None = None,
        accept: str = "application/vnd.github+json",
        max_response_bytes: int | None = None,
    ) -> GitHubBytesResult:
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
        if (
            not bearer_token
            or bearer_token != bearer_token.strip()
            or any(character.isspace() for character in bearer_token)
        ):
            raise ValueError("GitHub bearer token must not be empty or contain whitespace")
        if accept not in _ALLOWED_ACCEPT_HEADERS:
            raise ValueError("unsupported GitHub Accept header")
        request_content: bytes | None = None
        if json_body is not None:
            if normalized_method not in {"POST", "PATCH", "PUT"}:
                raise ValueError("GitHub JSON request bodies require a write-capable method")
            try:
                request_content = json.dumps(
                    json_body,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeError) as exc:
                raise ValueError("GitHub JSON request body must be serializable") from exc
            if len(request_content) > _MAX_JSON_REQUEST_BYTES:
                raise ValueError("GitHub JSON request body exceeds the 1 MiB limit")
        response_limit = (
            self.settings.max_response_bytes
            if max_response_bytes is None
            else max_response_bytes
        )
        if not 1 <= response_limit <= self.settings.max_response_bytes:
            raise ValueError("GitHub response limit exceeds the configured maximum")

        started = self._monotonic()
        try:
            headers = {
                "Accept": accept,
                "Authorization": f"Bearer {bearer_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if request_content is not None:
                headers["Content-Type"] = "application/json"
            with self._client.stream(
                normalized_method,
                path,
                params=params,
                headers=headers,
                content=request_content,
                timeout=self.settings.timeout,
            ) as response:
                if not 200 <= response.status_code < 300:
                    audit = self._audit(normalized_method, path, response, started)
                    raise SafeApplicationError(self._classify_response(response, audit))
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > response_limit:
                        audit = self._audit(normalized_method, path, response, started)
                        raise GitHubResponseTooLargeError(
                            SafeError(
                                code=ErrorCode.GITHUB_RESPONSE_TOO_LARGE,
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
                    details={"method": normalized_method, "path": path},
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
        return GitHubBytesResult(payload=bytes(content), audit=audit)

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
            message = "GitHub 身份令牌无效或已过期"
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
