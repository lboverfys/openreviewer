from pathlib import Path

from sqlalchemy import URL

from persistence.database import (
    DatabaseConfigurationError,
    database_url_from_environment,
)


def test_database_url_uses_separate_fields_without_manual_url_encoding() -> None:
    """验证分字段配置会通过结构化 URL 正确保留特殊字符密码。

    前提：密码包含 URL 中有语义的冒号、@ 和斜杠，同时使用非默认主机/端口。
    动作：调用 ``database_url_from_environment`` 构造 SQLAlchemy URL。
    预期：驱动、主机、端口、库名、用户和原始密码都能分别读回，证明代码没有
    使用容易错误拆分或泄露凭据的手工字符串拼接。
    """
    url = database_url_from_environment(
        {
            "OPENREVIEWER_DB_HOST": "postgres.internal",
            "OPENREVIEWER_DB_PORT": "5433",
            "OPENREVIEWER_DB_NAME": "reviews",
            "OPENREVIEWER_DB_USER": "review_user",
            "OPENREVIEWER_DB_PASSWORD": "colon:@/password",
        }
    )

    assert isinstance(url, URL)
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "postgres.internal"
    assert url.port == 5433
    assert url.database == "reviews"
    assert url.username == "review_user"
    assert url.password == "colon:@/password"


def test_database_url_requires_a_password() -> None:
    """验证完全缺少数据库密码时会在配置解析阶段立即失败。

    动作：传入空环境映射。
    预期：抛出 ``DatabaseConfigurationError``，并给出两种受支持密码来源的稳定
    提示；若没有异常则显式让测试失败，防止生产误用空密码连接。
    """
    try:
        database_url_from_environment({})
    except DatabaseConfigurationError as exc:
        assert str(exc) == (
            "OPENREVIEWER_DB_PASSWORD or OPENREVIEWER_DB_PASSWORD_FILE is required"
        )
    else:
        raise AssertionError("missing database password should fail")


def test_database_password_can_be_read_from_a_secret_file(tmp_path: Path) -> None:
    """验证数据库密码可以从 secret 文件读取并清理换行。

    参数：
        tmp_path: pytest 提供的隔离临时目录，不会接触真实部署 secret。

    前提：密码文件末尾带常见换行符。
    动作：仅设置 ``OPENREVIEWER_DB_PASSWORD_FILE`` 并构造 URL。
    预期：URL 内密码不含末尾换行，保证 Kubernetes/Docker secret 文件格式不会
    导致认证失败。
    """
    password_file = tmp_path / "postgres-password"
    password_file.write_text("file-only-password\n", encoding="utf-8")

    url = database_url_from_environment(
        {
            "OPENREVIEWER_DB_HOST": "postgres",
            "OPENREVIEWER_DB_PASSWORD_FILE": str(password_file),
        }
    )

    assert url.password == "file-only-password"
