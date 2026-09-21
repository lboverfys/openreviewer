"""PostgreSQL 原生分位数与原窗口算法保持相同的空值、分组和最近秩语义。"""

from sqlalchemy import true

from persistence.models import ModelUsageRequestRecord
from persistence.usage_statistics import request_statistics_statement
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)
from tests.integration.test_review_insights import usage


def test_postgres_ordered_set_matches_window_with_unknowns_and_ties(postgres_database):
    with postgres_database.sessions() as session:
        session.add_all([
            usage("percentile-a", "run", duration=100, cost=10),
            usage("percentile-b", "run", duration=100, cost=None, status="uncertain"),
            usage("percentile-c", "run", duration=900, cost=30),
            usage("percentile-d", "run", duration=None, cost=None, status="reserved"),
        ])
        # 通过同一事务比较，数据不会留给其他测试。
        session.flush()
        for dimensions in ((), ("agent",), ("provider", "model", "purpose")):
            for predicate in (true(), ModelUsageRequestRecord.id == "missing"):
                baseline = session.execute(request_statistics_statement(predicate, dimensions)).mappings().all()
                current = session.execute(request_statistics_statement(predicate, dimensions, postgres=True)).mappings().all()
                assert [dict(row) for row in baseline] == [dict(row) for row in current]
        session.rollback()
