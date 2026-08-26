"""固定多 Agent 审查 DAG 的纯领域状态机。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from domain.enums import ExecutionStatus


class WorkflowAction(str, Enum):
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    RETRY_STAGE = "retry_stage"
    APPROVE = "approve"
    REJECT = "reject"
    PUBLISH = "publish"


class WorkflowTransitionError(ValueError):
    """当前节点不允许执行请求的动作。"""


WORKFLOW_STAGES: tuple[ExecutionStatus, ...] = (
    ExecutionStatus.QUEUED,
    ExecutionStatus.CI,
    ExecutionStatus.PLANNING,
    ExecutionStatus.AGENT_BATCHES,
    ExecutionStatus.AGGREGATING,
    ExecutionStatus.AWAITING_APPROVAL,
    ExecutionStatus.APPROVED,
    ExecutionStatus.REJECTED,
    ExecutionStatus.AWAITING_PUBLISH,
    ExecutionStatus.PUBLISHING,
    ExecutionStatus.COMPLETED,
    ExecutionStatus.FAILED,
    ExecutionStatus.PAUSED,
)


# 暂停后允许继续的节点。终态和会触发外部副作用的 publishing 不可作为
# 恢复目标；调用方若没有保存暂停前节点，才使用 CI 作为兼容默认值。
RESUMABLE_STAGES: frozenset[ExecutionStatus] = frozenset(
    {
        ExecutionStatus.QUEUED,
        ExecutionStatus.CI,
        ExecutionStatus.PLANNING,
        ExecutionStatus.AGENT_BATCHES,
        ExecutionStatus.AGGREGATING,
        ExecutionStatus.AWAITING_APPROVAL,
        ExecutionStatus.AWAITING_PUBLISH,
    }
)

RETRYABLE_STAGES: frozenset[ExecutionStatus] = frozenset(
    {
        ExecutionStatus.CI,
        ExecutionStatus.PLANNING,
        ExecutionStatus.AGENT_BATCHES,
        ExecutionStatus.AGGREGATING,
    }
)


@dataclass(frozen=True, slots=True)
class WorkflowTransition:
    before: ExecutionStatus
    after: ExecutionStatus
    action: WorkflowAction
    target_stage: ExecutionStatus | None = None


def transition(
    current: ExecutionStatus,
    action: WorkflowAction,
    *,
    target_stage: ExecutionStatus | None = None,
) -> WorkflowTransition:
    """计算一次人工或编排动作的目标节点，不访问数据库或外部服务。"""

    if action is WorkflowAction.START:
        if current is not ExecutionStatus.QUEUED:
            raise WorkflowTransitionError("只有排队任务可以开始审查")
        return WorkflowTransition(current, ExecutionStatus.CI, action)
    if action is WorkflowAction.PAUSE:
        if current not in RESUMABLE_STAGES:
            raise WorkflowTransitionError("当前节点不能暂停")
        return WorkflowTransition(current, ExecutionStatus.PAUSED, action)
    if action is WorkflowAction.RESUME:
        if current is not ExecutionStatus.PAUSED:
            raise WorkflowTransitionError("当前任务没有暂停")
        stage = target_stage or ExecutionStatus.CI
        if stage not in RESUMABLE_STAGES:
            raise WorkflowTransitionError("继续目标节点无效")
        return WorkflowTransition(current, stage, action, stage)
    if action is WorkflowAction.RETRY_STAGE:
        if current not in {
            ExecutionStatus.FAILED,
            ExecutionStatus.REJECTED,
            ExecutionStatus.AWAITING_APPROVAL,
        }:
            raise WorkflowTransitionError("当前节点不能重试")
        stage = target_stage or ExecutionStatus.AGENT_BATCHES
        if stage not in RETRYABLE_STAGES:
            raise WorkflowTransitionError("重试目标节点无效")
        return WorkflowTransition(current, stage, action, stage)
    if action is WorkflowAction.APPROVE:
        if current is not ExecutionStatus.AWAITING_APPROVAL:
            raise WorkflowTransitionError("只有待批准审查可以批准")
        # ``approved`` 是人工判定本身；持久化适配器随后按固定自动边推进到
        # awaiting_publish，并分别记录两次状态变化。发布仍必须由用户另行触发。
        return WorkflowTransition(current, ExecutionStatus.APPROVED, action)
    if action is WorkflowAction.REJECT:
        if current not in {
            ExecutionStatus.AWAITING_APPROVAL,
            ExecutionStatus.AWAITING_PUBLISH,
        }:
            raise WorkflowTransitionError("只有待批准或待发布审查可以驳回")
        return WorkflowTransition(current, ExecutionStatus.REJECTED, action, target_stage)
    if action is WorkflowAction.PUBLISH:
        if current is not ExecutionStatus.AWAITING_PUBLISH:
            raise WorkflowTransitionError("只有批准后的审查可以发布")
        return WorkflowTransition(current, ExecutionStatus.PUBLISHING, action)
    raise WorkflowTransitionError("不支持的工作流动作")


def next_automatic_stage(current: ExecutionStatus) -> ExecutionStatus | None:
    """返回固定 DAG 的下一个自动节点；人工节点返回 ``None``。"""

    mapping = {
        ExecutionStatus.CI: ExecutionStatus.PLANNING,
        ExecutionStatus.PLANNING: ExecutionStatus.AGENT_BATCHES,
        ExecutionStatus.AGENT_BATCHES: ExecutionStatus.AGGREGATING,
        ExecutionStatus.AGGREGATING: ExecutionStatus.AWAITING_APPROVAL,
        ExecutionStatus.APPROVED: ExecutionStatus.AWAITING_PUBLISH,
        ExecutionStatus.PUBLISHING: ExecutionStatus.COMPLETED,
    }
    return mapping.get(current)
