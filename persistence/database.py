"""SQLAlchemy engine configuration without hard-coded credentials."""

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path

from sqlalchemy import URL, Engine, create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker


class DatabaseConfigurationError(RuntimeError):
    """Required database configuration is missing or invalid."""


def _password_from_environment(values: Mapping[str, str]) -> str:
    """从环境变量或秘密文件读取数据库密码。

    项目允许用 ``OPENREVIEWER_DB_PASSWORD`` 直接配置密码，也允许通过
    ``OPENREVIEWER_DB_PASSWORD_FILE`` 指向只读秘密文件，但两者不能同时出现。
    统一在这里处理可以避免不同入口对空密码、文件读取失败和配置冲突有不同解释。

    参数：
        values: 环境变量映射；支持直接密码或密码文件路径两种来源。

    返回：
        去掉首尾空白后的数据库密码。密码只用于构造 SQLAlchemy URL，不在本函数
        中打印或持久化。

    异常：
        DatabaseConfigurationError: 两种来源同时出现、文件读取失败，或最终密码为空。
    """
    direct_password = values.get("OPENREVIEWER_DB_PASSWORD")
    password_file = values.get("OPENREVIEWER_DB_PASSWORD_FILE", "").strip()
    if direct_password and password_file:
        raise DatabaseConfigurationError(
            "configure only one of OPENREVIEWER_DB_PASSWORD and "
            "OPENREVIEWER_DB_PASSWORD_FILE"
        )
    if password_file:
        try:
            password = Path(password_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise DatabaseConfigurationError(
                "OPENREVIEWER_DB_PASSWORD_FILE could not be read"
            ) from exc
    else:
        password = direct_password or ""
    if not password:
        raise DatabaseConfigurationError(
            "OPENREVIEWER_DB_PASSWORD or OPENREVIEWER_DB_PASSWORD_FILE is required"
        )
    return password


def database_url_from_environment(
    environment: Mapping[str, str] | None = None,
) -> URL:
    """根据环境配置构造 SQLAlchemy 数据库 URL。

    如果提供了完整的 ``OPENREVIEWER_DATABASE_URL``，优先使用它；否则组合主机、
    端口、数据库名、用户名和密码字段。使用 SQLAlchemy 的 ``URL.create`` 而不是
    手工拼接字符串，能正确处理密码中的特殊字符，并避免把凭据写入日志。

    参数：
        environment: 可选环境映射；不传时读取当前进程环境，便于 API、Worker 和
            Alembic 在生产中共享同一套配置规则。

    返回：
        可直接交给 SQLAlchemy ``create_engine`` 的 ``URL`` 对象。显式 URL 会保留
        其驱动、主机、端口和数据库信息；分字段配置则默认使用 PostgreSQL、5432
        端口及 ``openreviewer`` 用户/数据库名。

    异常：
        DatabaseConfigurationError: 显式 URL 无法解析、密码缺失、端口不是整数或
        不在 1 到 65535 范围内。

    URL 对象内部仍包含密码，但 SQLAlchemy 的日志/渲染调用方应主动隐藏密码；本
    函数不会把它转换成普通日志字符串。
    """
    values = os.environ if environment is None else environment
    explicit_url = values.get("OPENREVIEWER_DATABASE_URL", "").strip()
    if explicit_url:
        try:
            return make_url(explicit_url)
        except Exception as exc:  # SQLAlchemy exposes multiple parse errors.
            raise DatabaseConfigurationError(
                "OPENREVIEWER_DATABASE_URL is invalid"
            ) from exc

    password = _password_from_environment(values)

    port_value = values.get("OPENREVIEWER_DB_PORT", "5432")
    try:
        port = int(port_value)
    except ValueError as exc:
        raise DatabaseConfigurationError(
            "OPENREVIEWER_DB_PORT must be an integer"
        ) from exc
    if not 1 <= port <= 65535:
        raise DatabaseConfigurationError(
            "OPENREVIEWER_DB_PORT must be between 1 and 65535"
        )

    return URL.create(
        drivername="postgresql+psycopg",
        username=values.get("OPENREVIEWER_DB_USER", "openreviewer"),
        password=password,
        host=values.get("OPENREVIEWER_DB_HOST", "postgres"),
        port=port,
        database=values.get("OPENREVIEWER_DB_NAME", "openreviewer"),
    )


@dataclass(slots=True)
class Database:
    engine: Engine
    sessions: sessionmaker[Session]

    @classmethod
    def connect(cls, url: str | URL) -> "Database":
        """创建数据库引擎和可复用的 SQLAlchemy 会话工厂。

        ``pool_pre_ping`` 会在取出连接前检查连接是否仍然可用，适合 PostgreSQL
        容器重启后的长生命周期 API/Worker。会话设置为提交后不立即过期，方便
        仓储层在事务提交后组装返回值。

        参数：
            url: PostgreSQL 或测试 SQLite 的连接 URL；生产调用方通常先经过
                :func:`database_url_from_environment` 构造。

        返回：
            包含 SQLAlchemy ``Engine`` 和绑定到该引擎的 ``sessionmaker`` 的
            ``Database``。此时只创建连接池，不主动执行查询。

        连接池的实际连接通常在第一次查询时建立；如果后续初始化失败，调用方仍
        应在清理路径调用 :meth:`dispose`。
        """
        engine = create_engine(url, pool_pre_ping=True)
        return cls(
            engine=engine,
            sessions=sessionmaker(
                bind=engine,
                class_=Session,
                expire_on_commit=False,
            ),
        )

    @classmethod
    def from_environment(cls) -> "Database":
        """读取当前进程环境并建立数据库连接。

        返回：
            使用 :func:`database_url_from_environment` 解析结果创建的 ``Database``。

        异常：
            DatabaseConfigurationError: 必需环境变量缺失或值不合法；底层引擎创建
            失败时会继续抛出 SQLAlchemy 自身的连接/配置异常。

        方法不读取项目文件之外的配置，也不会自动创建数据库或执行迁移；迁移由
        Alembic 单独负责。
        """
        return cls.connect(database_url_from_environment())

    def dispose(self) -> None:
        """释放连接池中的全部连接。

        该方法应在 API 生命周期结束、Worker 主函数退出或测试 fixture 清理时调用。
        它不会删除数据库中的表或数据，也不会影响同一数据库上由其他 ``Database``
        实例创建的连接池；重复调用是安全的。
        """
        self.engine.dispose()
