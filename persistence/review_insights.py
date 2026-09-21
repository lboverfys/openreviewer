"""时间和仓库范围内的费用、批次、引用及复用聚合，不读取完整结果正文。"""

from sqlalchemy import BigInteger, and_, case, func, or_, select

from domain.evidence_metrics import EVIDENCE_REASON_GROUPS, evidence_category
from domain.platform import (
    BatchHealth,
    CompletedWorkflowCost,
    EvidenceHealth,
    EvidenceReasonCount,
    FailureDiagnostic,
    IndexReuseHealth,
    RequestStatistics,
    RetrievalCacheHealth,
    ReviewInsights,
)
from persistence.models import (
    CodeIndexRecord,
    ModelReviewBatchRecord,
    ModelUsageRequestRecord,
    RetrievalTraceRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
)
from persistence.resource_scope import resource_predicate
from persistence.usage_statistics import request_statistics_statement


def scoped(model, scope):
    return resource_predicate(scope, installation_column=model.installation_id, repository_column=model.repository,
        repository_key_column=model.repository_key if hasattr(model, "repository_key") else func.lower(model.repository))


def completed_cost_statement(scope, since, until, *, postgres: bool):
    run, plan, request = ReviewRunRecord, ReviewPlanRecord, ModelUsageRequestRecord
    cohort = select(run.id, run.created_at, plan.model_review_completed_at.label("completed_at")).join(plan, plan.review_run_id == run.id).where(
        scoped(run, scope), plan.model_review_completed_at >= since, plan.model_review_completed_at < until,
        run.coverage_status == "complete",
    ).subquery()
    costs = select(cohort.c.id, cohort.c.created_at, cohort.c.completed_at,
        func.count(request.id).label("requests"), func.sum(request.estimated_cost_microusd).label("cost"),
        func.count(request.id).filter(or_(request.estimated_cost_microusd.is_(None), request.status != "settled")).label("incomplete"),
    ).outerjoin(request, request.review_run_id == cohort.c.id).group_by(cohort.c.id, cohort.c.created_at, cohort.c.completed_at).subquery()
    priced = and_(costs.c.requests > 0, costs.c.incomplete == 0)
    elapsed = ((func.extract("epoch", costs.c.completed_at) - func.extract("epoch", costs.c.created_at)) * 1000
        if postgres else (func.julianday(costs.c.completed_at) - func.julianday(costs.c.created_at)) * 86_400_000)
    return select(func.count().label("completed_runs"), func.count().filter(priced).label("priced_runs"),
        func.count().filter(costs.c.requests == 0).label("missing_ledger_runs"),
        func.count().filter(costs.c.incomplete > 0).label("incomplete_cost_runs"),
        func.avg(costs.c.cost).filter(priced).label("mean_estimated_cost_microusd"),
        func.avg(elapsed).label("mean_turnaround_ms"),
    ).select_from(costs)


def batch_health_statement(scope, since, until):
    batch, plan, run = ModelReviewBatchRecord, ReviewPlanRecord, ReviewRunRecord
    reused = batch.result["reused_from_run_id"].as_string().is_not(None)
    claimed = and_(batch.attempt_count > 0, ~reused)
    statement = select(func.count().label("total"),
        *(func.count().filter(batch.status == value).label(value) for value in ("pending", "running", "succeeded", "failed")),
        func.count().filter(claimed).label("claimed"),
        func.count().filter(claimed, batch.attempt_count > 1).label("reclaimed"),
        func.count().filter(claimed, batch.status.in_(("succeeded", "failed"))).label("terminal_claimed"),
        func.count().filter(claimed, batch.status == "succeeded", batch.attempt_count == 1).label("single_claim_succeeded"),
        func.count().filter(reused, batch.status == "succeeded").label("reused_batches"),
        func.count().filter(reused, batch.status == "succeeded", batch.result["reused_input_tokens"].as_string().is_(None)).label("reused_input_unknown_batches"),
        func.coalesce(func.sum(case((and_(reused, batch.status == "succeeded"), batch.result["reused_input_tokens"].as_string().cast(BigInteger)), else_=0)), 0).label("estimated_avoided_input_tokens"),
    ).select_from(batch).join(plan, plan.id == batch.review_plan_id).join(run, run.id == plan.review_run_id).where(
        scoped(run, scope), batch.updated_at >= since, batch.updated_at < until,
    )
    return statement


