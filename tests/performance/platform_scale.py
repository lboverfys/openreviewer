"""十万请求账本的稳定分页和索引检查；造数只进入 CI 隔离 PostgreSQL。"""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import event, insert, select, text, update
from sqlalchemy.orm import sessionmaker

from persistence.models import (
    ModelUsageRequestRecord,
    RepositoryUsageMonthRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
)
from persistence.review_insights import collect_review_insights
from persistence.usage_queries import UsageQueries
from persistence.usage_statistics import request_statistics_statement
from services.rbac import ResourceScope
from tests.integration.test_postgres_contract import (
    _prepare_postgres_budget_lease,
)
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)


def test_request_ledger_at_100k_rows(postgres_database):
    count = 100_000
    now = datetime(2026, 9, 13, tzinfo=UTC)
    _, lease, plan_id = _prepare_postgres_budget_lease(postgres_database)
    with postgres_database.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(update(ReviewRunRecord).where(ReviewRunRecord.id == lease.review_run_id)
                .values(coverage_status="complete", created_at=now - timedelta(seconds=10)))
            connection.execute(update(ReviewPlanRecord).where(ReviewPlanRecord.id == plan_id)
                .values(model_review_completed_at=now))
            connection.execute(
                insert(RepositoryUsageMonthRecord),
                {
                    "id": "scale-month",
                    "installation_id": 32,
                    "repository": "lboverfys/BudgetConcurrencyContract",
                    "repository_key": "lboverfys/budgetconcurrencycontract",
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
                            "review_run_id": lease.review_run_id,
                            "installation_id": 32,
                            "repository": "lboverfys/BudgetConcurrencyContract",
                            "repository_key": "lboverfys/budgetconcurrencycontract",
                            "agent": "logic",
                            "purpose": "review",
                            "provider": "openai",
                            "model": "fixture-model",
                            "status": "settled",
                            "reserved_cost_microusd": 0,
                            "estimated_cost_microusd": 1,
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "duration_ms": number % 1000,
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
            aggregation = request_statistics_statement(
                ModelUsageRequestRecord.month_id == "scale-month", ("agent",), postgres=True,
            )
            group = connection.execute(aggregation).mappings().one()
            assert group["request_count"] == group["total_request_count"] == count
            assert group["estimated_cost_microusd"] == count
            assert (group["p50_duration_ms"], group["p95_duration_ms"]) == (499, 949)
            aggregate_sql = str(aggregation.compile(
                dialect=connection.dialect, compile_kwargs={"literal_binds": True}
            ))
            aggregate_plan = connection.execute(text(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + aggregate_sql
            )).scalar_one()[0]
            baseline = request_statistics_statement(
                ModelUsageRequestRecord.month_id == "scale-month", ("agent",),
            )
            assert dict(connection.execute(baseline).mappings().one()) == dict(group)
            baseline_sql = str(baseline.compile(dialect=connection.dialect, compile_kwargs={"literal_binds": True}))
            comparison = {"window_baseline": [], "ordered_set": []}
            # 交替顺序重复测量，保留全部执行计划，不设跨硬件的耗时门槛。
            for trial in range(6):
                order = (("window_baseline", baseline_sql), ("ordered_set", aggregate_sql))
                for label, sql in order[::1 if trial % 2 == 0 else -1]:
                    comparison[label].append(connection.execute(text(
                        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql
                    )).scalar_one()[0])
            insight_queries = []

            def capture_insight(_conn, _cursor, sql, parameters, *_args):
                if sql.lstrip().upper().startswith("SELECT"):
                    insight_queries.append((sql, parameters))

            event.listen(connection, "before_cursor_execute", capture_insight)
            try:
                with queries.sessions() as session:
                    report = collect_review_insights(session, ResourceScope.unrestricted_scope(),
                        now - timedelta(days=1), now + timedelta(days=1))
            finally:
                event.remove(connection, "before_cursor_execute", capture_insight)
            assert len(insight_queries) == 8
            assert report.requests.request_count == count
            assert report.completed_cost.priced_runs == 1
            assert report.completed_cost.mean_estimated_cost_microusd == count
            insight_plans = [connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, parameters
            ).scalar_one()[0] for sql, parameters in insight_queries]
            print(
                json.dumps(
                    {
                        "ledger_rows": count,
                        "page_size": 10,
                        "list_queries": len(statements),
                        "plan": plan,
                        "agent_aggregation_plan": aggregate_plan,
                        "aggregation_comparison": comparison,
                        "insights_query_count": len(insight_queries),
                        "insights_fixture": "one completed run; 100k requests; other fact tables empty",
                        "insights_plans": insight_plans,
                    }
                )
            )
        finally:
            transaction.rollback()
