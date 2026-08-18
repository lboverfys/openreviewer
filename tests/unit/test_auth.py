from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.auth import (
    AuthConfigurationError,
    AuthService,
    AuthSettings,
    InvalidSessionError,
    LoginAttemptLimiter,
    LoginRateLimitError,
)
from tests.support import (
    TEST_HASHER,
    TEST_PASSWORD,
    TEST_PASSWORD_HASH,
    TEST_USERNAME,
)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def settings() -> AuthSettings:
    return AuthSettings(
        username=TEST_USERNAME,
        password_hash=TEST_PASSWORD_HASH,
        session_secret=b"test-session-secret-is-at-least-32-bytes-long",
        cookie_secure=False,
        session_ttl=timedelta(minutes=30),
    )


def test_credentials_and_signed_session_round_trip() -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    clock = MutableClock(now)
    service = AuthService(settings(), password_hasher=TEST_HASHER, clock=clock)

    assert service.verify_credentials(TEST_USERNAME, TEST_PASSWORD) is True
    assert service.verify_credentials("unknown", TEST_PASSWORD) is False
    assert service.verify_credentials(TEST_USERNAME, "wrong") is False

    token, created = service.create_session()
    verified = service.verify_session(token)

    assert verified.username == TEST_USERNAME
    assert created.expires_at == now + timedelta(minutes=30)
    assert verified.expires_at == created.expires_at
    assert TEST_PASSWORD not in token


def test_tampered_and_expired_sessions_are_rejected() -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    clock = MutableClock(now)
    service = AuthService(settings(), password_hasher=TEST_HASHER, clock=clock)
    token, _ = service.create_session()

    with pytest.raises(InvalidSessionError):
        service.verify_session(f"{token[:-1]}x")

    clock.value = now + timedelta(minutes=31)
    with pytest.raises(InvalidSessionError):
        service.verify_session(token)


def test_auth_settings_load_secrets_from_files(tmp_path: Path) -> None:
    hash_file = tmp_path / "password-hash"
    secret_file = tmp_path / "session-secret"
    hash_file.write_text(TEST_PASSWORD_HASH, encoding="utf-8")
    secret_file.write_text("s" * 48, encoding="utf-8")

    loaded = AuthSettings.from_environment(
        {
            "OPENREVIEWER_ADMIN_USERNAME": TEST_USERNAME,
            "OPENREVIEWER_ADMIN_PASSWORD_HASH_FILE": str(hash_file),
            "OPENREVIEWER_SESSION_SECRET_FILE": str(secret_file),
            "OPENREVIEWER_COOKIE_SECURE": "false",
        }
    )

    assert loaded.password_hash == TEST_PASSWORD_HASH
    assert loaded.session_secret == b"s" * 48
    assert loaded.cookie_name == "openreviewer_session"


def test_auth_settings_reject_plaintext_password_configuration() -> None:
    with pytest.raises(AuthConfigurationError, match="Argon2id"):
        AuthSettings.from_environment(
            {
                "OPENREVIEWER_ADMIN_USERNAME": TEST_USERNAME,
                "OPENREVIEWER_ADMIN_PASSWORD_HASH": "plain-text-is-not-accepted",
                "OPENREVIEWER_SESSION_SECRET": "s" * 48,
            }
        )


def test_login_failures_are_limited_by_sliding_window() -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    clock = MutableClock(now)
    limiter = LoginAttemptLimiter(
        maximum_failures=3,
        window=timedelta(minutes=10),
        clock=clock,
    )

    for _ in range(3):
        limiter.check("client|user")
        limiter.record_failure("client|user")

    with pytest.raises(LoginRateLimitError):
        limiter.check("client|user")

    clock.value = now + timedelta(minutes=11)
    limiter.check("client|user")
