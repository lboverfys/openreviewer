"""progress 阶段的有界事务与数据访问。"""

from collections.abc import Mapping

from sqlalchemy.exc import SQLAlchemyError

from domain.enums import ExecutionStatus
from persistence.queue.common import (
    _add_event,
    _locked_owned_task_with_run,
    _set_workflow_status,
)
from persistence.queue.context import QueueStorage
from services.task_queue import (
    ModelReviewConflictError,
    ReviewTaskLease,
    TaskLeaseLostError,
    TaskQueueError,
)


def record_model_progress(
    self: QueueStorage,
    lease: ReviewTaskLease,
    phase: str,
    payload: Mapping[str, object],
    *,
    agent: str = "default",
) -> None:
    """用短事务写入模型批次进度，不保存或伪造 Chain-of-Thought。"""

    if not agent or len(agent) > 32:
        raise ValueError("model progress agent name is invalid")
    allowed_phases = {
        "agent_started",
        "retrieval_started",
        "retrieval_completed",
        "incremental_reused",
        "batches_planned",
        "batch_started",
        "request_started",
        "request_completed",
        "batch_completed",
        "batch_failed",
        "agent_completed",
        "agent_failed",
        "agent_not_applicable",
        "summary_completed",
        "summary_skipped",
        "aggregation_completed",
        "workflow_partial",
        "retry_requested",
        "retry_started",
    }
    if phase not in allowed_phases:
        raise ValueError("unsupported model progress phase")
    if lease.claimed_from_status is not ExecutionStatus.READY_FOR_REVIEW:
        raise ModelReviewConflictError("当前租约不属于模型审查阶段")
    now = self._clock()
    with self._sessions() as session:
        try:
            task, run = _locked_owned_task_with_run(session, lease, now)
            task.updated_at = now
            run.updated_at = now
            _add_event(
                self,
                session,
                task,
                f"review.model.{phase}",
                f"{agent}:model-attempt-{task.model_attempt_count}",
                now,
                extra_payload={"agent": agent, **dict(payload)},
            )
            session.commit()
        except (ModelReviewConflictError, TaskLeaseLostError):
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError("model progress could not be persisted") from exc


def mark_model_aggregating(self: QueueStorage, lease: ReviewTaskLease) -> None:
    """在三路 Agent 完成后，用短事务暴露固定 DAG 的汇总节点。"""

    if lease.claimed_from_status is not ExecutionStatus.READY_FOR_REVIEW:
        raise ModelReviewConflictError("当前租约不属于模型审查阶段")
    now = self._clock()
    with self._sessions() as session:
        try:
            task, run = _locked_owned_task_with_run(session, lease, now)
            current = ExecutionStatus(task.workflow_status)
            if current not in {
                ExecutionStatus.AGENT_BATCHES,
                ExecutionStatus.AGGREGATING,
            }:
                raise ModelReviewConflictError("当前工作流不能进入结果汇总阶段")
            if current is not ExecutionStatus.AGGREGATING:
                _set_workflow_status(
                    task,
                    run,
                    ExecutionStatus.AGGREGATING,
                    now,
                )
                _add_event(
                    self,
                    session,
                    task,
                    "review.model.aggregating_started",
                    f"model-attempt-{task.model_attempt_count}",
                    now,
                    extra_payload={
                        "review_plan_id": lease.review_plan_id,
                        "agent_count": 3,
                    },
                )
            session.commit()
        except (ModelReviewConflictError, TaskLeaseLostError):
            session.rollback()
            raise
        except (SQLAlchemyError, ValueError) as exc:
            session.rollback()
            raise TaskQueueError(
                "model aggregation state could not be persisted"
            ) from exc
