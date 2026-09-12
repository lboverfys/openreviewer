"""budget 阶段的有界事务与数据访问。"""

from collections.abc import Callable

from sqlalchemy import or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from domain.enums import ExecutionStatus
from domain.repository_policy import RepositoryRequestLimitError
from domain.security import SafeError
from persistence.models import ReviewRunRecord, ReviewTaskRecord
from persistence.queue.common import (
    _add_event,
    _locked_owned_task_with_run,
    _set_owned_status,
    _set_workflow_status,
)
from persistence.queue.context import QueueStorage
from services.model_budget import ModelBudgetRequest, ModelBudgetReservation
from services.task_queue import ReviewTaskLease, TaskLeaseLostError, TaskQueueError


def reserve_repository_request(
    self: QueueStorage, lease: ReviewTaskLease, limit: int
) -> None:
    """一条条件 UPDATE 在真实请求前预占额度；失败请求也计入次数。"""
    if not 1 <= limit <= 10_000:
        raise ValueError("repository request limit is invalid")
    now = self._clock()
    owned_task = (
        select(ReviewTaskRecord.id)
        .where(
            ReviewTaskRecord.id == lease.task_id,
            ReviewTaskRecord.review_run_id == lease.review_run_id,
            ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
            ReviewTaskRecord.lease_owner == lease.worker_id,
            ReviewTaskRecord.attempt_count == lease.attempt_count,
            ReviewTaskRecord.model_attempt_count == lease.model_attempt_count,
            ReviewTaskRecord.ci_poll_count == lease.ci_poll_count,
            ReviewTaskRecord.claimed_from_status == lease.claimed_from_status.value,
            or_(
                ReviewTaskRecord.workflow_status.is_(None),
                ReviewTaskRecord.workflow_status != ExecutionStatus.PAUSED.value,
            ),
            ReviewTaskRecord.lease_expires_at > now,
        )
        .exists()
    )
    run_is_owned = (
        ReviewRunRecord.id == lease.review_run_id,
        ReviewRunRecord.execution_status == ExecutionStatus.RUNNING.value,
        or_(
            ReviewRunRecord.workflow_status.is_(None),
            ReviewRunRecord.workflow_status != ExecutionStatus.PAUSED.value,
        ),
        owned_task,
    )
    with self._sessions() as session:
        try:
            used = session.scalar(
                update(ReviewRunRecord)
                .where(
                    *run_is_owned,
                    ReviewRunRecord.model_request_count < limit,
                )
                .values(
                    model_request_count=ReviewRunRecord.model_request_count + 1,
                )
                .returning(ReviewRunRecord.model_request_count)
            )
            if used is None:
                # UPDATE 已接触运行行，错误路径不能再倒序锁任务行；
                # 只读确认租约即可区分额度耗尽和所有权丢失。
                if (
                    session.scalar(select(ReviewRunRecord.id).where(*run_is_owned))
                    is None
                ):
                    raise TaskLeaseLostError()
                raise RepositoryRequestLimitError()
            session.commit()
        except SQLAlchemyError as exc:
            raise TaskQueueError("模型请求额度暂时无法预占") from exc


def monthly_accountant(
    self: QueueStorage, lease: Callable[[], ReviewTaskLease], agent: str
):
    from persistence.usage import SqlAlchemyUsageLedger
    from services.usage import MonthlyModelAccountant

    return MonthlyModelAccountant(
        SqlAlchemyUsageLedger(self._sessions, clock=self._clock), lease, agent
    )


def task_profile_id(self: QueueStorage, lease: ReviewTaskLease) -> str | None:
    with self._sessions() as session:
        return session.scalar(
            select(
                ReviewRunRecord.repository_policy["review_profile_id"].as_string()
            ).where(
                ReviewRunRecord.id == lease.review_run_id,
            )
        )


def pause_for_monthly_budget(
    self: QueueStorage, lease: ReviewTaskLease, error: SafeError
) -> None:
    now = self._clock()
    with self._sessions() as session, session.begin():
        task, run = _locked_owned_task_with_run(session, lease, now)
        _set_owned_status(task, run, ExecutionStatus.READY_FOR_REVIEW, now)
        _set_workflow_status(task, run, ExecutionStatus.PAUSED, now)
        task.workflow_paused_from = ExecutionStatus.AGENT_BATCHES.value
        run.workflow_paused_from = ExecutionStatus.AGENT_BATCHES.value
        task.last_error = error.safe_message
        task.last_error_code = error.code.value
        task.last_error_retryable = False
        task.last_error_details = dict(error.details)
        _add_event(
            self,
            session,
            task,
            "review.budget.paused",
            str(self._uuid_factory()),
            now,
            extra_payload={"reason": error.safe_message},
        )


def reserve_model_budget(
    self: QueueStorage,
    lease: ReviewTaskLease,
    request: ModelBudgetRequest,
    *,
    agent: str = "default",
) -> ModelBudgetReservation:
    """兼容旧调用方，但不再创建全局用量或配额记录。

    模型请求的上下文、响应大小、超时和租约保护由各自运行时负责。
    该入口只返回短生命周期对象，绝不会查询或修改数据库，也不会因
    历史计划字段、Token、费用或请求次数拒绝请求。
    """

    if not agent or len(agent) > 32:
        raise ValueError("model request agent name is invalid")
    numeric_values = (
        request.request_bytes,
        request.input_token_upper_bound,
        request.output_token_upper_bound,
    )
    if any(value < 0 for value in numeric_values):
        raise ValueError("model request bounds cannot be negative")
    if (
        request.cost_upper_bound_microusd is not None
        and request.cost_upper_bound_microusd < 0
    ):
        raise ValueError("model request bounds cannot be negative")
    return ModelBudgetReservation(
        id=str(self._uuid_factory()),
        review_plan_id=lease.review_plan_id or "",
        sequence=1,
        reserved_input_tokens=request.input_token_upper_bound,
        reserved_output_tokens=request.output_token_upper_bound,
        reserved_cost_microusd=request.cost_upper_bound_microusd or 0,
        # 该字段仅为旧类型兼容；模型适配器不会用它限制 HTTP 超时。
        remaining_duration_ms=0,
    )


def settle_model_budget(
    self: QueueStorage,
    reservation: ModelBudgetReservation,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    estimated_cost_microusd: int | None,
    response_status: int | None,
    duration_ms: int,
    uncertain: bool = False,
) -> None:
    """兼容旧签名；模型用量不再由队列结算或持久化。"""

    del (
        reservation,
        input_tokens,
        output_tokens,
        estimated_cost_microusd,
        response_status,
        duration_ms,
        uncertain,
    )


def pause_for_model_budget(
    self: QueueStorage,
    lease: ReviewTaskLease,
    error: SafeError,
) -> None:
    """兼容旧 Worker；绝不把任务切到人工暂停状态。"""

    del lease, error