def evidence_category_expression():
    finding = ReviewFindingRecord
    unchecked = or_(finding.evidence_verification_status == "unverified", finding.evidence_verification_status.is_(None))
    return case(
        (and_(finding.evidence_verification_status == "verified", finding.evidence_verification_reason.in_(EVIDENCE_REASON_GROUPS["matched"])), "matched"),
        (and_(finding.evidence_verification_status == "unverified", finding.evidence_verification_reason.in_(EVIDENCE_REASON_GROUPS["unmatched"])), "unmatched"),
        (and_(unchecked, finding.evidence_verification_reason.in_(EVIDENCE_REASON_GROUPS["infrastructure"])), "infrastructure"),
        (and_(unchecked, finding.evidence_verification_reason.in_(EVIDENCE_REASON_GROUPS["not_covered"])), "not_covered"),
        else_="unclassified",
    )


def collect_review_insights(session, scope, since, until) -> ReviewInsights:
    run, plan, batch, request, finding, trace, index = (
        ReviewRunRecord, ReviewPlanRecord, ModelReviewBatchRecord, ModelUsageRequestRecord,
        ReviewFindingRecord, RetrievalTraceRecord, CodeIndexRecord,
    )
    requests = session.execute(request_statistics_statement(and_(scoped(request, scope), request.created_at >= since, request.created_at < until), postgres=session.get_bind().dialect.name == "postgresql")).mappings().one()
    costs = session.execute(completed_cost_statement(scope, since, until, postgres=session.get_bind().dialect.name == "postgresql")).mappings().one()
    batches = session.execute(batch_health_statement(scope, since, until)).mappings().one()
    errors = session.execute(select(batch.error_code.label("code"), func.count().label("count"))
        .join(plan, plan.id == batch.review_plan_id).join(run, run.id == plan.review_run_id)
        .where(scoped(run, scope), batch.updated_at >= since, batch.updated_at < until, batch.error_code.is_not(None))
        .group_by(batch.error_code).order_by(func.count().desc(), batch.error_code).limit(51)).mappings().all()
    category = evidence_category_expression()
    evidence_filter = and_(scoped(run, scope), finding.created_at >= since, finding.created_at < until)
    evidence = dict(session.execute(select(func.count().label("total"),
        *(func.count().filter(category == name).label(name) for name in (*EVIDENCE_REASON_GROUPS, "unclassified")),
    ).select_from(finding).join(run, run.id == finding.review_run_id).where(evidence_filter)).mappings().one())
    reasons = session.execute(select(finding.evidence_verification_status.label("status"), finding.evidence_verification_reason.label("reason"), func.count().label("count"))
        .join(run, run.id == finding.review_run_id).where(evidence_filter)
        .group_by(finding.evidence_verification_status, finding.evidence_verification_reason)
        .order_by(func.count().desc(), finding.evidence_verification_reason, finding.evidence_verification_status).limit(51)).mappings().all()
    eligible = evidence["matched"] + evidence["unmatched"]
    evidence.update(automatic_coverage=eligible / evidence["total"] if evidence["total"] else None,
                    eligible_pass_rate=evidence["matched"] / eligible if eligible else None)
    cache = session.execute(select(func.count().label("groups"),
        func.count().filter(trace.payload["query_cache_hit"].as_boolean().is_not(None)).label("query_recorded_groups"),
        func.count().filter(trace.payload["query_cache_hit"].as_boolean().is_(True)).label("query_all_hit_groups"),
        func.count().filter(trace.payload["rerank_cache_hit"].as_boolean().is_not(None)).label("rerank_recorded_groups"),
        func.count().filter(trace.payload["rerank_cache_hit"].as_boolean().is_(True)).label("rerank_all_hit_groups"),
    ).select_from(trace).join(run, run.id == trace.review_run_id).where(scoped(run, scope), trace.agent.is_not(None),
        trace.created_at >= since, trace.created_at < until)).mappings().one()
    indexes = session.execute(select(func.count().label("indexes"),
        *(func.coalesce(func.sum(getattr(index, field)), 0).label(name) for field, name in (
            ("parsed_files", "parsed_files"), ("reused_files", "reused_files"),
            ("embedded_count", "embedded_vectors"), ("reused_count", "reused_vectors"))),
    ).select_from(index).where(scoped(index, scope), index.created_at >= since, index.created_at < until,
        index.status == "ready", index.completed_at.is_not(None))).mappings().one()
    return ReviewInsights(requests=RequestStatistics.model_validate(requests),
        completed_cost=CompletedWorkflowCost.model_validate(costs), batches=BatchHealth.model_validate(batches),
        batch_errors=tuple(FailureDiagnostic.model_validate(row) for row in errors[:50]), batch_errors_truncated=len(errors) > 50,
        evidence=EvidenceHealth.model_validate(evidence),
        evidence_reasons=tuple(EvidenceReasonCount.model_validate({**row,"category":evidence_category(row["status"],row["reason"])}) for row in reasons[:50]),
        evidence_reasons_truncated=len(reasons) > 50, retrieval_cache=RetrievalCacheHealth.model_validate(cache), index_reuse=IndexReuseHealth.model_validate(indexes))
