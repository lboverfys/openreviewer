"""审查管理 common 存储职责。"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.security import redact_sensitive
from persistence.models import OutboxEventRecord
from services.review_management import StoredReviewEvent

_PUBLISH_RECOVERY_AFTER = timedelta(minutes=5)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _required_utc(value: datetime | None, field_name: str) -> datetime:
    normalized = _as_utc(value)
    if normalized is None:
        raise ValueError(f"{field_name} must not be null")
    return normalized


def _review_change_token(
    run_updated_at: datetime | None,
    task_updated_at: datetime | None,
    latest_event_id: str | None,
) -> str:
    raw = "|".join(
        (
            _required_utc(
                run_updated_at,
                "review_run.updated_at",
            ).isoformat(timespec="microseconds"),
            _required_utc(
                task_updated_at,
                "review_task.updated_at",
            ).isoformat(timespec="microseconds"),
            latest_event_id or "",
        )
    )
    return sha256(raw.encode("utf-8")).hexdigest()[:24]


def _safe_payload(value: object) -> object:
    """递归脱敏事件 JSON，保证日志面板不会回显凭据。"""

    return redact_sensitive(value)


def _latest_summary_failed(session: Session, review_run_id: str) -> bool:
    """读取最近一次汇总终态，判断汇总节点是否仍可人工重试。"""

    row = session.execute(
        select(OutboxEventRecord.event_type, OutboxEventRecord.payload)
        .where(
            OutboxEventRecord.aggregate_type == "review_run",
            OutboxEventRecord.aggregate_id == review_run_id,
            OutboxEventRecord.event_type.in_(
                (
                    "review.model.summary_completed",
                    "review.model.summary_skipped",
                )
            ),
        )
        .order_by(
            OutboxEventRecord.occurred_at.desc(),
            OutboxEventRecord.id.desc(),
        )
        .limit(1)
    ).one_or_none()
    if row is None:
        return False
    event_type, payload = row
    return (
        event_type == "review.model.summary_completed"
        and isinstance(payload, dict)
        and payload.get("agent_status") == "failed"
    )


def _project_agent_progress(
    events: tuple[StoredReviewEvent, ...],
    *,
    coverage_status: str,
    model_completed: bool,
) -> tuple[
    dict[str, str],
    dict[str, dict[str, object]],
    str,
    str,
    bool,
    tuple[str, ...],
    tuple[dict[str, object], ...],
]:
    """从已加载事件在内存中投影固定 Agent 的可公开状态。

    详情读取只额外扫描一次有界事件列表，不在 Agent/批次循环中查询数据库；
    因此查询次数仍为 O(1)，而投影成本为 O(事件数)。
    """

    statuses: dict[str, str] = {
        "security": "waiting",
        "convention": "waiting",
        "logic": "waiting",
        "summary": "not_executed",
    }
    summaries: dict[str, dict[str, object]] = {}
    failed_batches: dict[tuple[str, int], dict[str, object]] = {}
    aggregation_status = "not_started"
    summary_status = "not_executed"
    partial_result = coverage_status == "partial"
    failed_agents: set[str] = set()
    # 人工重试会把模型次数归零；先隔开旧窗口，不能让旧的第 3 次压过新的第 1 次。
    reset_at = max(
        (event.occurred_at for event in events if event.event_type == "review.manual.retry"),
        default=None,
    )
    if reset_at is not None:
        events = tuple(event for event in events if event.occurred_at >= reset_at)
    # 新版事件会把模型代次写入 payload；旧版事件可能没有该字段。只要
    # 事件集中出现了任一明确代次，就把缺少代次的旧事件视为历史数据并
    # 忽略，避免第一次重试时旧的 completed/failed 状态覆盖当前代次。
    # 只有整组事件都没有代次信息时，才按旧版兼容策略全部纳入。
    attempts = [
        value
        for event in events
        for value in (event.payload.get("model_attempt_count"),)
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    latest_attempt = max(attempts) if attempts else None
    for event in events:
        if not event.event_type.startswith("review.model."):
            continue
        payload = event.payload
        event_attempt = payload.get("model_attempt_count")
        if latest_attempt is not None:
            if not isinstance(event_attempt, int) or isinstance(event_attempt, bool):
                continue
            if event_attempt != latest_attempt:
                continue
        raw_agent = payload.get("agent")
        agent = raw_agent if isinstance(raw_agent, str) else None
        if event.event_type.endswith("workflow_partial"):
            partial_result = True
            values = payload.get("failed_agents")
            if isinstance(values, list):
                failed_agents.update(
                    value for value in values if isinstance(value, str)
                )
            continue
        if event.event_type.endswith("aggregation_completed"):
            aggregation_status = str(payload.get("aggregation_status") or "completed")
            continue
        if event.event_type.endswith("summary_skipped"):
            summary_status = "skipped"
            statuses["summary"] = "not_executed"
            continue
        if event.event_type.endswith("summary_completed") or event.event_type.endswith(
            "summary_failed"
        ):
            raw_status = payload.get("agent_status")
            summary_status = "completed" if raw_status == "completed" else "failed"
            statuses["summary"] = summary_status
            if summary_status == "failed":
                failed_agents.add("summary")
            summaries["summary"] = {
                key: payload[key]
                for key in ("verdict", "summary", "checked_areas", "finding_count")
                if key in payload
            }
            continue
        if agent is None or agent == "summary":
            continue
        if event.event_type.endswith("agent_started"):
            statuses[agent] = "preparing"
            failed_agents.discard(agent)
        elif event.event_type.endswith("agent_completed"):
            statuses[agent] = (
                "not_applicable"
                if payload.get("status") == "not_applicable"
                else "completed"
            )
            summaries[agent] = {
                key: payload[key]
                for key in ("verdict", "summary", "checked_areas", "finding_count")
                if key in payload
            }
        elif event.event_type.endswith("agent_not_applicable"):
            statuses[agent] = "not_applicable"
            failed_agents.discard(agent)
        elif event.event_type.endswith("agent_failed"):
            if payload.get("status") == "disabled":
                statuses[agent] = "disabled"
                failed_agents.discard(agent)
            else:
                statuses[agent] = "failed"
                failed_agents.add(agent)
        elif event.event_type.endswith("batch_failed"):
            statuses[agent] = "partial"
            failed_agents.add(agent)
            number = payload.get("batch_number")
            if isinstance(number, int):
                failed_batches[(agent, number)] = {
                    "agent": agent,
                    "batch_number": number,
                    "error_code": payload.get("error_code"),
                    "error_message": payload.get("error_message"),
                    "retryable": payload.get("error_retryable"),
                }
        elif event.event_type.endswith("request_started") or event.event_type.endswith(
            "batch_started"
        ):
            if statuses.get(agent) not in {"completed", "failed"}:
                statuses[agent] = "running"
    if summary_status == "not_executed" and model_completed:
        summary_status = "skipped"
    if aggregation_status == "not_started" and model_completed:
        aggregation_status = "completed"
    return (
        statuses,
        summaries,
        aggregation_status,
        summary_status,
        partial_result,
        tuple(sorted(failed_agents)),
        tuple(failed_batches[key] for key in sorted(failed_batches)),
    )


class FindingDecisionError(LookupError):
    """内部转换用的 Finding 不存在异常。"""
