"""租用和推进审查任务的应用边界。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from domain.enums import ExecutionStatus, WorkerStatus
from domain.github import GitHubReviewContext, PullRequestFile
from domain.model_review import (
    MaterializedFinding,
    ModelReviewInput,
    ModelReviewResult,
)
from domain.review_planning import RepositoryRulesSnapshot, ReviewPlan
from domain.security import ErrorCode, SafeApplicationError, SafeError


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


class ReviewTaskQueue(Protocol):
    def record_heartbeat(
        self,
        worker_id: str,
        worker_status: WorkerStatus,
        current_task_id: str | None = None,
    ) -> None:
        """写入 Worker 当前生命周期状态和正在处理的任务。

        参数：
            worker_id: Worker 实例的稳定标识。
            worker_status: ``starting``、``idle``、``busy`` 或 ``stopping``。
            current_task_id: 忙碌时关联的任务 ID；其他状态通常传 ``None``。

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
    ) -> None:
        """在不暴露模型私密思维文本的前提下记录批次级真实进度。"""
        ...

    def store_model_review(
        self,
        lease: ReviewTaskLease,
        review_input: ModelReviewInput,
        result: ModelReviewResult,
        findings: tuple[MaterializedFinding, ...],
        *,
        configuration_revision: int | None = None,
    ) -> StoredModelReview:
        """验证租约、SHA 和计划指纹，并原子保存调用、Finding 与 Outbox。"""
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
