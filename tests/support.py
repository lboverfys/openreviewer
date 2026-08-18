"""Shared non-production credentials and factories for tests."""

from argon2 import PasswordHasher

from services.auth import AuthService, AuthSettings


TEST_USERNAME = "test-administrator"
TEST_PASSWORD = "test-only-password"
TEST_HASHER = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
TEST_PASSWORD_HASH = TEST_HASHER.hash(TEST_PASSWORD)


def make_auth_service() -> AuthService:
    return AuthService(
        AuthSettings(
            username=TEST_USERNAME,
            password_hash=TEST_PASSWORD_HASH,
            session_secret=b"test-session-secret-is-at-least-32-bytes-long",
            cookie_secure=False,
        ),
        password_hasher=TEST_HASHER,
    )
