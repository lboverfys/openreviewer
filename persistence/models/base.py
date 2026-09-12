"""按职责集中维护的 base 数据记录。"""

from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    """生成 ORM 默认使用的当前 UTC 时间。

    返回：
        带 ``UTC`` 时区信息的 ``datetime``。SQLAlchemy 在创建没有显式时间值的
        记录时调用它，例如任务的 ``created_at`` 和心跳的 ``last_seen_at``。

    该函数不使用数据库服务器时间，因此测试可以通过显式传值或服务层注入时钟
    获得确定性结果；它也不会修改任何全局时钟配置。
    """
    return datetime.now(UTC)


def enum_values(enum_type: type[Enum]) -> str:
    """把 Python 枚举转换成迁移/约束所需的 SQL 字符串列表。

    数据库模型使用字符串列而不是原生数据库枚举，因此需要在 CHECK 约束中
    重复声明允许值。该辅助函数集中生成带引号的值，避免各模型手工拼接。

    参数：
        enum_type: 成员值为字符串的 Python ``Enum`` 类型。

    返回：
        以逗号分隔、每个值带单引号的 SQL 片段，例如 ``'queued', 'running'``。

    该片段只用于项目内固定枚举定义生成约束；它不是通用 SQL 转义器，不应接收
    来自用户请求的任意字符串。
    """
    return ", ".join(f"'{member.value}'" for member in enum_type)
