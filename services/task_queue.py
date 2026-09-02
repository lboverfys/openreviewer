"""租用和推进审查任务的应用边界。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from domain.enums import ExecutionStatus, ModelBatchStatus, WorkerStatus
from domain.github import GitHubReviewContext, PullRequestFile
from domain.model_review import (
    MaterializedFinding,
    ModelReviewInput,
    ModelReviewResult,
)
from domain.review_planning import RepositoryRulesSnapshot, ReviewPlan
from domain.security import ErrorCode, SafeApplicationError, SafeError
from services.model_budget import (
    ModelBudgetRequest,
    ModelBudgetReservation,
)
from services.model_review import ModelReviewBatch


class TaskQueueError(SafeApplicationError):
    """持久化队列操作无法完成。"""

    def __init__(self, message: str = "任务队列暂时不可用") -> None:
        super().__init__(
            SafeError(
                code=ErrorCode.TASK_QUEUE_UNAVAILABLE,
                safe_message=message,
                retryable=True,
            )
        )


class TaskLeaseLostError(TaskQueueError):
    """Worker 失去租约后仍尝试修改任务。"""

    def __init__(self, message: str = "Worker 已失去任务租约") -> None:
        SafeApplicationError.__init__(
            self,
            SafeError(
                code=ErrorCode.TASK_LEASE_LOST,
                safe_message=message,
                retryable=False,
            ),
        )


class ReviewPlanInputError(TaskQueueError):
    """数据库里的 PR 文件快照不足以生成可信计划。"""

    def __init__(self, message: str = "PR 文件快照不完整，无法生成审查计划") -> None:
        SafeApplicationError.__init__(
            self,
            SafeError(
                code=ErrorCode.REVIEW_PLAN_INPUT_INCOMPLETE,
                safe_message=message,
                retryable=False,
            ),
        )


class ReviewPlanConflictError(TaskQueueError):
    """同一运行已经保存了不同指纹，或目标版本已经过期。"""

    def __init__(self, message: str = "审查计划与当前任务版本冲突") -> None:
        SafeApplicationError.__init__(
            self,
            SafeError(
                code=ErrorCode.REVIEW_PLAN_CONFLICT,
                safe_message=message,
                retryable=False,
            ),
        )


class ModelReviewInputError(TaskQueueError):
    """持久化计划无法构造成可信的模型输入。"""

    def __init__(self, message: str = "模型审查输入不完整") -> None:
        SafeApplicationError.__init__(
            self,
            SafeError(
                code=ErrorCode.MODEL_REVIEW_INPUT_INVALID,
                safe_message=message,
                retryable=False,
            ),
        )


class ModelReviewConflictError(TaskQueueError):
    """模型结果来自旧租约、旧 SHA 或不同 Review Plan。"""

    def __init__(self, message: str = "模型审查结果与当前计划冲突") -> None:
        SafeApplicationError.__init__(
            self,
            SafeError(
                code=ErrorCode.MODEL_REVIEW_CONFLICT,
                safe_message=message,
                retryable=False,
            ),
        )


class ModelReviewCheckpointTooLargeError(ModelReviewConflictError):
    """截断恢复检查点超过持久化边界，但模型结果本身仍然有效。"""

    def __init__(self) -> None:
        SafeApplicationError.__init__(
            self,
            SafeError(
                code=ErrorCode.MODEL_REVIEW_CONFLICT,
                safe_message="截断恢复检查点超过安全大小",
                retryable=False,
                details={"checkpoint_too_large": True},
            ),
        )


class ModelBatchBusyError(TaskQueueError):
    """批次仍有有效执行租约，应等待而不是重复调用模型。"""

    def __init__(
        self,
        message: str = "模型批次正在执行，请稍后重试",
        *,
        retry_at: datetime | None = None,
        lease_expires_at: datetime | None = None,
        available_at: datetime | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """构造可安全持久化的批次级等待错误。

        ``retry_at`` 由队列从批次租约或退避时间计算得出。把时间放进结构化
        错误详情后，Worker 可以把任务重新排到正确的时间点，而不会在默认的
        5 秒任务退避内反复抢同一批次。
        """

        error_details: dict[str, object] = {
            "batch_retry_managed": True,
            **dict(details or {}),
        }
        if retry_at is not None:
            error_details["retry_at"] = retry_at.isoformat()
        if lease_expires_at is not None:
            error_details["lease_expires_at"] = lease_expires_at.isoformat()
        if available_at is not None:
            error_details["available_at"] = available_at.isoformat()
        SafeApplicationError.__init__(
            self,
            SafeError(
                code=ErrorCode.MODEL_BATCH_BUSY,
                safe_message=message,
                retryable=True,
                details=error_details,
            ),
        )


class ModelBudgetExceededError(TaskQueueError):
    """旧版硬预算计划超限，必须转人工处理。"""

    def __init__(
        self,
        reason: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        SafeApplicationError.__init__(
            self,
            SafeError(
                code=ErrorCode.MODEL_BUDGET_EXCEEDED,
                safe_message="模型审查已达到资源预算，已暂停等待人工处理",
                retryable=False,
                details={"budget_reason": reason, **dict(details or {})},
            ),
        )


@dataclass(frozen=True, slots=True)
class ReviewTaskLease:
    task_id: str
    review_run_id: str
    worker_id: str
    attempt_count: int
    lease_expires_at: datetime
    claimed_from_status: ExecutionStatus = ExecutionStatus.QUEUED
    ci_poll_count: int = 0
    model_attempt_count: int = 0
    review_plan_id: str | None = None
    # 汇总增强节点失败后，人工重试需要在复用前三路成功批次的同时
    # 强制再次调用汇总模型；该标记由持久化队列从最近的汇总失败事件投影。
    force_summary: bool = False


@dataclass(frozen=True, slots=True)
class ReviewTarget:
    """Worker 在不持有数据库事务时读取 GitHub 所需的稳定任务身份。"""

    installation_id: int
    repository_id: int
    repository: str
    pull_request_number: int
    head_sha: str
    review_version_key: str
    context_fetched_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReviewPlanningInput:
    """一次有界数据库读取产生的计划目标和 changed files。"""

    target: ReviewTarget
    files: tuple[PullRequestFile, ...]


@dataclass(frozen=True, slots=True)
class StoredReviewPlan:
    """计划原子保存或幂等复用的结果。"""

    plan_id: str | None
    created: bool
    execution_status: ExecutionStatus


@dataclass(frozen=True, slots=True)
class StoredModelReview:
    """模型调用与候选 Finding 原子保存或幂等复用的结果。"""

    model_call_id: str | None
    created: bool
    finding_count: int
    execution_status: ExecutionStatus
    coverage_status: str = "complete"
    partial: bool = False


@dataclass(frozen=True, slots=True)
class StoredModelBatch:
    """持久化批次的最小恢复快照。"""

    id: str
    review_plan_id: str
    agent: str
    batch_number: int
    batch_count: int
    unit_keys: tuple[str, ...]
    estimated_input_tokens: int
    status: ModelBatchStatus
    attempt_count: int
    request_fingerprint: str | None
    result: ModelReviewResult | None
    error_code: str | None
    error_message: str | None
    # 输出截断后拆分恢复所需的有界检查点；不包含密钥或原始提示词。
    checkpoint: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ModelBatchLease:
    """心跳续租所需的批次身份和租约时长。"""

    agent: str
    batch_number: int
    lease_duration: timedelta


class ReviewTaskQueue(Protocol):
    def start_heartbeat(self, worker_id: str, instance_id: str) -> None:
        """用进程级 token 原子接管稳定 Worker ID 的心跳行。"""

        ...

    def record_heartbeat(
        self,
        worker_id: str,
        worker_status: WorkerStatus,
        current_task_id: str | None = None,
        *,
        instance_id: str | None = None,
    ) -> None:
        """写入 Worker 当前生命周期状态和正在处理的任务。

        参数：
            worker_id: Worker 实例的稳定标识。
            worker_status: ``starting``、``idle``、``busy`` 或 ``stopping``。
            current_task_id: 忙碌时关联的任务 ID；其他状态通常传 ``None``。
            instance_id: 当前进程的心跳所有权 token；不匹配必须拒绝写入。

        异常：
            TaskQueueError: 心跳无法持久化。

        实现应更新最近心跳时间；首次看到该 ID 时还要创建心跳记录。
        """
        ...

    def recover_expired_leases(self) -> int:
        """扫描处于运行中但租约已经到期的任务并恢复状态。

        返回：
            本轮锁定并处理的过期任务数量。没有过期任务时返回 0。

        异常：
            TaskQueueError: 扫描、状态更新或事务提交失败。

        未耗尽尝试次数的任务应退避后重新排队，耗尽次数的任务应最终失败；
        实现必须同步更新审查运行并写入 Outbox 事件。
        """
        ...

    def claim_next(
        self,
        worker_id: str,
        lease_duration: timedelta,
        *,
        ai_configured: bool = True,
    ) -> ReviewTaskLease | None:
        """按队列顺序原子领取一个当前可执行的任务。

        参数：
            worker_id: 领取任务的 Worker ID，随后会成为租约所有者。
            lease_duration: 从领取时刻开始计算的租约有效期，必须大于零。

        返回：
            成功时返回包含任务、运行、Worker、失败尝试次数、CI 轮询代次和到期
            时间的租约；当前没有可领取任务时返回 ``None``。

        异常：
            ValueError: 租约时长不大于零。
            TaskQueueError: 锁定任务或原子更新状态失败。
        """
        ...

    def renew_lease(
        self,
        lease: ReviewTaskLease,
        lease_duration: timedelta,
    ) -> ReviewTaskLease:
        """在所有权仍有效时延长任务租约。

        参数：
            lease: 领取任务时得到的旧租约快照。
            lease_duration: 从续租时刻重新计算的新有效期，必须大于零。

        返回：
            身份字段不变、仅到期时间更新的新 ``ReviewTaskLease``。

        异常：
            ValueError: 新租约时长不大于零。
            TaskLeaseLostError: 任务、运行、Worker、失败尝试次数、CI 轮询代次或
            有效期不再匹配。
            TaskQueueError: 数据库操作失败。
        """
        ...

    def load_target(self, lease: ReviewTaskLease) -> ReviewTarget:
        """用一次 JOIN 读取当前租约关联的 PR 身份和上下文准备状态。"""
        ...

    def store_github_context(
        self,
        lease: ReviewTaskLease,
        context: GitHubReviewContext,
        *,
        ci_poll_interval: timedelta,
        ci_wait_timeout: timedelta,
    ) -> ExecutionStatus:
        """原子保存 GitHub 快照并推进、等待、取消或淘汰当前任务。"""
        ...

    def load_planning_input(self, lease: ReviewTaskLease) -> ReviewPlanningInput:
        """一次有界 JOIN 读取当前 SHA 的目标和全部持久化 changed files。"""
        ...

    def store_review_plan(
        self,
        lease: ReviewTaskLease,
        rules: RepositoryRulesSnapshot,
        plan: ReviewPlan,
    ) -> StoredReviewPlan:
        """验证租约与 SHA，并原子批量保存规则、Unit、文件结果和 Outbox。"""
        ...

    def load_model_review_input(self, lease: ReviewTaskLease) -> ModelReviewInput:
        """用固定三次有界查询读取计划、规则和全部 Review Unit。"""
        ...

    def record_model_progress(
        self,
        lease: ReviewTaskLease,
        phase: str,
        payload: Mapping[str, object],
        *,
        agent: str = "default",
    ) -> None:
        """在不暴露模型私密思维文本的前提下记录批次级真实进度。"""
        ...

    def mark_model_aggregating(self, lease: ReviewTaskLease) -> None:
        """三路 Agent 完成后，把固定 DAG 原子推进到结果汇总节点。"""
        ...

    def ensure_model_batches(
        self,
        lease: ReviewTaskLease,
        batches: tuple[ModelReviewBatch, ...],
        *,
        agent: str = "default",
    ) -> tuple[StoredModelBatch, ...]:
        """幂等保存批次定义并返回当前状态。

        同一计划因配置变化而重新规划时，仅在旧批次没有成功结果且没有有效
        运行租约的情况下允许安全重建；否则应报告模型计划冲突。
        """
        ...

    def claim_model_batch(
        self,
        lease: ReviewTaskLease,
        batch_number: int,
        *,
        agent: str = "default",
        lease_duration: timedelta,
    ) -> StoredModelBatch:
        """锁定一个待执行批次；已成功批次直接返回且不会再次执行。"""
        ...

    def renew_model_batch(
        self,
        lease: ReviewTaskLease,
        batch_number: int,
        *,
        agent: str = "default",
        lease_duration: timedelta,
    ) -> StoredModelBatch:
        """在任务租约有效且当前 Worker 仍持有批次时延长批次租约。"""
        ...

    def renew_model_batches(
        self,
        lease: ReviewTaskLease,
        batches: tuple[ModelBatchLease, ...],
    ) -> tuple[StoredModelBatch, ...]:
        """在一个短事务中批量延长所有活动模型批次租约。"""
        ...

    def complete_model_batch(
        self,
        lease: ReviewTaskLease,
        batch_number: int,
        result: ModelReviewResult,
        *,
        agent: str = "default",
        expected_attempt_count: int | None = None,
    ) -> StoredModelBatch:
        """原子保存单批结果，并可校验领取时的批次代次。"""
        ...

    def checkpoint_model_batch(
        self,
        lease: ReviewTaskLease,
        batch_number: int,
        checkpoint: Mapping[str, object],
        *,
        agent: str = "default",
        expected_attempt_count: int | None = None,
    ) -> StoredModelBatch:
        """保存输出截断拆分的有界恢复检查点，不结束批次租约。"""
        ...

    def fail_model_batch(
        self,
        lease: ReviewTaskLease,
        batch_number: int,
        error: SafeError,
        *,
        agent: str = "default",
        retry_delay: timedelta | None = None,
        expected_attempt_count: int | None = None,
    ) -> StoredModelBatch:
        """保存单批安全错误并安排阶段级重试。"""
        ...

    def load_model_batches(
        self,
        lease: ReviewTaskLease,
        *,
        agent: str = "default",
    ) -> tuple[StoredModelBatch, ...]:
        """按批次号读取一个 Agent 的有界批次结果。"""
        ...

    def reserve_model_budget(
        self,
        lease: ReviewTaskLease,
        request: ModelBudgetRequest,
        *,
        agent: str = "default",
    ) -> ModelBudgetReservation:
        """在真实 HTTP 请求前原子预留计划级用量并执行兼容阈值策略。"""
        ...

    def settle_model_budget(
        self,
        reservation: ModelBudgetReservation,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        estimated_cost_microusd: int | None,
        response_status: int | None,
        duration_ms: int,
        uncertain: bool = False,
    ) -> None:
        """按供应商真实用量结算；未知结果保留保守预留。"""
        ...

    def pause_for_model_budget(
        self,
        lease: ReviewTaskLease,
        error: SafeError,
    ) -> None:
        """旧版硬预算超限时撤销租约并进入可人工重新审查的暂停状态。"""
        ...

    def store_model_review(
        self,
        lease: ReviewTaskLease,
        review_input: ModelReviewInput,
        result: ModelReviewResult,
        findings: tuple[MaterializedFinding, ...],
        *,
        configuration_revision: int | None = None,
        partial: bool = False,
    ) -> StoredModelReview:
        """验证租约、SHA 和计划指纹，并原子保存调用、Finding 与 Outbox。

        ``partial=True`` 表示固定 DAG 仍有失败节点，但已有 Agent 结果可以
        先展示。该模式不会写入模型阶段完成时间，成功批次和 Finding 会保留，
        后续失败节点重试时可幂等复用。
        """
        ...

    def mark_waiting_for_ci(self, lease: ReviewTaskLease) -> None:
        """把仍由当前租约拥有的任务推进到 ``waiting_for_ci``。

        参数：
            lease: 当前 Worker 持有且尚未过期的租约。

        副作用：
            同步更新任务和审查运行，清除租约，并追加等待 CI 的 Outbox 事件。

        异常：
            TaskLeaseLostError: 租约已经失效或所有权不匹配。
            TaskQueueError: 关联运行缺失或事务无法提交。
        """
        ...

    def retry_or_fail(self, lease: ReviewTaskLease, error: SafeError) -> None:
        """记录本次处理错误，并按剩余尝试次数选择重试或失败。

        参数：
            lease: 发生错误时 Worker 仍持有的任务租约。
            error: 已分类、已脱敏且带稳定错误码的安全错误对象。

        副作用：
            清除当前租约；任务可能带退避时间回到 ``queued``，也可能与运行一起
            进入 ``failed``，并写入对应 Outbox 事件。

        异常：
            TaskLeaseLostError: 失败上报前租约已经失效。
            TaskQueueError: 状态无法持久化。
        """
        ...

    def heartbeat_is_fresh(self, worker_id: str, max_age: timedelta) -> bool:
        """判断指定 Worker 的最后心跳是否仍在健康窗口内。

        参数：
            worker_id: 要检查的稳定 Worker ID。
            max_age: 允许的最大心跳年龄。

        返回：
            存在记录且 ``当前时间 - last_seen_at <= max_age`` 时返回 ``True``；
            没有记录或记录过旧时返回 ``False``。

        异常：
            TaskQueueError: 心跳查询失败。
        """
        ...
