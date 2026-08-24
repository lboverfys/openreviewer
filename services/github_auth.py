"""GitHub App JWT 与短期 installation token 身份服务。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path

import jwt
from jwt.exceptions import InvalidKeyError

from domain.security import ErrorCode, SafeApplicationError, SafeError
from services.github import GitHubApiClient


@dataclass(frozen=True, slots=True)
class GitHubAppSettings:
    """GitHub App 身份所需的非敏感参数和密钥文件位置。"""

    app_id: int
    private_key_file: Path
    jwt_lifetime: timedelta = timedelta(minutes=9)
    jwt_clock_skew: timedelta = timedelta(seconds=60)
    token_refresh_margin: timedelta = timedelta(minutes=2)
    max_private_key_bytes: int = 64 * 1024
    max_cached_installations: int = 128

    def __post_init__(self) -> None:
        if self.app_id <= 0:
            raise ValueError("GitHub App ID must be positive")
        if not self.private_key_file.is_absolute():
            raise ValueError("GitHub App private key path must be absolute")
        if not timedelta(minutes=1) <= self.jwt_lifetime <= timedelta(minutes=10):
            raise ValueError("GitHub App JWT lifetime must be between 1 and 10 minutes")
        if not timedelta(0) <= self.jwt_clock_skew <= timedelta(minutes=2):
            raise ValueError("GitHub App JWT clock skew is outside the allowed range")
        if not timedelta(0) < self.token_refresh_margin < timedelta(hours=1):
            raise ValueError("GitHub token refresh margin is outside the allowed range")
        if not 1024 <= self.max_private_key_bytes <= 1024 * 1024:
            raise ValueError("GitHub App private key size limit is invalid")
        if not 1 <= self.max_cached_installations <= 1000:
            raise ValueError("GitHub token cache size must be between 1 and 1000")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "GitHubAppSettings":
        values = environment if environment is not None else os.environ
        raw_app_id = values.get("OPENREVIEWER_GITHUB_APP_ID", "").strip()
        raw_key_path = values.get(
            "OPENREVIEWER_GITHUB_PRIVATE_KEY_FILE", ""
        ).strip()
        try:
            app_id = int(raw_app_id)
        except ValueError as exc:
            raise ValueError("OPENREVIEWER_GITHUB_APP_ID must be a positive integer") from exc
        if not raw_key_path:
            raise ValueError("OPENREVIEWER_GITHUB_PRIVATE_KEY_FILE must be configured")
        return cls(app_id=app_id, private_key_file=Path(raw_key_path))


@dataclass(frozen=True, slots=True)
class InstallationToken:
    """只保存在当前 Worker 内存中的短期 installation token。"""

    value: str
    expires_at: datetime


class GitHubAppTokenProvider:
    """按 installation 缓存短期 Token，不写入日志、数据库或磁盘。"""

    def __init__(
        self,
        api: GitHubApiClient,
        settings: GitHubAppSettings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._api = api
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._private_key = self._read_private_key()
        self._cache: dict[int, InstallationToken] = {}
        self._validate_private_key()

    @property
    def app_id(self) -> int:
        return self._settings.app_id

    def get_token(self, installation_id: int) -> str:
        if installation_id <= 0:
            raise ValueError("GitHub installation ID must be positive")
        now = self._now()
        cached = self._cache.get(installation_id)
        if (
            cached is not None
            and cached.expires_at - self._settings.token_refresh_margin > now
        ):
            return cached.value

        app_jwt = self._build_app_jwt(now)
        result = self._api.request_json(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            bearer_token=app_jwt,
            max_response_bytes=64 * 1024,
        )
        token = self._parse_token_response(result.payload, now)
        self._cache[installation_id] = token
        self._trim_cache()
        return token.value

    def _read_private_key(self) -> bytes:
        try:
            size = self._settings.private_key_file.stat().st_size
            if not 1 <= size <= self._settings.max_private_key_bytes:
                raise ValueError("GitHub App private key file size is invalid")
            with self._settings.private_key_file.open("rb") as private_key_file:
                content = private_key_file.read(self._settings.max_private_key_bytes + 1)
            if not 1 <= len(content) <= self._settings.max_private_key_bytes:
                raise ValueError("GitHub App private key file size is invalid")
            return content
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError("GitHub App private key file could not be read") from exc

    def _validate_private_key(self) -> None:
        try:
            self._build_app_jwt(self._now())
        except (InvalidKeyError, TypeError, ValueError) as exc:
            raise ValueError("GitHub App private key is not a valid RSA key") from exc

    def _build_app_jwt(self, now: datetime) -> str:
        issued_at = now - self._settings.jwt_clock_skew
        expires_at = now + self._settings.jwt_lifetime
        encoded = jwt.encode(
            {
                "iat": int(issued_at.timestamp()),
                "exp": int(expires_at.timestamp()),
                "iss": str(self._settings.app_id),
            },
            self._private_key,
            algorithm="RS256",
        )
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("GitHub App JWT signing returned an invalid value")
        return encoded

    def _parse_token_response(
        self,
        payload: object,
        now: datetime,
    ) -> InstallationToken:
        if not isinstance(payload, dict):
            raise self._invalid_response()
        raw_token = payload.get("token")
        raw_expires_at = payload.get("expires_at")
        if (
            not isinstance(raw_token, str)
            or not raw_token
            or raw_token != raw_token.strip()
            or any(character.isspace() for character in raw_token)
            or not isinstance(raw_expires_at, str)
        ):
            raise self._invalid_response()
        try:
            parsed_expires_at = datetime.fromisoformat(
                raw_expires_at.replace("Z", "+00:00")
            )
            if parsed_expires_at.tzinfo is None:
                raise ValueError("GitHub token expiry must include a timezone")
            expires_at = parsed_expires_at.astimezone(UTC)
        except ValueError as exc:
            raise self._invalid_response() from exc
        if expires_at <= now + self._settings.token_refresh_margin:
            raise self._invalid_response()
        return InstallationToken(value=raw_token, expires_at=expires_at)

    def _trim_cache(self) -> None:
        if len(self._cache) <= self._settings.max_cached_installations:
            return
        oldest_installation = min(
            self._cache,
            key=lambda installation_id: self._cache[installation_id].expires_at,
        )
        del self._cache[oldest_installation]

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _invalid_response() -> SafeApplicationError:
        return SafeApplicationError(
            SafeError(
                code=ErrorCode.GITHUB_INVALID_RESPONSE,
                safe_message="GitHub installation token 响应格式无效",
                retryable=False,
            )
        )
