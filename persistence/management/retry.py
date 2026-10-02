"""审查管理 retry 存储职责。"""

from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from domain.enums import ExecutionStatus, ModelBatchStatus, ReviewAgent
from persistence.management.common import _as_utc, _latest_summary_failed, _required_utc
from persistence.management.retry_configuration import retry_impacts
from persistence.models import (
    ModelCallRecord,
    ModelReviewBatchRecord,
    OutboxEventRecord,
    ReviewFilePlanRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewPlanRuleRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
    ReviewUnitRecord,
)
from services.review_management import ReviewActionConflictError


def _prepare_stage_retry(
    session: Session,
    run: ReviewRunRecord,
    task: ReviewTaskRecord,
    plan: ReviewPlanRecord | None,
    target_stage: ExecutionStatus,
) -> None:
    """清除目标阶段及其后续产物，同时保留可安全复用的前序结果。"""

    if plan is None:
        raise ReviewActionConflictError("指定阶段重审需要已有审查计划")

    plan_id = plan.id
    session.execute(
        delete(ReviewFindingRecord).where(ReviewFindingRecord.review_plan_id == plan_id)
    )
    session.execute(
        delete(ModelCallRecord).where(ModelCallRecord.review_plan_id == plan_id)
    )

    if target_stage in {ExecutionStatus.CI, ExecutionStatus.PLANNING}:
        # 规划是不可变快照。回到 CI 或规划时必须删除旧计划及全部子资源，
        # 让 Worker 基于当前 PR 快照重新生成，而不是误用旧计划。
        session.execute(
            delete(ModelReviewBatchRecord).where(
                ModelReviewBatchRecord.review_plan_id == plan_id
            )
        )
        session.execute(
            delete(ReviewFilePlanRecord).where(
                ReviewFilePlanRecord.review_plan_id == plan_id
            )
        )
        session.execute(
            delete(ReviewUnitRecord).where(ReviewUnitRecord.review_plan_id == plan_id)
        )
        session.execute(
            delete(ReviewPlanRuleRecord).where(
                ReviewPlanRuleRecord.review_plan_id == plan_id
            )
        )
        session.delete(plan)
        run.coverage_status = "unknown"
    elif target_stage is ExecutionStatus.AGENT_BATCHES:
        session.execute(
            delete(ModelReviewBatchRecord).where(
                ModelReviewBatchRecord.review_plan_id == plan_id
            )
        )
        plan.model_review_completed_at = None
    elif target_stage is ExecutionStatus.AGGREGATING:
        reusable_agents = tuple(
            agent.value
            for agent in (
                ReviewAgent.SECURITY,
                ReviewAgent.CONVENTION,
                ReviewAgent.LOGIC,
            )
        )
        session.execute(
            delete(ModelReviewBatchRecord).where(
                ModelReviewBatchRecord.review_plan_id == plan_id,
                ModelReviewBatchRecord.agent.not_in(reusable_agents),
            )
        )
        plan.model_review_completed_at = None
    else:
        raise ReviewActionConflictError("重审目标阶段无效")

    run.review_conclusion = None
    if target_stage is ExecutionStatus.CI:
        task.ci_poll_count = 0
        task.ci_wait_started_at = None
        task.ci_deadline_at = None


