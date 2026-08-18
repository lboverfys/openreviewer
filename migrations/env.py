"""Alembic runtime configuration sourced from OpenReviewer settings."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import URL, create_engine, make_url, pool

from persistence.database import database_url_from_environment
from persistence.models import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def migration_url() -> URL:
    """取得 Alembic 运行时使用的数据库 URL。

    命令行配置文件中的 ``sqlalchemy.url`` 具有最高优先级；未提供时复用应用
    自身的环境变量解析逻辑，确保迁移、API 和 Worker 不会连接到不同数据库。

    返回：
        Alembic 当前运行应使用的 SQLAlchemy ``URL``。配置文件中的 URL 适合
        一次性命令覆盖，环境变量路径适合容器部署和日常发布。

    异常：
        DatabaseConfigurationError: 环境配置缺失或不合法。
        ValueError/SQLAlchemy URL 异常: 配置文件中的 URL 无法解析。

    URL 对象可能包含密码；调用方在日志中输出时必须使用隐藏密码的渲染方式。
    """
    configured_url = config.get_main_option("sqlalchemy.url", "").strip()
    if configured_url:
        return make_url(configured_url)
    return database_url_from_environment()


def run_migrations_offline() -> None:
    """在不建立数据库连接的情况下生成离线迁移 SQL。

    该模式读取 URL 只是为了确定数据库方言和绑定参数，不会打开网络连接；
    ``literal_binds`` 让 Alembic 尽量把参数写入输出 SQL，适合审阅或交给外部
    发布系统执行。迁移脚本仍由 Alembic 的上下文按版本顺序驱动。

    异常：
        配置 URL 无法解析或迁移脚本本身失败时，异常向上传给命令行进程。
    """
    url = migration_url()
    context.configure(
        url=url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """建立临时引擎并在线执行 Alembic 迁移。

    执行流程：
        1. 用 :func:`migration_url` 创建不带连接池复用的临时引擎；
        2. 打开一个连接并把 Alembic 上下文绑定到该连接；
        3. 在 Alembic 事务中按版本执行迁移；
        4. 退出连接上下文后显式释放引擎。

    使用 ``NullPool`` 是因为迁移是一次性命令，不需要把连接留给 API/Worker；
    该函数只改数据库结构，不启动应用服务或处理任务数据。
    """
    connectable = create_engine(migration_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
