"""十万请求账本的稳定分页和索引检查；造数只进入 CI 隔离 PostgreSQL。"""

import json
from datetime import UTC, datetime

from sqlalchemy import event, insert, select, text
from sqlalchemy.orm import sessionmaker

from persistence.models import ModelUsageRequestRecord, RepositoryUsageMonthRecord
from persistence.usage_queries import UsageQueries
from services.rbac import ResourceScope
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)


def test_request_ledger_at_100k_rows(postgres_database):
    count = 100_000
    now = datetime(2026, 9, 13, tzinfo=UTC)
    with postgres_database.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                insert(RepositoryUsageMonthRecord),
                {
                    "id": "scale-month",
                    "installation_id": 10,
                    "repository": "scale/repo",
                    "repository_key": "scale/repo",
                    "month": now.replace(day=1),
                    "request_count": count,
                    "input_tokens": count,
                    "output_tokens": count,
                    "estimated_cost_microusd": count,
                    "reserved_cost_microusd": 0,
                    "unknown_count": 0,
                    "uncertain_count": 0,
                    "created_at": now,
                },
            )
            for start in range(0, count, 1000):
                connection.execute(
                    insert(ModelUsageRequestRecord),
                    [
                        {
                            "id": f"usage-scale-{number}",
                            "month_id": "scale-month",
                            "review_run_id": "scale-run",
                            "installation_id": 10,
                            "repository": "scale/repo",
                            "repository_key": "scale/repo",
                            "agent": "logic",
                            "purpose": "review",
                            "provider": "openai",
                            "model": "fixture-model",
                            "status": "settled",
                            "reserved_cost_microusd": 0,
                            "estimated_cost_microusd": 1,
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "created_at": now,
                        }
                        for number in range(start, start + 1000)
                    ],
                )
            connection.execute(text("ANALYZE model_usage_requests"))
            queries = UsageQueries(
                sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
            )
            statements = []

            def capture(_conn, _cursor, sql, *_args):
                if sql.lstrip().upper().startswith("SELECT"):
                    statements.append(sql)

            event.listen(connection, "before_cursor_execute", capture)
            try:
                first = queries.requests(
                    ResourceScope.unrestricted_scope(), "scale-month"
                )
                second = queries.requests(
                    ResourceScope.unrestricted_scope(),
                    "scale-month",
                    cursor=first.next_cursor,
                )
            finally:
                event.remove(connection, "before_cursor_execute", capture)
            assert len(first.items) == len(second.items) == 10 and len(statements) == 2
            assert not {item.id for item in first.items} & {
                item.id for item in second.items
            }
            statement = (
                select(ModelUsageRequestRecord.id)
                .where(ModelUsageRequestRecord.month_id == "scale-month")
                .order_by(
                    ModelUsageRequestRecord.created_at.desc(),
                    ModelUsageRequestRecord.id.desc(),
                )
                .limit(11)
            )
            sql = str(
                statement.compile(
                    dialect=connection.dialect, compile_kwargs={"literal_binds": True}
                )
            )
            plan = connection.execute(
                text("EXPLAIN (FORMAT JSON) " + sql)
            ).scalar_one()[0]
            assert "Index" in json.dumps(plan)
            print(
                json.dumps(
                    {
                        "ledger_rows": count,
                        "page_size": 10,
                        "list_queries": len(statements),
                        "plan": plan,
                    }
                )
            )
        finally:
            transaction.rollback()
