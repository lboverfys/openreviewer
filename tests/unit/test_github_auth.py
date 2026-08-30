import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from domain.security import ErrorCode, SafeApplicationError
from services.github import GitHubApiClient, GitHubClientSettings
from services.github_auth import (
    GITHUB_PUBLISH_TOKEN_SCOPE,
    GITHUB_READ_TOKEN_SCOPE,
    GitHubAppSettings,
    GitHubAppTokenProvider,
    GitHubTokenScope,
)

APP_ID = 4699977
INSTALLATION_ID = 156153422
FAKE_INSTALLATION_TOKEN = "ghs_FAKEINSTALLATIONTOKEN123456789"
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _write_test_key(path: Path) -> bytes:
    """生成只属于临时测试目录的 RSA 私钥，并返回对应公钥。"""

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def test_app_jwt_requests_and_caches_short_lived_installation_token(
    tmp_path: Path,
) -> None:
    """验证 App JWT 身份、Token 解析和仅内存缓存。"""

    private_key_file = tmp_path / "github-app.pem"
    public_key = _write_test_key(private_key_file)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == (
            f"/app/installations/{INSTALLATION_ID}/access_tokens"
        )
        assert json.loads(request.content) == {
            "permissions": {
                "checks": "read",
                "contents": "read",
                "pull_requests": "read",
                "statuses": "read",
            }
        }
        authorization = request.headers["authorization"]
        assert authorization.startswith("Bearer ")
        claims = jwt.decode(
            authorization.removeprefix("Bearer "),
            public_key,
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims["iss"] == str(APP_ID)
        assert claims["iat"] == int((NOW - timedelta(seconds=60)).timestamp())
        assert claims["exp"] == int((NOW + timedelta(minutes=9)).timestamp())
        return httpx.Response(
            201,
            json={
                "token": FAKE_INSTALLATION_TOKEN,
                "expires_at": "2026-08-24T13:00:00Z",
            },
        )

    api = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
    )
    provider = GitHubAppTokenProvider(
        api,
        GitHubAppSettings(
            app_id=APP_ID,
            private_key_file=private_key_file,
        ),
        GITHUB_READ_TOKEN_SCOPE,
        clock=lambda: NOW,
    )

    assert provider.get_token(INSTALLATION_ID) == FAKE_INSTALLATION_TOKEN
    assert provider.get_token(INSTALLATION_ID) == FAKE_INSTALLATION_TOKEN
    assert len(requests) == 1
    assert not (tmp_path / "installation-token").exists()


def test_app_settings_reject_missing_values_without_reading_process_environment() -> None:
    """验证显式空环境不会意外回退到当前进程环境。"""

    try:
        GitHubAppSettings.from_environment({})
    except ValueError as error:
        assert "OPENREVIEWER_GITHUB_APP_ID" in str(error)
    else:
        raise AssertionError("空配置必须被拒绝")


def test_private_key_reader_rejects_oversized_file_before_signing(
    tmp_path: Path,
) -> None:
    """验证超出配置上限的私钥不会进入签名流程。"""

    private_key_file = tmp_path / "oversized-key.pem"
    private_key_file.write_bytes(b"x" * 2048)
    try:
        GitHubAppTokenProvider(
            GitHubApiClient(
                GitHubClientSettings(api_base_url="https://api.github.test"),
                client=httpx.Client(
                    base_url="https://api.github.test",
                    transport=httpx.MockTransport(
                        lambda _request: httpx.Response(500),
                    ),
                ),
            ),
            GitHubAppSettings(
                app_id=APP_ID,
                private_key_file=private_key_file,
                max_private_key_bytes=1024,
            ),
            GITHUB_READ_TOKEN_SCOPE,
        )
    except ValueError as error:
        assert "private key file size" in str(error)
    else:
        raise AssertionError("超出上限的私钥必须被拒绝")


def test_installation_token_expiry_must_include_timezone(tmp_path: Path) -> None:
    """验证 Token 过期时间缺少时区时不会按本地时区误解释。"""

    private_key_file = tmp_path / "github-app.pem"
    _write_test_key(private_key_file)
    api = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    201,
                    json={
                        "token": FAKE_INSTALLATION_TOKEN,
                        "expires_at": "2026-08-24T13:00:00",
                    },
                ),
            ),
        ),
    )
    provider = GitHubAppTokenProvider(
        api,
        GitHubAppSettings(app_id=APP_ID, private_key_file=private_key_file),
        GITHUB_PUBLISH_TOKEN_SCOPE,
        clock=lambda: NOW,
    )

    with pytest.raises(SafeApplicationError) as captured:
        provider.get_token(INSTALLATION_ID)
    assert captured.value.error.code is ErrorCode.GITHUB_INVALID_RESPONSE


def test_token_scope_rejects_unknown_or_duplicate_permissions() -> None:
    with pytest.raises(ValueError, match="permissions"):
        GitHubTokenScope((("administration", "write"),))
    with pytest.raises(ValueError, match="permissions"):
        GitHubTokenScope((("contents", "read"), ("contents", "write")))


def test_publish_scope_contains_only_required_write_permissions() -> None:
    assert GITHUB_PUBLISH_TOKEN_SCOPE.request_body() == {
        "permissions": {
            "checks": "write",
            "pull_requests": "write",
        }
    }
