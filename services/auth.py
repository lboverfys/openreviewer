"""Password verification, signed sessions and login throttling."""

from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
from threading import Lock

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class AuthConfigurationError(RuntimeError):
    """Required authentication settings are absent or invalid."""


class InvalidSessionError(ValueError):
    """The supplied browser session is missing, invalid or expired."""


class LoginRateLimitError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("too many failed login attempts")
        self.retry_after_seconds = retry_after_seconds


def _read_setting_or_file(
    values: Mapping[str, str],
    direct_name: str,
    file_name: str,
) -> str:
    direct_value = values.get(direct_name, "")
    path_value = values.get(file_name, "").strip()
    if direct_value and path_value:
        raise AuthConfigurationError(
            f"configure only one of {direct_name} and {file_name}"
        )
    if path_value:
        try:
            return Path(path_value).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AuthConfigurationError(f"{file_name} could not be read") from exc
    return direct_value.strip()


def _environment_boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw_value = values.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise AuthConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class AuthSettings:
    username: str
    password_hash: str
    session_secret: bytes
    cookie_secure: bool = True
    session_ttl: timedelta = timedelta(hours=8)

    @property
    def cookie_name(self) -> str:
        return (
            "__Host-openreviewer_session"
            if self.cookie_secure
            else "openreviewer_session"
        )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "AuthSettings":
        values = os.environ if environment is None else environment
        username = values.get("OPENREVIEWER_ADMIN_USERNAME", "").strip()
        if not 1 <= len(username) <= 100:
            raise AuthConfigurationError(
                "OPENREVIEWER_ADMIN_USERNAME must contain 1 to 100 characters"
            )

        password_hash = _read_setting_or_file(
            values,
            "OPENREVIEWER_ADMIN_PASSWORD_HASH",
            "OPENREVIEWER_ADMIN_PASSWORD_HASH_FILE",
        )
        if not password_hash.startswith("$argon2id$"):
            raise AuthConfigurationError("the administrator password must use Argon2id")

        session_secret = _read_setting_or_file(
            values,
            "OPENREVIEWER_SESSION_SECRET",
            "OPENREVIEWER_SESSION_SECRET_FILE",
        ).encode("utf-8")
        if len(session_secret) < 32:
            raise AuthConfigurationError(
                "the session signing secret must contain at least 32 bytes"
            )

        ttl_raw = values.get("OPENREVIEWER_SESSION_TTL_SECONDS", "28800")
        try:
            ttl_seconds = int(ttl_raw)
        except ValueError as exc:
            raise AuthConfigurationError(
                "OPENREVIEWER_SESSION_TTL_SECONDS must be an integer"
            ) from exc
        if not 300 <= ttl_seconds <= 86400:
            raise AuthConfigurationError(
                "OPENREVIEWER_SESSION_TTL_SECONDS must be between 300 and 86400"
            )

        return cls(
            username=username,
            password_hash=password_hash,
            session_secret=session_secret,
            cookie_secure=_environment_boolean(
                values,
                "OPENREVIEWER_COOKIE_SECURE",
                True,
            ),
            session_ttl=timedelta(seconds=ttl_seconds),
        )


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    username: str
    issued_at: datetime
    expires_at: datetime


class AuthService:
    def __init__(
        self,
        settings: AuthSettings,
        *,
        password_hasher: PasswordHasher | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self._password_hasher = password_hasher or PasswordHasher()
        self._clock = clock or (lambda: datetime.now(UTC))

    def verify_credentials(self, username: str, password: str) -> bool:
        """Always verify the hash so an unknown username is not a fast path."""

        try:
            password_matches = self._password_hasher.verify(
                self.settings.password_hash,
                password,
            )
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            password_matches = False
        username_matches = hmac.compare_digest(username, self.settings.username)
        return bool(password_matches and username_matches)

    def create_session(self) -> tuple[str, SessionPrincipal]:
        issued_at = self._clock().astimezone(UTC)
        expires_at = issued_at + self.settings.session_ttl
        payload = {
            "v": 1,
            "sub": self.settings.username,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "nonce": secrets.token_urlsafe(16),
        }
        encoded_payload = self._encode(
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        signed_data = f"v1.{encoded_payload}".encode("ascii")
        signature = hmac.new(
            self.settings.session_secret,
            signed_data,
            hashlib.sha256,
        ).digest()
        token = f"v1.{encoded_payload}.{self._encode(signature)}"
        return token, SessionPrincipal(
            username=self.settings.username,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def verify_session(self, token: str | None) -> SessionPrincipal:
        if not token:
            raise InvalidSessionError("session cookie is missing")
        try:
            version, encoded_payload, encoded_signature = token.split(".")
            if version != "v1":
                raise InvalidSessionError("unsupported session version")
            signed_data = f"{version}.{encoded_payload}".encode("ascii")
            expected_signature = hmac.new(
                self.settings.session_secret,
                signed_data,
                hashlib.sha256,
            ).digest()
            supplied_signature = self._decode(encoded_signature)
            if not hmac.compare_digest(expected_signature, supplied_signature):
                raise InvalidSessionError("session signature is invalid")

            payload = json.loads(self._decode(encoded_payload))
            if (
                not isinstance(payload, dict)
                or payload.get("v") != 1
                or payload.get("sub") != self.settings.username
                or not isinstance(payload.get("iat"), int)
                or not isinstance(payload.get("exp"), int)
            ):
                raise InvalidSessionError("session payload is invalid")
            issued_at = datetime.fromtimestamp(payload["iat"], UTC)
            expires_at = datetime.fromtimestamp(payload["exp"], UTC)
            now = self._clock().astimezone(UTC)
            if expires_at <= now or issued_at > now + timedelta(minutes=1):
                raise InvalidSessionError("session is expired or not active")
            return SessionPrincipal(
                username=self.settings.username,
                issued_at=issued_at,
                expires_at=expires_at,
            )
        except InvalidSessionError:
            raise
        except (ValueError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise InvalidSessionError("session token is malformed") from exc

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
        if not hmac.compare_digest(AuthService._encode(decoded), value):
            raise ValueError("base64 value is not canonically encoded")
        return decoded


class LoginAttemptLimiter:
    """Process-local sliding window suitable for the single API replica in M2."""

    def __init__(
        self,
        *,
        maximum_failures: int = 5,
        window: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if maximum_failures <= 0 or window.total_seconds() <= 0:
            raise ValueError("login limiter values must be positive")
        self._maximum_failures = maximum_failures
        self._window = window
        self._clock = clock or (lambda: datetime.now(UTC))
        self._failures: dict[str, deque[datetime]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> None:
        with self._lock:
            failures = self._active_failures(key)
            if len(failures) < self._maximum_failures:
                return
            retry_at = failures[0] + self._window
            remaining = max(1, int((retry_at - self._clock()).total_seconds()) + 1)
            raise LoginRateLimitError(remaining)

    def record_failure(self, key: str) -> None:
        with self._lock:
            failures = self._active_failures(key)
            failures.append(self._clock())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def _active_failures(self, key: str) -> deque[datetime]:
        failures = self._failures[key]
        cutoff = self._clock() - self._window
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            self._failures.pop(key, None)
            failures = self._failures[key]
        return failures
