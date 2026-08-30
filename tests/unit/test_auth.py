from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.auth import (
    AuthConfigurationError,
    AuthService,
    AuthSettings,
    InMemorySessionStore,
    InvalidSessionError,
    LoginAttemptLimiter,
    LoginRateLimitError,
)
from services.rbac import AccessRole
from tests.support import (
    TEST_HASHER,
    TEST_PASSWORD,
    TEST_PASSWORD_HASH,
    TEST_USERNAME,
)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        """创建可由测试手工推进的时钟。

        参数：
            value: 初始“当前时间”，通常使用带 UTC 时区的固定值。

        生产代码只依赖无参数 callable，因此测试直接修改 ``value`` 就能模拟会话
        过期和限流窗口流逝，无需真实等待。
        """
        self.value = value

    def __call__(self) -> datetime:
        """返回当前测试时间，匹配生产代码的时钟 callable 协议。

        返回：
            当前保存的 ``datetime``，不自动前进。只有测试显式修改 ``value`` 时
            时间才变化，因此同一断言阶段内所有时间计算完全确定。
        """
        return self.value


def settings() -> AuthSettings:
    """构造一个 TTL 为 30 分钟的测试认证配置。

    返回：
        使用共享测试用户名、预先计算的 Argon2id 哈希、固定签名密钥和非 Secure
        Cookie 的 ``AuthSettings``。

    该辅助函数只减少测试样板；所有值都是测试专用数据，不从真实环境变量读取。
    """
    return AuthSettings(
        username=TEST_USERNAME,
        password_hash=TEST_PASSWORD_HASH,
        session_secret=b"test-session-secret-is-at-least-32-bytes-long",
        cookie_secure=False,
        session_ttl=timedelta(minutes=30),
    )


def test_credentials_and_signed_session_round_trip() -> None:
    """验证凭据校验和签名会话的完整成功/失败路径。

    前提：固定管理员配置和固定 UTC 时钟。
    动作：分别校验正确账号、错误账号、错误密码，再创建并重新验证会话 Token。
    预期：只有完整正确凭据通过；会话主体和 30 分钟到期时间保持一致，且 Token
    文本不包含管理员明文密码。
    """
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
    """验证签名篡改和时间过期都会使会话失效。

    前提：在固定时刻创建一个有效 Token。
    动作：先改动 Token 最后一个字符，再把测试时钟推进到 TTL 之后验证原 Token。
    预期：两种情况都抛出 ``InvalidSessionError``，证明签名完整性和有效期是两个
    独立门槛，不能仅靠客户端 Cookie 到期属性保证安全。
    """
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    clock = MutableClock(now)
    service = AuthService(settings(), password_hasher=TEST_HASHER, clock=clock)
    token, _ = service.create_session()

    with pytest.raises(InvalidSessionError):
        service.verify_session(f"{token[:-1]}x")

    clock.value = now + timedelta(minutes=31)
    with pytest.raises(InvalidSessionError):
        service.verify_session(token)


def test_revoked_session_is_rejected() -> None:
    """服务端吊销后，即使客户端仍持有签名正确的 Cookie 也必须失效。"""

    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    service = AuthService(
        settings(),
        password_hasher=TEST_HASHER,
        clock=MutableClock(now),
    )
    token, _ = service.create_session()
    service.revoke_session(token)

    with pytest.raises(InvalidSessionError, match="revoked"):
        service.verify_session(token)


def test_all_sessions_can_be_revoked_without_touching_other_users() -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    store = InMemorySessionStore()
    service = AuthService(
        settings(),
        password_hasher=TEST_HASHER,
        clock=MutableClock(now),
        session_store=store,
    )
    first, _ = service.create_session()
    second, _ = service.create_session()
    assert service.revoke_all_sessions(TEST_USERNAME) == 2
    with pytest.raises(InvalidSessionError, match="revoked"):
        service.verify_session(first)
    with pytest.raises(InvalidSessionError, match="revoked"):
        service.verify_session(second)


def test_auth_settings_load_secrets_from_files(tmp_path: Path) -> None:
    """验证容器 secret 文件可以提供密码哈希和会话密钥。

    参数：
        tmp_path: pytest 为本用例创建的隔离临时目录。

    前提：分别写入 Argon2id 哈希和 48 字节签名密钥文件。
    动作：只配置两个 ``*_FILE`` 环境字段并关闭 Secure Cookie。
    预期：读取结果与文件内容一致，且 Cookie 名切换成本地 HTTP 测试使用的普通名称。
    """
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


def test_session_secret_rotation_accepts_old_tokens_and_signs_with_current_key() -> None:
    """轮换窗口内接受旧 Token，但新 Token 只能由当前密钥验证。"""

    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    clock = MutableClock(now)
    store = InMemorySessionStore()
    old_secret = b"old-session-secret-is-at-least-32-bytes-long"
    current_secret = b"new-session-secret-is-at-least-32-bytes-long"
    old_settings = AuthSettings(
        username=TEST_USERNAME,
        password_hash=TEST_PASSWORD_HASH,
        session_secret=old_secret,
        cookie_secure=False,
    )
    rotated_settings = AuthSettings(
        username=TEST_USERNAME,
        password_hash=TEST_PASSWORD_HASH,
        session_secret=current_secret,
        previous_session_secrets=(old_secret,),
        cookie_secure=False,
    )
    old_service = AuthService(old_settings, clock=clock, session_store=store)
    rotated_service = AuthService(rotated_settings, clock=clock, session_store=store)

    old_token, _ = old_service.create_session()
    assert rotated_service.verify_session(old_token).username == TEST_USERNAME

    current_token, _ = rotated_service.create_session()
    with pytest.raises(InvalidSessionError, match="signature"):
        old_service.verify_session(current_token)


