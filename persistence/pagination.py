"""管理列表共享的稳定游标条件。"""

from sqlalchemy import and_, or_

from domain.pagination import decode_cursor


def apply_cursor(statement, time_column, id_column, cursor: str | None):
    if cursor:
        created_at, identifier = decode_cursor(cursor)
        statement = statement.where(or_(
            time_column < created_at,
            and_(time_column == created_at, id_column < identifier),
        ))
    return statement.order_by(time_column.desc(), id_column.desc())
