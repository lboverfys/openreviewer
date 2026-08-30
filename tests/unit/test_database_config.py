from pathlib import Path

from sqlalchemy import URL

from persistence.database import (
    DatabaseConfigurationError,
    DatabaseEngineSettings,
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


def test_postgres_engine_settings_apply_bounded_pool_and_sql_timeouts() -> None:
    settings = DatabaseEngineSettings.from_environment(
        {
            "OPENREVIEWER_DB_POOL_SIZE": "7",
            "OPENREVIEWER_DB_MAX_OVERFLOW": "3",
            "OPENREVIEWER_DB_POOL_TIMEOUT_SECONDS": "12",
            "OPENREVIEWER_DB_POOL_RECYCLE_SECONDS": "900",
            "OPENREVIEWER_DB_STATEMENT_TIMEOUT_MS": "45000",
            "OPENREVIEWER_DB_LOCK_TIMEOUT_MS": "4000",
            "OPENREVIEWER_DB_IDLE_TRANSACTION_TIMEOUT_MS": "55000",
            "OPENREVIEWER_DB_APPLICATION_NAME": "openreviewer-test",
        }
    )

    options = settings.engine_options(
        "postgresql+psycopg://user:password@postgres/openreviewer"
    )

    assert options["pool_size"] == 7
    assert options["max_overflow"] == 3
    assert options["pool_timeout"] == 12
    assert options["pool_recycle"] == 900
    assert options["pool_use_lifo"] is True
    assert options["pool_reset_on_return"] == "rollback"
    assert options["connect_args"] == {
        "application_name": "openreviewer-test",
        "options": (
            "-c statement_timeout=45000 -c lock_timeout=4000 "
            "-c idle_in_transaction_session_timeout=55000"
        ),
    }


def test_sqlite_engine_does_not_receive_postgres_pool_or_session_options() -> None:
    options = DatabaseEngineSettings().engine_options("sqlite:///:memory:")

    assert options == {"pool_pre_ping": True}


def test_database_engine_settings_reject_unbounded_values() -> None:
    try:
        DatabaseEngineSettings.from_environment(
            {"OPENREVIEWER_DB_STATEMENT_TIMEOUT_MS": "0"}
        )
    except DatabaseConfigurationError as exc:
        assert "OPENREVIEWER_DB_STATEMENT_TIMEOUT_MS" in str(exc)
    else:
        raise AssertionError("unbounded database timeout should fail")
