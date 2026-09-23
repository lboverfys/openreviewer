"""在人工动作事务中按计划检查覆盖；与详情使用同一原因规则。"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domain.review_coverage import coverage_block_reason
from persistence.models import ReviewFilePlanRecord, ReviewPlanRecord


def load_coverage_block_reason(session: Session, run_id: str, coverage_status: str) -> str | None:
    if coverage_status != "partial":
        return coverage_block_reason(coverage_status, model_completed=False)
    excluded_count = select(func.count()).select_from(ReviewFilePlanRecord).where(
        ReviewFilePlanRecord.review_plan_id == ReviewPlanRecord.id,
        ReviewFilePlanRecord.decision != "planned",
    ).correlate(ReviewPlanRecord).scalar_subquery()
    plan = session.execute(select(
        ReviewPlanRecord.model_review_completed_at, ReviewPlanRecord.rules_complete,
        excluded_count.label("excluded_count"),
    ).where(ReviewPlanRecord.review_run_id == run_id)).one_or_none()
    return coverage_block_reason(
        coverage_status,
        model_completed=plan is not None and plan.model_review_completed_at is not None,
        excluded_file_count=plan.excluded_count if plan is not None else 0,
        rules_complete=plan.rules_complete if plan is not None else None,
    )
