"""月度分组与诊断共用实际请求口径，窗口函数只在数据库内排序。"""

from sqlalchemy import and_, case, func, or_, select

from persistence.models import ModelUsageRequestRecord as Request


def request_statistics_statement(predicate, dimensions: tuple[str, ...] = (), *, postgres: bool = False):
    groups = [getattr(Request, name) for name in dimensions]
    fields = ("status", "response_status", "duration_ms", "estimated_cost_microusd", "reserved_cost_microusd", "cost_reason")
    ranked = select(
        *groups, *(getattr(Request, name) for name in fields),
        func.row_number().over(partition_by=groups or None,
            order_by=(Request.duration_ms.asc().nulls_last(), Request.id)).label("position"),
        func.count(Request.duration_ms).over(partition_by=groups or None).label("timed_count"),
    ).where(predicate).subquery()
    # PostgreSQL 的有序集合聚合只排序耗时列，不把每条账本连同窗口计数反复物化。
    # SQLite 的隔离测试继续使用等价的最近秩算法；两条路径都忽略未知耗时。
    row = Request if postgres else ranked.c
    priced = and_(row.status == "settled", row.estimated_cost_microusd.is_not(None))
    unpriced = row.estimated_cost_microusd.is_(None)
    statement = select(
        *(getattr(row, name) for name in dimensions),
        func.count().label("request_count"),
        func.coalesce(func.sum(row.estimated_cost_microusd), 0).label("estimated_cost_microusd"),
        func.count().filter(row.estimated_cost_microusd.is_(None)).label("unknown_count"),
        func.count(row.estimated_cost_microusd).label("known_count"),
        func.count().filter(and_(unpriced, row.cost_reason.in_(("usage_missing", "incomplete_response")))).label("missing_usage_count"),
        func.count().filter(and_(unpriced, row.cost_reason.in_(("pricing_missing", "cache_price_missing")))).label("missing_price_count"),
        func.count().filter(and_(unpriced, row.status != "reserved",
            or_(row.cost_reason.is_(None), row.cost_reason == "legacy_unknown"))).label("legacy_unknown_count"),
        func.count().filter(and_(row.status == "uncertain", row.estimated_cost_microusd.is_not(None))).label("partial_cost_count"),
        *(func.count().filter(row.status == status).label(f"{status}_count") for status in ("reserved", "uncertain", "settled")),
        func.count().filter(row.response_status.between(200, 299)).label("http_2xx_count"),
        func.count().filter(or_(row.response_status < 200, row.response_status >= 300)).label("http_non_2xx_count"),
        func.count().filter(row.response_status.is_(None)).label("http_unknown_count"),
        func.count(row.duration_ms).label("duration_sample_count"),
        *((func.percentile_disc(percentile / 100).within_group(row.duration_ms)
           if postgres else func.min(case((ranked.c.position * 100 >= ranked.c.timed_count * percentile, ranked.c.duration_ms))))
          .label(f"p{percentile}_duration_ms") for percentile in (50, 95)),
        func.count().filter(priced).label("settled_priced_count"),
        func.coalesce(func.sum(case((priced, row.reserved_cost_microusd), else_=0)), 0).label("settled_reservation_microusd"),
        func.coalesce(func.sum(case((priced, row.estimated_cost_microusd), else_=0)), 0).label("settled_cost_microusd"),
    ).select_from(Request if postgres else ranked)
    if postgres:
        statement = statement.where(predicate)
    if dimensions:
        statement = statement.add_columns(
            func.sum(func.count()).over().label("total_request_count"),
            func.sum(func.coalesce(func.sum(row.estimated_cost_microusd), 0)).over().label("total_estimated_cost_microusd"),
            func.sum(func.count().filter(row.estimated_cost_microusd.is_(None))).over().label("total_unknown_count"),
        ).group_by(*(getattr(row, name) for name in dimensions)).order_by(
            func.count().desc(), *(getattr(row, name) for name in dimensions),
        )
    return statement
