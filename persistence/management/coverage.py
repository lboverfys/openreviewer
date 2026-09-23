"""覆盖校验及绑定本轮模型结果的人工范围确认。"""

from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domain.review_coverage import ReviewCoverage
from persistence.models import OutboxEventRecord, ReviewFilePlanRecord, ReviewPlanRecord


def acknowledgement_key(plan_id: str, completed_at: datetime) -> str:
    completed = completed_at.replace(tzinfo=UTC) if completed_at.tzinfo is None else completed_at.astimezone(UTC)
    digest = sha256(f"{plan_id}:{completed.isoformat()}".encode()).hexdigest()
    return f"review.coverage.acknowledged:{digest}"


def exclusions_acknowledged(session: Session, plan_id: str | None, completed_at: datetime | None) -> bool:
    if plan_id is None or completed_at is None:
        return False
    return session.scalar(select(OutboxEventRecord.id).where(
        OutboxEventRecord.event_key == acknowledgement_key(plan_id, completed_at),
    )) is not None


def load_coverage(session: Session, run_id: str, coverage_status: str, *, acknowledge_exclusions: bool = False) -> ReviewCoverage:
    if coverage_status != "partial":
        return ReviewCoverage(coverage_status, model_completed=False)
    rows = session.execute(select(
        ReviewPlanRecord.id, ReviewPlanRecord.model_review_completed_at, ReviewPlanRecord.rules_complete,
        ReviewFilePlanRecord.decision, func.count(ReviewFilePlanRecord.id).label("file_count"),
    ).outerjoin(ReviewFilePlanRecord, ReviewFilePlanRecord.review_plan_id == ReviewPlanRecord.id)
        .where(ReviewPlanRecord.review_run_id == run_id)
        .group_by(ReviewPlanRecord.id, ReviewPlanRecord.model_review_completed_at,
                  ReviewPlanRecord.rules_complete, ReviewFilePlanRecord.decision).limit(16)).all()
    if not rows:
        return ReviewCoverage(coverage_status, model_completed=False)
    plan = rows[0]
    return ReviewCoverage(
        coverage_status, model_completed=plan.model_review_completed_at is not None,
        rules_complete=plan.rules_complete,
        file_decisions={row.decision: row.file_count for row in rows if row.decision is not None},
        exclusions_acknowledged=acknowledge_exclusions or exclusions_acknowledged(session, plan.id, plan.model_review_completed_at),
    )
