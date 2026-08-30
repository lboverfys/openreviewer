"""把用户资源范围转换成 SQLAlchemy 可复用的过滤条件。"""

from typing import Any

from sqlalchemy import and_, false, or_, true

from services.rbac import ResourceScope


def resource_predicate(
    scope: ResourceScope | None,
    *,
    installation_column: Any,
    repository_column: Any,
    repository_key_column: Any | None = None,
) -> Any:
    """返回资源范围条件，确保聚合和详情查询在数据库侧先完成隔离。

    未提供范围代表兼容旧调用方的全量读取；显式空范围返回恒假条件，避免把
    “没有授权”误当成“全部授权”。组织前缀通过 SQLAlchemy 的自动转义生成，
    即使合法 GitHub 名称包含下划线也不会被当成 SQL ``LIKE`` 通配符。受限查询
    应传入 ``repository_key_column``；未传入时保留旧调用方的精确匹配兼容性，
    但无法纠正历史展示大小写差异。
    """

    if scope is None or scope.unrestricted:
        return true()

    conditions: list[Any] = []
    if scope.installation_ids:
        conditions.append(installation_column.in_(scope.installation_ids))

    repository_match_column = repository_key_column or repository_column
    repository_conditions: list[Any] = []
    if scope.repositories:
        # scope 和 repository_key 都已在写入/构造边界 casefold；比较不对索引列
        # 套函数，让精确仓库范围仍能利用普通 B-tree 索引。
        repository_conditions.append(repository_match_column.in_(scope.repositories))
    repository_conditions.extend(
        repository_match_column.startswith(f"{organization}/", autoescape=True)
        for organization in scope.organizations
    )
    if repository_conditions:
        conditions.append(or_(*repository_conditions))

    if not conditions:
        return false()
    return and_(*conditions)