def _prepare_failed_node_retry(
    session: Session,
    run: ReviewRunRecord,
    task: ReviewTaskRecord,
    plan: ReviewPlanRecord | None,
    *,
    agent: str | None,
    batch_number: int | None,
    now: datetime,
) -> dict[str, object]:
    """按当前配置补跑失败节点，配置变化时重建该 Agent 的全部批次。

    配置一致时批量恢复未完成批次；配置改变时只重建目标 Agent，并使旧汇总
    失效。没有已持久化批次时也允许继续，让 Worker 首次规划失败的 Agent。
    """

    if plan is None:
        raise ReviewActionConflictError("失败节点重试需要已有审查计划")
    if run.coverage_status == "stale" and not run.snapshot_review:
        raise ReviewActionConflictError("该任务已被新提交替代")
    if task.execution_status == ExecutionStatus.RUNNING.value and task.lease_owner is not None and task.lease_expires_at is not None and _required_utc(task.lease_expires_at, "lease_expires_at") > _required_utc(now, "now"):
        raise ReviewActionConflictError("任务正在执行，请等待其完成后重试")
    summary_retry = agent == ReviewAgent.SUMMARY.value and batch_number is None
    summary_failed = _latest_summary_failed(session, run.id)
    if agent == ReviewAgent.SUMMARY.value and batch_number is not None:
        raise ReviewActionConflictError("汇总 Agent 不支持批次号")
    if summary_retry and not summary_failed:
        raise ReviewActionConflictError("汇总 Agent 当前没有可重试的失败节点")
    rows = list(
        session.scalars(
            select(ModelReviewBatchRecord)
            .where(ModelReviewBatchRecord.review_plan_id == plan.id)
            .order_by(
                ModelReviewBatchRecord.agent.asc(),
                ModelReviewBatchRecord.batch_number.asc(),
            )
            .limit(3001)
            .with_for_update()
        )
    )
    if len(rows) > 3000:
        raise ReviewActionConflictError("模型批次数量超过安全上限")
    selected = [
        row
        for row in rows
        if (agent is None or row.agent == agent)
        and (batch_number is None or row.batch_number == batch_number)
        and row.status != ModelBatchStatus.SUCCEEDED.value
    ]
    if batch_number is not None and not any(
        row.batch_number == batch_number and (agent is None or row.agent == agent)
        for row in rows
    ):
        raise ReviewActionConflictError("指定模型批次不存在")
    # 汇总 Agent 不建立普通批次行；它的失败状态由 summary_completed
    # 事件表达。只有明确存在该失败事件时，才允许在没有可更新批次的
    # 情况下继续，并把计划重新打开给 Worker 执行强制汇总。
    summary_only_retry = (
        (summary_failed or (bool(rows) and plan.model_review_completed_at is None))
        and batch_number is None
        and (agent is None or agent == ReviewAgent.SUMMARY.value)
        and not selected
    )
    ranked = select(OutboxEventRecord.event_type, OutboxEventRecord.payload,
        func.row_number().over(partition_by=OutboxEventRecord.payload["agent"].as_string(),
            order_by=(OutboxEventRecord.occurred_at.desc(), OutboxEventRecord.id.desc())).label("position")
    ).where(OutboxEventRecord.aggregate_id == run.id,
        OutboxEventRecord.event_type.in_(("review.model.agent_failed", "review.model.agent_completed",
            "review.model.agent_not_applicable", "review.model.agent_started"))).subquery()
    failed_agents = {payload["agent"] for event_type, payload in session.execute(
        select(ranked.c.event_type, ranked.c.payload).where(ranked.c.position == 1).limit(4))
        if event_type == "review.model.agent_failed" and payload.get("status") == "failed"
        and payload.get("model_attempt_count", task.model_attempt_count) == task.model_attempt_count
        and payload.get("agent") in {"security", "convention", "logic"}}
    target_agents = ({agent} if agent is not None else failed_agents | {row.agent for row in selected})
    agent_failure = bool(target_agents & failed_agents) and batch_number is None
    summary_only_retry = summary_only_retry and not agent_failure
    if not selected and rows and not summary_only_retry and not agent_failure:
        raise ReviewActionConflictError("指定节点没有可重试的失败批次")
    impacts = retry_impacts(session, plan.id, tuple(sorted(target_agents)))
    restarted_agents = {name for name, impact in impacts.items() if impact.restart}
    live_running = []
    now_utc = _as_utc(now)
    selected_row_ids = {row.id for row in selected}
    for row in rows:
        if row.id not in selected_row_ids and row.agent not in restarted_agents:
            continue
        lease_expires_at = row.lease_expires_at
        normalized_expires_at = (
            _as_utc(lease_expires_at) if lease_expires_at is not None else None
        )
        if (
            row.status == ModelBatchStatus.RUNNING.value
            and normalized_expires_at is not None
            and now_utc is not None
            and normalized_expires_at > now_utc
        ):
            live_running.append(row)
    if live_running:
        raise ReviewActionConflictError("指定批次正在执行，请等待其完成")
    if restarted_agents:
        _prepare_stage_retry(session, run, task, plan, ExecutionStatus.AGGREGATING)
        session.execute(delete(ModelReviewBatchRecord).where(
            ModelReviewBatchRecord.review_plan_id == plan.id,
            ModelReviewBatchRecord.agent.in_(restarted_agents),
        ).execution_options(synchronize_session=False))
    selected_ids = tuple(row.id for row in selected if row.agent not in restarted_agents)
    if selected_ids:
        session.execute(
            update(ModelReviewBatchRecord)
            .where(ModelReviewBatchRecord.id.in_(selected_ids))
            .values(
                status=ModelBatchStatus.PENDING.value,
                # 人工重试是一次新的批次尝试窗口。若保留自动重试已
                # 累积的计数，Worker 会在重新领取前立即判定“达到上限”，
                # 用户点击重试却仍然不会发出请求。截断拆分检查点位于
                # error_details 中，未被清除，因此成功子批次仍可复用。
                attempt_count=0,
                available_at=now,
                lease_owner=None,
                lease_expires_at=None,
                error_code=None,
                error_message=None,
                response_status=None,
                duration_ms=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
    # 上游变化后汇总输入已失效；前三路中不在本次恢复范围的成功批次保留。
    session.execute(delete(ModelReviewBatchRecord).where(
        ModelReviewBatchRecord.review_plan_id == plan.id,
        ModelReviewBatchRecord.agent == ReviewAgent.SUMMARY.value,
    ).execution_options(synchronize_session=False))
    plan.model_review_completed_at = None
    task.execution_status = ExecutionStatus.READY_FOR_REVIEW.value
    task.workflow_status = ExecutionStatus.AGENT_BATCHES.value
    task.workflow_paused_from = None
    task.available_at = now
    task.last_error = None
    task.last_error_code = None
    task.last_error_retryable = None
    task.last_error_details = None
    task.lease_owner = None
    task.lease_expires_at = None
    task.claimed_from_status = None
    task.updated_at = now
    run.execution_status = ExecutionStatus.READY_FOR_REVIEW.value
    run.workflow_status = ExecutionStatus.AGENT_BATCHES.value
    run.workflow_paused_from = None
    run.review_conclusion = None
    # 汇总失败不代表三路审查覆盖不完整；重试期间保留完整覆盖标记，
    # 只有确实存在失败/未完成批次时才显示部分覆盖。
    run.coverage_status = "complete" if summary_only_retry else "partial"
    run.updated_at = now
    previous_profile = (run.repository_policy or {}).get("review_profile_id")
    if previous_profile is not None:
        run.repository_policy = {**(run.repository_policy or {}), "review_profile_id": None}
    return {"configuration_source": "current", "previous_review_profile_id": previous_profile,
        "target_agents": sorted(target_agents),
        "restarted_agents": sorted(restarted_agents),
        "retry_impacts": {name: impact.model_dump(mode="json") for name, impact in impacts.items()}}


def _paused_execution_status(
    workflow_status: ExecutionStatus,
    current_execution: str,
) -> ExecutionStatus:
    """把暂停中的 DAG 节点映射到不会被旧 Worker 继续执行的队列状态。"""

    current = ExecutionStatus(current_execution)
    if workflow_status is ExecutionStatus.CI:
        # 上下文可能尚未读取，也可能已经进入 CI 轮询；保守地重新排队，
        # 继续时会重新校验 GitHub 当前快照。
        return (
            current
            if current
            in {
                ExecutionStatus.QUEUED,
                ExecutionStatus.WAITING_FOR_CI,
            }
            else ExecutionStatus.QUEUED
        )
    if workflow_status in {
        ExecutionStatus.PLANNING,
        ExecutionStatus.AGENT_BATCHES,
        ExecutionStatus.AGGREGATING,
    }:
        return ExecutionStatus.READY_FOR_REVIEW
    if current is ExecutionStatus.RUNNING:
        # 人工阶段没有可领取的队列状态；清除运行标记后以 completed 作为
        # 兼容旧读模型的静态承载状态，真正节点仍由 workflow_status 表示。
        return ExecutionStatus.COMPLETED
    return current


def _resumed_execution_status(
    workflow_status: ExecutionStatus,
    current_execution: str,
) -> ExecutionStatus:
    """将暂停节点恢复为对应的旧队列状态。"""

    if workflow_status is ExecutionStatus.QUEUED:
        return ExecutionStatus.QUEUED
    if workflow_status is ExecutionStatus.CI:
        return ExecutionStatus.QUEUED
    if workflow_status in {
        ExecutionStatus.PLANNING,
        ExecutionStatus.AGENT_BATCHES,
        ExecutionStatus.AGGREGATING,
    }:
        return ExecutionStatus.READY_FOR_REVIEW
    # awaiting_approval/awaiting_publish 都是人工节点，不应重新进入 Worker
    # 队列；保留原静态状态，若旧值是 running 则改成 completed。
    current = ExecutionStatus(current_execution)
    return ExecutionStatus.COMPLETED if current is ExecutionStatus.RUNNING else current
