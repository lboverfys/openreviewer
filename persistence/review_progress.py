"""按任务索引汇总批次；日志正文只保留各类事件的最近状态。"""

from datetime import UTC
from typing import cast

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from domain.review_progress import BatchProgress
from domain.security import redact_sensitive
from persistence.models import ModelReviewBatchRecord, OutboxEventRecord
from services.review_management import StoredReviewEvent


def load_batch_progress(session: Session, plan_id: str | None) -> dict[str, BatchProgress]:
    if plan_id is None:
        return {}
    batch = ModelReviewBatchRecord
    succeeded = batch.status == "succeeded"
    usage = batch.result["usage"]
    # 使用现有 (review_plan_id, agent, batch_number) 索引；聚合最多四个角色。
    rows = session.execute(select(
        batch.agent,
        func.count().label("total"),
        func.sum(case((succeeded, 1), else_=0)).label("completed"),
        func.sum(case((batch.status == "failed", 1), else_=0)).label("failed"),
        func.sum(case((batch.status == "running", 1), else_=0)).label("running"),
        *(func.sum(case((succeeded, usage[name].as_integer()))).label(label)
          for name, label in (("input_tokens", "input_tokens"),
                              ("output_tokens", "output_tokens"),
                              ("reasoning_output_tokens", "reasoning_tokens"))),
        func.sum(case((succeeded, batch.duration_ms))).label("duration_ms"),
    ).where(batch.review_plan_id == plan_id).group_by(batch.agent).limit(5)).mappings()
    return {row["agent"]: BatchProgress(**{key: row[key] for key in BatchProgress.model_fields})
            for row in rows}


def load_progress_events(session: Session, run_id: str) -> tuple[StoredReviewEvent, ...]:
    event = OutboxEventRecord
    # 仅对一条任务的事件索引排序；先取ID，再读少量正文，不把历史JSON搬入应用。
    ranked = select(event.id, func.row_number().over(
        partition_by=(event.event_type, event.payload["agent"].as_string()),
        order_by=(event.occurred_at.desc(), event.id.desc()),
    ).label("position")).where(
        event.aggregate_type == "review_run", event.aggregate_id == run_id,
    ).subquery()
    rows = session.execute(select(event.id, event.event_type, event.payload, event.occurred_at)
        .join(ranked, ranked.c.id == event.id).where(ranked.c.position == 1)
        .order_by(event.occurred_at.desc(), event.id.desc()).limit(128)).all()
    return tuple(StoredReviewEvent(id=row.id, event_type=row.event_type,
        payload=cast(dict[str, object], redact_sensitive(row.payload)),
        occurred_at=row.occurred_at.replace(tzinfo=UTC) if row.occurred_at.tzinfo is None else row.occurred_at)
        for row in reversed(rows))
