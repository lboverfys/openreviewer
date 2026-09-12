"""只在CI隔离PostgreSQL运行：一万及十万任务的分页与执行计划。"""

import json
import math
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, insert, select, text
from sqlalchemy.orm import sessionmaker

from persistence.dashboard import SqlAlchemyDashboardRepository
from persistence.models import ReviewRunRecord, ReviewTaskRecord
from services.dashboard import DashboardService
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)


@pytest.mark.parametrize("count", [10_000, 100_000])
def test_management_scale(postgres_database, count):
    # 外层事务回滚全部造数；不连接业务库，不请求模型，内存每批最多1000行。
    with postgres_database.engine.connect() as connection:
        transaction = connection.begin()
        try:
            now = datetime(2026, 9, 12, tzinfo=UTC)
            for offset in range(0, count, 1000):
                runs = [{"id": f"scale-{n}", "review_version_key": f"scale:{n}",
                    "installation_id": 10, "repository_id": 42, "repository": "scale/repo",
                    "repository_key": "scale/repo", "pull_request_number": n + 1,
                    "head_sha": "a" * 40, "execution_status": "completed", "workflow_status": "completed",
                    "coverage_status": "complete", "idempotency_key": f"scale-{n}",
                    "request_fingerprint": "b" * 64, "created_at": now, "updated_at": now,
                } for n in range(offset, min(offset + 1000, count))]
                connection.execute(insert(ReviewRunRecord), runs)
                connection.execute(insert(ReviewTaskRecord), [{"id": row["id"], "review_run_id": row["id"],
                    "execution_status": "completed"} for row in runs])
            connection.execute(text("ANALYZE review_runs"))
            connection.execute(text("ANALYZE review_tasks"))
            sessions = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
            service = DashboardService(SqlAlchemyDashboardRepository(sessions))
            first = service.snapshot(10, include_overview=False)
            assert first.total_reviews == count and len(first.recent_reviews) == 10
            sql_counts = []
            def capture(_conn, _cursor, statement, _params, _context, _many):
                if statement.lstrip().upper().startswith("SELECT"):
                    sql_counts.append(statement)
            event.listen(connection, "before_cursor_execute", capture)
            durations = []
            try:
                cursor = first.next_cursor
                for _ in range(20):
                    before = time.perf_counter()
                    page = service.snapshot(10, cursor, include_overview=False)
                    durations.append((time.perf_counter() - before) * 1000)
                    assert len(page.recent_reviews) == 10
                    cursor = page.next_cursor
            finally:
                event.remove(connection, "before_cursor_execute", capture)
            assert len(sql_counts) <= 42  # 超过五秒时允许计数重新校验。
            query = select(ReviewRunRecord.id).order_by(ReviewRunRecord.created_at.desc(), ReviewRunRecord.id.desc()).limit(11)
            sql = str(query.compile(dialect=connection.dialect, compile_kwargs={"literal_binds": True}))
            plan = connection.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)).scalar_one()[0]
            assert "Index" in json.dumps(plan)
            ordered = sorted(durations)
            print(json.dumps({"tasks": count, "page_size": 10, "requests": 20,
                "sql_queries": len(sql_counts), "p95_ms": round(ordered[math.ceil(len(ordered) * .95) - 1], 2),
                "page_plan_ms": plan["Execution Time"], "plan": plan["Plan"]}, ensure_ascii=False))
        finally:
            transaction.rollback()
