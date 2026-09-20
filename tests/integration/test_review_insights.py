"""费用和诊断口径的有界 SQL 行为；数据是合成账本，不调用外部模型。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, true

from domain.evidence_metrics import EVIDENCE_REASON_GROUPS, evidence_category
from domain.platform import PlatformNotFoundError
from persistence.models import (
    CodeIndexRecord,
    ModelReviewBatchRecord,
    ModelUsageRequestRecord,
    RetrievalTraceRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
)
from persistence.review_insights import collect_review_insights
from persistence.usage_queries import UsageQueries
from persistence.usage_statistics import request_statistics_statement
from services.rbac import ResourceScope
from tests.integration.test_finding_pagination import _seed_review
from tests.integration.test_management_api import database as database
from tests.integration.test_team_platform import request, settle, setup_ledger

ALL = ResourceScope.unrestricted_scope()
NOW = datetime(2026, 9, 21, tzinfo=UTC)


def usage(identity, run_id, *, cost=10, status="settled", duration=100, **values):
    return ModelUsageRequestRecord(id=identity, month_id="synthetic-month",
        review_run_id=run_id, installation_id=10, repository="lboverfys/NiuMa",
        repository_key="lboverfys/niuma", agent="logic", purpose="review",
        provider="openai", model="fixture", status=status,
        reserved_cost_microusd=50, estimated_cost_microusd=cost,
        duration_ms=duration, created_at=NOW, **values)


def insights(database, scope=ALL):
    with database.sessions() as session:
        return collect_review_insights(session, scope, NOW - timedelta(days=1), NOW + timedelta(days=1))


def test_request_statistics_keep_unknowns_http_and_same_cohort(database):
    with database.sessions() as session:
        session.add_all([
            usage("a", "run", response_status=200),
            usage("b", "run", cost=30, duration=300, response_status=500),
            usage("c", "run", cost=None, status="uncertain", duration=500),
            usage("d", "run", cost=None, status="reserved", duration=None),
        ])
        session.commit()
        row = session.execute(request_statistics_statement(true())).mappings().one()
    assert (row["request_count"], row["known_count"], row["unknown_count"]) == (4, 2, 2)
    assert (row["settled_count"], row["uncertain_count"], row["reserved_count"]) == (2, 1, 1)
    assert (row["http_2xx_count"], row["http_non_2xx_count"], row["http_unknown_count"]) == (1, 1, 2)
    assert (row["duration_sample_count"], row["p50_duration_ms"], row["p95_duration_ms"]) == (3, 300, 500)
    assert (row["settled_priced_count"], row["settled_reservation_microusd"], row["settled_cost_microusd"]) == (2, 100, 40)


def test_agent_groups_reconcile_and_model_truncation_keeps_full_totals(database):
    _, lease, _, ledger = setup_ledger(database)
    permit = ledger.reserve(lease, "security", request())
    settle(ledger, permit)
    queries = UsageQueries(database.sessions)
    month = queries.months(ALL, "2026-09").items[0]
    with database.sessions() as session:
        for number in range(102):
            row = usage(f"group-{number}", lease.review_run_id)
            row.month_id, row.model = month.id, f"model-{number}"
            session.add(row)
        session.commit()
    groups = queries.breakdown(ALL, month.id)
    agents = queries.breakdown(ALL, month.id, "agent")
    assert len(groups) == 100 and all(item.groups_truncated for item in groups)
    assert groups[0].total_request_count == 103
    assert groups[0].total_estimated_cost_microusd == 1040
    assert sum(item.known_cost_share for item in groups) < 1
    assert {item.agent for item in agents} == {"logic", "security"}
    assert sum(item.estimated_cost_microusd for item in agents) == 1040
    assert sum(item.known_cost_share for item in agents) == pytest.approx(1)
    with pytest.raises(PlatformNotFoundError):
        queries.breakdown(ResourceScope.deny_all(), month.id)


def test_completed_cost_includes_cross_month_and_excludes_incomplete_runs(database):
    runs = [_seed_review(database, finding_count=i, finding_id_prefix=f"{i}-") for i in range(4)]
    with database.sessions() as session:
        for number, run_id in enumerate(runs):
            run = session.get(ReviewRunRecord, run_id)
            run.created_at = NOW - timedelta(days=31)
            plan = session.scalar(select(ReviewPlanRecord).where(ReviewPlanRecord.review_run_id == run_id))
            plan.model_review_completed_at = NOW
            if number == 3:
                run.coverage_status = "partial"
        early = usage("early", runs[0], cost=70)
        early.created_at, early.month_id = NOW - timedelta(days=30), "previous-month"
        session.add_all([early, usage("late", runs[0], cost=30),
            usage("unknown", runs[1], cost=None), usage("failed", runs[3], cost=500)])
        session.commit()
    cost = insights(database).completed_cost
    assert (cost.completed_runs, cost.priced_runs, cost.incomplete_cost_runs, cost.missing_ledger_runs) == (3, 1, 1, 1)
    assert cost.mean_estimated_cost_microusd == 100
    assert cost.mean_turnaround_ms == pytest.approx(31 * 86_400_000)
    denied = insights(database, ResourceScope.deny_all())
    assert denied.completed_cost.completed_runs == denied.requests.request_count == 0
    assert denied.completed_cost.mean_estimated_cost_microusd is None


def test_evidence_batch_cache_and_index_metrics_are_distinct_and_bounded(database):
    reasons = [("verified" if category == "matched" else "unverified", reason, category)
        for category, values in EVIDENCE_REASON_GROUPS.items() for reason in sorted(values)]
    reasons += [("verified", "future_reason", "unclassified"), ("unverified", "future_reason", "unclassified")]
    assert evidence_category(None, None) == "unclassified"
    run_id = _seed_review(database, finding_count=len(reasons))
    with database.sessions() as session:
        findings = session.scalars(select(ReviewFindingRecord).order_by(ReviewFindingRecord.id)).all()
        plan_id = findings[0].review_plan_id
        for finding, (status, reason, category) in zip(findings, reasons, strict=True):
            finding.created_at = NOW
            finding.evidence_verification_status, finding.evidence_verification_reason = status, reason
            assert evidence_category(status, reason) == category
        for number, (attempts, status, result) in enumerate([
            (1, "succeeded", {}), (2, "succeeded", {}), (2, "failed", {}),
            (1, "succeeded", {"reused_from_run_id": "old", "reused_input_tokens": 42}),
            (1, "succeeded", {"reused_from_run_id": "old"}),
        ], start=1):
            session.add(ModelReviewBatchRecord(id=f"batch-{number}", review_plan_id=plan_id,
                batch_number=number, batch_count=5, unit_keys=[], attempt_count=attempts,
                status=status, result=result, updated_at=NOW))
        session.add(CodeIndexRecord(id="index", installation_id=10, repository_id=42,
            repository="lboverfys/NiuMa", head_sha="a" * 40, configuration_key="key",
            embedding_model="fixture", status="ready", parsed_files=2, reused_files=3,
            embedded_count=5, reused_count=7, created_at=NOW, completed_at=NOW))
        for number, payload in enumerate([{"query_cache_hit": True, "rerank_cache_hit": False}, {}, {"query_cache_hit": False}]):
            session.add(RetrievalTraceRecord(id=f"trace-{number}", index_id="index",
                review_run_id=run_id, agent=f"agent-{number}", payload=payload, created_at=NOW))
        session.commit()
    statements = []

    def capture(_conn, _cursor, sql, *_args):
        if sql.lstrip().upper().startswith("SELECT"):
            statements.append(sql)

    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        report = insights(database)
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)
    assert len(statements) == 8
    assert (report.batches.claimed, report.batches.reclaimed, report.batches.single_claim_succeeded) == (3, 2, 1)
    assert (report.batches.reused_batches, report.batches.estimated_avoided_input_tokens, report.batches.reused_input_unknown_batches) == (2, 42, 1)
    assert (report.evidence.matched, report.evidence.unmatched, report.evidence.unclassified) == (2, 3, 2)
    assert report.evidence.automatic_coverage == pytest.approx(5 / len(reasons))
    assert report.evidence.eligible_pass_rate == pytest.approx(2 / 5)
    assert (report.retrieval_cache.groups, report.retrieval_cache.query_recorded_groups, report.retrieval_cache.query_all_hit_groups) == (3, 2, 1)
    assert report.index_reuse.model_dump() == {"indexes": 1, "parsed_files": 2, "reused_files": 3, "embedded_vectors": 5, "reused_vectors": 7}
    assert insights(database, ResourceScope.deny_all()).evidence.total == 0
