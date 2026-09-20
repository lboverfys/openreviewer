"""授权范围内读取调用证据元数据，不通过日常接口返回供应商正文。"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domain.evaluation_workbench import EvaluationNotFoundError, EvaluationOutputView
from domain.pagination import CursorPage, encode_cursor
from persistence.models import EvaluationModelOutputRecord as Output
from persistence.models import ModelUsageRequestRecord as Request
from persistence.models import ReviewRunRecord
from persistence.pagination import apply_cursor
from persistence.resource_scope import resource_predicate
from services.rbac import ResourceScope


def output_summary_query(run_ids: tuple[str, ...], now: datetime):
    complete = Output.status.in_(("captured", "parse_failed")) & (Output.expires_at > now) & Output.output_text.is_not(None)
    return select(
        Request.review_run_id,
        func.count(Request.id).label("request_count"),
        func.count(Output.id).filter(complete).label("captured_count"),
        func.min(Output.expires_at).label("expires_at"),
    ).outerjoin(Output, Output.id == Request.id).where(
        Request.review_run_id.in_(run_ids), Request.purpose == "review",
    ).group_by(Request.review_run_id).subquery()


def output_scope(scope: ResourceScope):
    return resource_predicate(scope, installation_column=Output.installation_id,
        repository_column=Output.repository, repository_key_column=Output.repository_key)


def list_output_evidence(session: Session, run_id: str, scope: ResourceScope, *, limit: int = 10, cursor: str | None = None):
    visible = session.scalar(select(ReviewRunRecord.id).where(ReviewRunRecord.id == run_id,
        resource_predicate(scope, installation_column=ReviewRunRecord.installation_id,
            repository_column=ReviewRunRecord.repository, repository_key_column=ReviewRunRecord.repository_key)))
    if visible is None and session.scalar(select(Output.id).where(Output.review_run_id == run_id, output_scope(scope)).limit(1)) is None:
        raise EvaluationNotFoundError("审查调用证据不存在或无权访问")
    statement = select(*(getattr(Output, key) for key in EvaluationOutputView.model_fields)).where(
        Output.review_run_id == run_id, output_scope(scope),
    )
    rows = session.execute(apply_cursor(statement, Output.created_at, Output.id, cursor).limit(limit + 1)).mappings().all()
    now = datetime.now(UTC)
    items = []
    for row in rows[:limit]:
        expires = row["expires_at"].replace(tzinfo=UTC) if row["expires_at"].tzinfo is None else row["expires_at"]
        items.append(EvaluationOutputView.model_validate({**row, "status":"expired" if expires <= now else row["status"]}))
    return CursorPage(items=tuple(items), next_cursor=(
        encode_cursor(items[-1].created_at, items[-1].id) if len(rows) > limit else None
    ))
