"""scheduling 阶段的有界事务与数据访问。"""

from datetime import timedelta

from sqlalchemy import func, literal, or_, select
from sqlalchemy.exc import SQLAlchemyError

from domain.enums import ExecutionStatus
from domain.security import ErrorCode, SafeError
from persistence.models import (
    RepositoryPolicyRecord,
    RepositoryScheduleRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.queue.common import (
    _add_event,
    _latest_summary_failed,
    _locked_owned_task,
    _locked_owned_task_with_run,
    _reschedule_or_fail,
    _set_workflow_status,
    _task_run_mutation_load_options,
)
from persistence.queue.context import QueueStorage
from services.task_queue import ReviewTaskLease, TaskLeaseLostError, TaskQueueError


def recover_expired_leases(self: QueueStorage) -> int:
    """恢复一批已过期的运行中任务租约。

    查询使用 ``FOR UPDATE SKIP LOCKED``，多个 Worker 并行恢复时不会互相等待
    或重复处理同一任务。每个任务根据剩余尝试次数回到 ``queued`` 并设置退避，
    或进入 ``failed``；状态变化和 Outbox 事件在同一事务中提交。

    返回：
        本次事务成功处理的过期任务数量。

    异常：
        TaskQueueError: 查询、状态转换或事务提交失败；整个批次会回滚，避免
        只恢复一部分任务。

    任务状态筛选只接受 ``running`` 且 ``lease_expires_at <= now`` 的行，
    每批最多处理配置的 ``recovery_batch_size`` 条。任务与关联运行通过同一
    JOIN 读取；如果运行缺失，该任务不会被误当成可恢复记录。
    """
    now = self._clock()
    with self._sessions() as session:
        try:
            statement = (
                select(ReviewTaskRecord, ReviewRunRecord)
                .join(
                    ReviewRunRecord,
                    ReviewRunRecord.id == ReviewTaskRecord.review_run_id,
                )
                .where(
                    ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
                    or_(
                        ReviewTaskRecord.workflow_status.is_(None),
                        ReviewTaskRecord.workflow_status
                        != ExecutionStatus.PAUSED.value,
                    ),
                    or_(
                        ReviewRunRecord.workflow_status.is_(None),
                        ReviewRunRecord.workflow_status != ExecutionStatus.PAUSED.value,
                    ),
                    ReviewTaskRecord.lease_expires_at.is_not(None),
                    ReviewTaskRecord.lease_expires_at <= now,
                )
                .order_by(ReviewTaskRecord.lease_expires_at.asc())
                .limit(self._recovery_batch_size)
                .options(*_task_run_mutation_load_options())
                .with_for_update(skip_locked=True)
            )
            expired_tasks = list(session.execute(statement))
            lease_error = SafeError(
                code=ErrorCode.TASK_LEASE_EXPIRED,
                safe_message="Worker 租约超时，任务已进入恢复流程",
                retryable=True,
            )
            for task, run in expired_tasks:
                is_model_stage = (
                    task.claimed_from_status == ExecutionStatus.READY_FOR_REVIEW.value
                    and task.model_attempt_count > 0
                )
                _reschedule_or_fail(
                    self,
                    session,
                    task,
                    run,
                    now,
                    lease_error,
                    event_suffix=(
                        f"lease-expired-model-{task.model_attempt_count}"
                        if is_model_stage
                        else f"lease-expired-{task.attempt_count}"
                    ),
                )
            session.commit()
            return len(expired_tasks)
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError("expired task leases could not be recovered") from exc


def claim_next(
    self: QueueStorage,
    worker_id: str,
    lease_duration: timedelta,
    *,
    ai_configured: bool = True,
) -> ReviewTaskLease | None:
    """原子领取一个当前可执行的任务。

    选择 ``queued``、到期的 ``waiting_for_ci``，或尚未保存计划的
    ``ready_for_review`` 任务，并按优先级、可用时间和创建时间排序。锁定后
    同时更新任务和运行状态、原阶段、尝试次数、租约所有者及过期时间，再写入
    ``review.task.running`` 事件；没有可领取任务时返回 ``None``。

    参数：
        worker_id: 领取者的稳定身份，会写入 ``lease_owner``。
        lease_duration: 从当前时钟到租约到期的时长，必须大于零。

    返回：
        成功时返回包含数据库主键、运行 ID、尝试次数和到期时间的租约快照；
        没有合资格任务时返回 ``None``，此时只提交一个空事务并释放锁。

    异常：
        ValueError: 租约时长不大于零。
        TaskQueueError: 关联运行不存在、数据库锁定/更新/提交失败。

    新任务或错误重试会增加 ``attempt_count``；正常 CI 轮询只增加
    ``ci_poll_count``，不会因为等待外部流水线而耗尽错误重试次数。
    """
    if lease_duration.total_seconds() <= 0:
        raise ValueError("lease_duration must be positive")
    now = self._clock()
    lease_expires_at = now + lease_duration
    with self._sessions() as session:
        try:
            plan_id_query = (
                select(ReviewPlanRecord.id)
                .where(ReviewPlanRecord.review_run_id == ReviewRunRecord.id)
                .scalar_subquery()
            )
            model_completed_query = (
                select(ReviewPlanRecord.model_review_completed_at)
                .where(ReviewPlanRecord.review_run_id == ReviewRunRecord.id)
                .scalar_subquery()
            )
            # 领取仅占用一个短事务；跨 Worker 串行化“检查并发数 + 领取”，
            # 避免两个进程同时看到余量。外部调用不持有该锁。
            if session.get_bind().dialect.name == "postgresql":
                if not session.scalar(
                    select(func.pg_try_advisory_xact_lock(1_937_516_042))
                ):
                    return None
            running_by_repository = (
                select(
                    ReviewRunRecord.repository_key.label("repository_key"),
                    func.count().label("running_count"),
                )
                .join(
                    ReviewTaskRecord,
                    ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
                )
                .where(
                    ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
                    ReviewTaskRecord.lease_expires_at > now,
                )
                .group_by(ReviewRunRecord.repository_key)
                .subquery()
            )
            concurrency_limit = RepositoryPolicyRecord.policy[
                "max_concurrent_reviews"
            ].as_integer()
            statement = (
                select(
                    ReviewTaskRecord,
                    ReviewRunRecord,
                    plan_id_query.label("review_plan_id"),
                )
                .join(
                    ReviewRunRecord,
                    ReviewRunRecord.id == ReviewTaskRecord.review_run_id,
                )
                .outerjoin(
                    RepositoryPolicyRecord,
                    RepositoryPolicyRecord.repository_key
                    == ReviewRunRecord.repository_key,
                )
                .outerjoin(
                    RepositoryScheduleRecord,
                    RepositoryScheduleRecord.repository_key
                    == ReviewRunRecord.repository_key,
                )
                .outerjoin(
                    running_by_repository,
                    running_by_repository.c.repository_key
                    == ReviewRunRecord.repository_key,
                )
                .where(
                    or_(
                        concurrency_limit.is_(None),
                        func.coalesce(running_by_repository.c.running_count, 0)
                        < concurrency_limit,
                    ),
                    ReviewTaskRecord.execution_status.in_(
                        (
                            ExecutionStatus.QUEUED.value,
                            ExecutionStatus.WAITING_FOR_CI.value,
                            ExecutionStatus.READY_FOR_REVIEW.value,
                        )
                    ),
                    or_(
                        ReviewTaskRecord.workflow_status.is_(None),
                        ReviewTaskRecord.workflow_status
                        != ExecutionStatus.PAUSED.value,
                    ),
                    or_(
                        ReviewRunRecord.workflow_status.is_(None),
                        ReviewRunRecord.workflow_status != ExecutionStatus.PAUSED.value,
                    ),
                    ReviewTaskRecord.available_at <= now,
                    or_(
                        literal(ai_configured),
                        ReviewRunRecord.repository_policy["review_profile_id"]
                        .as_string()
                        .is_not(None),
                        ReviewTaskRecord.execution_status
                        != ExecutionStatus.READY_FOR_REVIEW.value,
                    ),
                    or_(
                        ReviewTaskRecord.execution_status
                        != ExecutionStatus.READY_FOR_REVIEW.value,
                        plan_id_query.is_(None),
                        model_completed_query.is_(None),
                    ),
                )
                .order_by(
                    ReviewTaskRecord.priority.desc(),
                    RepositoryScheduleRecord.last_claimed_at.asc().nulls_first(),
                    ReviewTaskRecord.available_at.asc(),
                    ReviewTaskRecord.created_at.asc(),
                )
                .limit(1)
                .options(*_task_run_mutation_load_options())
                .with_for_update(
                    skip_locked=True, of=(ReviewTaskRecord, ReviewRunRecord)
                )
            )
            # 行锁保证多个 Worker 同时轮询时，只有一个能拿到这条任务。
            row = session.execute(statement).one_or_none()
            if row is None:
                session.commit()
                return None
            task, run, review_plan_id = row

            from persistence.platform_common import dialect_insert

            session.execute(
                dialect_insert(session, RepositoryScheduleRecord)
                .values(
                    repository_key=run.repository_key,
                    last_claimed_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["repository_key"], set_={"last_claimed_at": now}
                )
            )

            if task.first_claimed_at is None:
                task.first_claimed_at = now

            claimed_from_status = ExecutionStatus(task.execution_status)
            task.execution_status = ExecutionStatus.RUNNING.value
            is_model_stage = (
                claimed_from_status is ExecutionStatus.READY_FOR_REVIEW
                and review_plan_id is not None
            )
            force_summary = (
                _latest_summary_failed(session, task.review_run_id)
                if is_model_stage
                else False
            )
            if is_model_stage:
                task.model_attempt_count += 1
            elif claimed_from_status in {
                ExecutionStatus.QUEUED,
                ExecutionStatus.READY_FOR_REVIEW,
            }:
                task.attempt_count += 1
            else:
                task.ci_poll_count += 1
            task.claimed_from_status = claimed_from_status.value
            task.lease_owner = worker_id
            task.lease_expires_at = lease_expires_at
            task.last_error = None
            task.last_error_code = None
            task.last_error_retryable = None
            task.last_error_details = None
            task.updated_at = now
            run.execution_status = ExecutionStatus.RUNNING.value
            run.updated_at = now
            _add_event(
                self,
                session,
                task,
                "review.task.running",
                (
                    f"model-review:{task.model_attempt_count}"
                    if is_model_stage
                    else f"{claimed_from_status.value}:{task.attempt_count}:"
                    f"ci-poll-{task.ci_poll_count}"
                ),
                now,
            )
            session.commit()
            return ReviewTaskLease(
                task_id=task.id,
                review_run_id=task.review_run_id,
                worker_id=worker_id,
                attempt_count=task.attempt_count,
                model_attempt_count=task.model_attempt_count,
                lease_expires_at=lease_expires_at,
                ci_poll_count=task.ci_poll_count,
                claimed_from_status=claimed_from_status,
                review_plan_id=review_plan_id,
                force_summary=force_summary,
            )
        except TaskQueueError:
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError("the next review task could not be claimed") from exc


def renew_lease(
    self: QueueStorage,
    lease: ReviewTaskLease,
    lease_duration: timedelta,
) -> ReviewTaskLease:
    """延长当前租约并返回新的租约对象。

    更新前会再次按任务 ID、运行 ID、Worker ID、尝试次数和未过期条件加锁
    校验。任何一项不匹配都意味着旧 Worker 已失去所有权，此时抛出
    ``TaskLeaseLostError``，阻止过期 Worker 覆盖新 Worker 的状态。

    参数：
        lease: 之前领取任务时保存的所有权快照。
        lease_duration: 从本次续租时刻重新计算的有效期，必须大于零。

    返回：
        与旧租约身份相同、``lease_expires_at`` 更新后的新快照。

    异常：
        ValueError: 续租时长不大于零。
        TaskLeaseLostError: 任务已被恢复/重新领取，或租约已过期。
        TaskQueueError: 数据库更新或提交失败。

    更新使用行锁并在事务中提交；失败时回滚，所以不会留下“内存认为续租成功、
    数据库仍是旧到期时间”的半完成状态。
    """
    if lease_duration.total_seconds() <= 0:
        raise ValueError("lease_duration must be positive")
    now = self._clock()
    renewed_until = now + lease_duration
    with self._sessions() as session:
        try:
            task = _locked_owned_task(session, lease, now)
            task.lease_expires_at = renewed_until
            task.updated_at = now
            session.commit()
            return ReviewTaskLease(
                task_id=lease.task_id,
                review_run_id=lease.review_run_id,
                worker_id=lease.worker_id,
                attempt_count=lease.attempt_count,
                model_attempt_count=lease.model_attempt_count,
                lease_expires_at=renewed_until,
                ci_poll_count=lease.ci_poll_count,
                claimed_from_status=lease.claimed_from_status,
                review_plan_id=lease.review_plan_id,
                force_summary=lease.force_summary,
            )
        except TaskLeaseLostError:
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError("the review task lease could not be renewed") from exc


def mark_waiting_for_ci(self: QueueStorage, lease: ReviewTaskLease) -> None:
    """供兼容测试路径把任务直接推进到 ``waiting_for_ci``。

    生产 Worker 使用 ``store_github_context`` 保存真实 CI；本方法只保留给不注入
    GitHub 读取器的测试和兼容调用。它清除租约并写事件，但绝不会伪造 ``completed``。

    参数：
        lease: 当前 Worker 领取任务时获得的、尚未过期的租约。

    异常：
        TaskLeaseLostError: 任务不再属于该 Worker。
        TaskQueueError: 关联运行不存在，或状态/事件事务无法提交。

    成功后任务不再有租约；本方法不会调用 GitHub、模型或外部消息系统。
    """
    now = self._clock()
    with self._sessions() as session:
        try:
            task, run = _locked_owned_task_with_run(session, lease, now)

            task.execution_status = ExecutionStatus.WAITING_FOR_CI.value
            task.lease_owner = None
            task.lease_expires_at = None
            task.claimed_from_status = None
            task.updated_at = now
            run.execution_status = ExecutionStatus.WAITING_FOR_CI.value
            run.updated_at = now
            _set_workflow_status(
                task,
                run,
                ExecutionStatus.CI,
                now,
            )
            _add_event(
                self,
                session,
                task,
                "review.waiting_for_ci",
                f"waiting-for-ci:{task.attempt_count}",
                now,
            )
            session.commit()
        except (TaskLeaseLostError, TaskQueueError):
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError(
                "the review task could not enter the CI waiting state"
            ) from exc


def retry_or_fail(self: QueueStorage, lease: ReviewTaskLease, error: SafeError) -> None:
    """记录本次处理失败，并根据尝试次数安排重试或最终失败。

    错误对象已经包含稳定代码、重试属性、脱敏说明和安全详情；持久化层再次
    限制说明长度。只有仍持有有效租约的 Worker 才能执行此更新；状态、错误
    信息和事件一次性提交，失败时整体回滚。

    参数：
        lease: 发生异常的那次领取操作对应的租约。
        error: 已完成分类和统一脱敏的安全错误对象。

    异常：
        TaskLeaseLostError: 上报时租约已经失效，旧 Worker 不得覆盖新状态。
        TaskQueueError: 任务/运行读取或事务提交失败。

    当 ``attempt_count < max_attempts`` 时按指数退避重新排队；达到上限时把
    任务和运行都改为 ``failed``。两种结果都会写唯一 Outbox 事件。
    """
    now = self._clock()
    with self._sessions() as session:
        try:
            task, run = _locked_owned_task_with_run(session, lease, now)
            _reschedule_or_fail(
                self,
                session,
                task,
                run,
                now,
                error,
                event_suffix=(
                    f"model-attempt-{task.model_attempt_count}"
                    if lease.review_plan_id is not None
                    else f"attempt-{task.attempt_count}"
                ),
            )
            session.commit()
        except (TaskLeaseLostError, TaskQueueError):
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError(
                "the failed review task could not be recorded"
            ) from exc
