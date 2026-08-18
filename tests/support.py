"""Shared non-production credentials and factories for tests."""

from argon2 import PasswordHasher

from services.auth import AuthService, AuthSettings


TEST_USERNAME = "test-administrator"
TEST_PASSWORD = "test-only-password"
TEST_HASHER = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
TEST_PASSWORD_HASH = TEST_HASHER.hash(TEST_PASSWORD)


def make_auth_service() -> AuthService:
    """创建使用固定测试凭据的认证服务。

    测试专用哈希器降低了 Argon2 计算成本，固定的签名密钥和非 Secure Cookie
    只用于内存 ASGI 客户端，不可复制到生产配置。

    返回：
        使用模块级固定账号/密码、30 字节以上测试签名密钥和低成本 Argon2
        哈希器的 ``AuthService``。

    该工厂让 API 集成测试共享相同认证规则，同时避免每个用例重复生成配置。
    ``cookie_secure=False`` 只因为 httpx 的内存测试入口使用 HTTP；生产 Compose
    会强制 Secure Cookie。
    """
    return AuthService(
        AuthSettings(
            username=TEST_USERNAME,
            password_hash=TEST_PASSWORD_HASH,
            session_secret=b"test-session-secret-is-at-least-32-bytes-long",
            cookie_secure=False,
        ),
        password_hasher=TEST_HASHER,
    )