def test_auth_settings_load_and_validate_previous_session_secrets() -> None:
    """旧会话密钥列表必须是有界、无重复的 JSON 数组。"""

    loaded = AuthSettings.from_environment(
        {
            "OPENREVIEWER_ADMIN_USERNAME": TEST_USERNAME,
            "OPENREVIEWER_ADMIN_PASSWORD_HASH": TEST_PASSWORD_HASH,
            "OPENREVIEWER_SESSION_SECRET": "n" * 48,
            "OPENREVIEWER_SESSION_PREVIOUS_SECRETS_JSON": '["o' + "o" * 47 + '"]',
        }
    )

    assert loaded.previous_session_secrets == (b"o" * 48,)

    with pytest.raises(AuthConfigurationError, match="unique"):
        AuthSettings.from_environment(
            {
                "OPENREVIEWER_ADMIN_USERNAME": TEST_USERNAME,
                "OPENREVIEWER_ADMIN_PASSWORD_HASH": TEST_PASSWORD_HASH,
                "OPENREVIEWER_SESSION_SECRET": "n" * 48,
                "OPENREVIEWER_SESSION_PREVIOUS_SECRETS_JSON": '["n' + "n" * 47 + '"]',
            }
        )


def test_additional_user_scope_is_carried_by_signed_session() -> None:
    configured = AuthSettings.from_environment(
        {
            "OPENREVIEWER_ADMIN_USERNAME": TEST_USERNAME,
            "OPENREVIEWER_ADMIN_PASSWORD_HASH": TEST_PASSWORD_HASH,
            "OPENREVIEWER_SESSION_SECRET": "s" * 48,
            "OPENREVIEWER_AUTH_USERS_JSON": (
                '[{"username":"reviewer","password_hash":"'
                + TEST_PASSWORD_HASH
                + '","role":"viewer","scope":{"installation_ids":[10],'
                '"organizations":["lboverfys"],"repositories":[]}}]'
            ),
        }
    )
    reviewer = next(user for user in configured.users if user.username == "reviewer")
    assert reviewer.role is AccessRole.VIEWER
    assert reviewer.resource_scope.allows(10, "lboverfys/NiuMa") is True
    assert reviewer.resource_scope.allows(10, "other/secret") is False

    service = AuthService(
        configured,
        password_hasher=TEST_HASHER,
    )
    authenticated = service.authenticate("reviewer", TEST_PASSWORD)
    assert authenticated is not None
    token, principal = service.create_session(authenticated)
    verified = service.verify_session(token)
    assert principal.resource_scope == verified.resource_scope
    assert verified.resource_scope.allows(10, "lboverfys/NiuMa") is True


def test_auth_settings_reject_plaintext_password_configuration() -> None:
    """验证认证配置不会把明文密码误当成哈希接受。

    前提：用户名和会话密钥合法，但密码字段不带 ``$argon2id$`` 前缀。
    动作：调用 ``AuthSettings.from_environment``。
    预期：在应用启动配置阶段抛出包含 Argon2id 提示的
    ``AuthConfigurationError``，而不是等到第一次登录才失败。
    """
    with pytest.raises(AuthConfigurationError, match="Argon2id"):
        AuthSettings.from_environment(
            {
                "OPENREVIEWER_ADMIN_USERNAME": TEST_USERNAME,
                "OPENREVIEWER_ADMIN_PASSWORD_HASH": "plain-text-is-not-accepted",
                "OPENREVIEWER_SESSION_SECRET": "s" * 48,
            }
        )


def test_login_failures_are_limited_by_sliding_window() -> None:
    """验证进程内滑动窗口会限流，并在旧记录过期后自动恢复。

    前提：限制为 10 分钟内最多 3 次失败，使用可推进时钟。
    动作：连续检查并记录 3 次失败，再执行第 4 次检查；随后把时间推进 11 分钟。
    预期：第 4 次检查抛出 ``LoginRateLimitError``，窗口过去后同一键重新允许尝试，
    证明清理逻辑依据时间窗口而不是永久封禁。
    """
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


def test_in_memory_login_limiter_does_not_create_unknown_keys() -> None:
    """只读检查未知键时不应留下空队列，避免随机键耗尽内存。"""

    limiter = LoginAttemptLimiter(maximum_keys=2)

    limiter.check("unknown-1")
    limiter.check("unknown-2")

    assert len(limiter._failures) == 0


def test_in_memory_login_limiter_evicts_old_keys_at_capacity() -> None:
    """超过容量时应淘汰最久未访问的键，而不是无限增长。"""

    limiter = LoginAttemptLimiter(maximum_failures=3, maximum_keys=2)
    limiter.record_failure("first")
    limiter.record_failure("second")
    limiter.record_failure("third")

    assert len(limiter._failures) == 2
    assert "first" not in limiter._failures
    assert set(limiter._failures) == {"second", "third"}
