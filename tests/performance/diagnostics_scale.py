"""多仓库、跨月和倾斜分布的真实关联表规模；仅用显式隔离 PostgreSQL。"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import event, insert, select, text
from sqlalchemy.orm import sessionmaker

from persistence.models import (
    CodeIndexRecord,
    ModelReviewBatchRecord,
    ModelUsageRequestRecord,
    RetrievalTraceRecord,
    ReviewPlanRecord,
)
from persistence.review_insights import collect_review_insights
from services.rbac import ResourceScope
from tests.evaluation_support import seed_evaluation_runs
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)

NOW = datetime(2026, 9, 21, tzinfo=UTC)
REPOSITORIES = ("scale/hot", "scale/normal", "scale/small", "scale/private")


def repository_for(number):
    return REPOSITORIES[
        0 if number < 700 else 1 if number < 900 else 2 if number < 975 else 3
    ]


def test_diagnostics_multi_repository_related_facts(postgres_database):
    with postgres_database.engine.connect() as connection:
        transaction = connection.begin()
        try:
            sessions = sessionmaker(
                bind=connection, join_transaction_mode="create_savepoint"
            )
            database = SimpleNamespace(sessions=sessions)
            runs = {}
            for start in range(0, 1000, 100):
                runs.update(
                    seed_evaluation_runs(
                        database,
                        [
                            {
                                "label": str(n),
                                "pr": n + 1,
                                "repository": repository_for(n),
                                "repository_id": 100
                                + REPOSITORIES.index(repository_for(n)),
                                "created_at": NOW
                                if n % 10
                                else NOW - timedelta(days=60),
                                "completed_at": NOW
                                if n % 10
                                else NOW - timedelta(days=60),
                                "coverage": "complete" if n % 4 != 3 else "partial",
                                "batch_count": 4,
                                "evidence_reason": "source_blob_unavailable",
                                "findings": [f"合成问题 {i}" for i in range(12)],
                            }
                            for n in range(start, start + 100)
                        ],
                    )
                )
            with sessions() as session:
                rows = session.execute(
                    select(ReviewPlanRecord.id, ReviewPlanRecord.review_run_id).where(
                        ReviewPlanRecord.review_run_id.in_(runs.values())
                    )
                ).all()
            plans = {row.review_run_id: row.id for row in rows}
            expected = {
                repo: {
                    "requests": 0,
                    "runs": 0,
                    "priced": 0,
                    "incomplete": 0,
                    "batches": 0,
                    "findings": 0,
                }
                for repo in REPOSITORIES
            }
            for start in range(0, 1000, 10):
                requests, batches, traces = [], [], []
                for n in range(start, start + 10):
                    run_id, repo = runs[str(n)], repository_for(n)
                    recent = n % 10 != 0
                    stamp = NOW if recent else NOW - timedelta(days=60)
                    complete = n % 4 != 3
                    incomplete = n % 5 in (1, 2)
                    for j in range(100):
                        uncertain = incomplete and j == 99
                        at = stamp - timedelta(days=32) if j == 0 else stamp
                        requests.append(
                            {
                                "id": f"multi-{n}-{j}",
                                "month_id": at.strftime("%Y-%m"),
                                "review_run_id": run_id,
                                "installation_id": 10,
                                "repository": repo,
                                "repository_key": repo,
                                "agent": (
                                    "security",
                                    "logic",
                                    "convention",
                                    "retrieval",
                                )[j % 4],
                                "purpose": "review",
                                "provider": "openai",
                                "model": f"fixture-{n % 137}",
                                "status": ("reserved" if n % 5 == 1 else "uncertain")
                                if uncertain
                                else "settled",
                                "reserved_cost_microusd": 20,
                                "estimated_cost_microusd": None if uncertain else 10,
                                "duration_ms": None if uncertain else (n + j) % 1000,
                                "response_status": None if uncertain else 200,
                                "created_at": at,
                            }
                        )
                    for j in range(2, 5):
                        batches.append(
                            {
                                "id": f"multi-batch-{n}-{j}",
                                "review_plan_id": plans[run_id],
                                "agent": "logic",
                                "batch_number": j,
                                "batch_count": 4,
                                "unit_keys": [],
                                "attempt_count": j - 1,
                                "status": "succeeded" if j < 4 else "failed",
                                "result": {
                                    "reused_from_run_id": "older",
                                    "reused_input_tokens": 42,
                                }
                                if j == 2
                                else {},
                                "error_code": "model_timeout" if j == 4 else None,
                                "updated_at": stamp,
                            }
                        )
                    for j in range(3):
                        traces.append(
                            {
                                "id": f"multi-trace-{n}-{j}",
                                "index_id": f"multi-index-{REPOSITORIES.index(repo)}",
                                "review_run_id": run_id,
                                "agent": ("security", "logic", "convention")[j],
                                "payload": {
                                    "query_cache_hit": j == 0,
                                    "rerank_cache_hit": j == 1,
                                },
                                "created_at": stamp,
                            }
                        )
                    if recent:
                        bucket = expected[repo]
                        bucket["requests"] += 99
                        bucket["batches"] += 4
                        bucket["findings"] += 12
                        if complete:
                            bucket["runs"] += 1
                            bucket["incomplete" if incomplete else "priced"] += 1
                if start == 0:
                    connection.execute(
                        insert(CodeIndexRecord),
                        [
                            {
                                "id": f"multi-index-{i}",
                                "installation_id": 10,
                                "repository_id": 100 + i,
                                "repository": repo,
                                "head_sha": "a" * 40,
                                "configuration_key": "fixture",
                                "embedding_model": "fixture",
                                "status": "ready",
                                "parsed_files": 2,
                                "reused_files": 3,
                                "embedded_count": 5,
                                "reused_count": 7,
                                "created_at": NOW,
                                "completed_at": NOW,
                            }
                            for i, repo in enumerate(REPOSITORIES)
                        ],
                    )
                connection.execute(insert(ModelUsageRequestRecord), requests)
                connection.execute(insert(ModelReviewBatchRecord), batches)
                connection.execute(insert(RetrievalTraceRecord), traces)
            for table in (
                "review_runs",
                "review_plans",
                "model_usage_requests",
                "model_review_batches",
                "review_findings",
                "retrieval_traces",
                "code_indexes",
            ):
                connection.execute(text("ANALYZE " + table))
            evidence = []
            for repositories in (("scale/hot",), ("scale/normal", "scale/small"), ()):
                statements = []

                def capture(
                    _conn, _cursor, sql, parameters, *_args, statements=statements
                ):
                    if sql.lstrip().upper().startswith("SELECT"):
                        statements.append((sql, parameters))

                event.listen(connection, "before_cursor_execute", capture)
                try:
                    with sessions() as session:
                        result = collect_review_insights(
                            session,
                            ResourceScope(repositories=frozenset(repositories)),
                            NOW - timedelta(days=7),
                            NOW + timedelta(days=1),
                        )
                finally:
                    event.remove(connection, "before_cursor_execute", capture)
                totals = {
                    key: sum(expected[repo][key] for repo in repositories)
                    for key in expected[REPOSITORIES[0]]
                }
                assert len(statements) == 8
                assert result.requests.request_count == totals["requests"]
                assert result.completed_cost.completed_runs == totals["runs"]
                assert result.completed_cost.priced_runs == totals["priced"]
                assert (
                    result.completed_cost.incomplete_cost_runs == totals["incomplete"]
                )
                assert result.completed_cost.mean_estimated_cost_microusd == (
                    1000 if totals["priced"] else None
                )
                assert result.batches.total == totals["batches"]
                assert result.batches.reused_batches == totals["batches"] // 4
                assert (
                    result.batches.estimated_avoided_input_tokens
                    == totals["batches"] // 4 * 42
                )
                assert result.evidence.infrastructure == totals["findings"]
                assert result.retrieval_cache.groups == totals["batches"] // 4 * 3
                assert result.index_reuse.indexes == len(repositories)
                plans_report = [
                    connection.exec_driver_sql(
                        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, parameters
                    ).scalar_one()[0]
                    for sql, parameters in statements
                ]
                evidence.append(
                    {
                        "scope": repositories,
                        "result": result.model_dump(mode="json"),
                        "plans": plans_report,
                    }
                )
            print(
                json.dumps(
                    {
                        "runs": 1000,
                        "requests": 100000,
                        "batches": 4000,
                        "findings": 12000,
                        "traces": 3000,
                        "repositories": 4,
                        "largest_repository_fraction": 0.7,
                        "seed_batch_requests": 1000,
                        "cross_month": True,
                        "expired_data_fraction": 0.1,
                        "queries_per_scope": 8,
                        "scopes": evidence,
                    }
                )
            )
        finally:
            transaction.rollback()
