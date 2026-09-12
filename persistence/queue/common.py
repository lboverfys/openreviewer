"""common 阶段的有界事务与数据访问。"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sqlalchemy import or_, select
from sqlalchemy.orm import Load, Session

from domain.enums import ExecutionStatus
from domain.security import ErrorCode, SafeError, redact_sensitive
from persistence.models import OutboxEventRecord, ReviewRunRecord, ReviewTaskRecord
from persistence.queue.context import QueueStorage
from services.task_queue import (
    ModelReviewCheckpointTooLargeError,
    ModelReviewConflictError,
    ReviewTaskLease,
    TaskLeaseLostError,
)


def _task_run_mutation_load_options(
    *,
    include_repository_policy: bool = False,
) -> tuple[Load, Load]:
    """只加载任务和运行状态转换代码实际读取的列。"""

    return (
        Load(ReviewTaskRecord).load_only(
            ReviewTaskRecord.id,
            ReviewTaskRecord.review_run_id,
            ReviewTaskRecord.execution_status,
            ReviewTaskRecord.workflow_status,
            ReviewTaskRecord.workflow_paused_from,
            ReviewTaskRecord.attempt_count,
            ReviewTaskRecord.model_attempt_count,
            ReviewTaskRecord.max_attempts,
            ReviewTaskRecord.claimed_from_status,
            ReviewTaskRecord.ci_poll_count,
            ReviewTaskRecord.ci_wait_started_at,
            ReviewTaskRecord.ci_deadline_at,
            ReviewTaskRecord.first_claimed_at,
            raiseload=True,
        ),
        Load(ReviewRunRecord).load_only(
            ReviewRunRecord.id,
            ReviewRunRecord.review_version_key,
            ReviewRunRecord.installation_id,
            ReviewRunRecord.repository_id,
            ReviewRunRecord.repository,
            ReviewRunRecord.repository_key,
            ReviewRunRecord.pull_request_number,
            ReviewRunRecord.head_sha,
            ReviewRunRecord.execution_status,
            ReviewRunRecord.workflow_status,
            ReviewRunRecord.workflow_paused_from,
            ReviewRunRecord.coverage_status,
            ReviewRunRecord.created_at,
            *(
                (ReviewRunRecord.repository_policy,)
                if include_repository_policy
                else ()
            ),
            raiseload=True,
        ),
    )


def _latest_summary_failed(session: Session, review_run_id: str) -> bool:
    """读取当前运行最近一次汇总终态，判断是否需要强制汇总重试。

    汇总事件数量受详情读取上限约束；这里按时间和自增 ID 取一条，查询始终为
    O(1)。``summary_skipped`` 会覆盖旧的失败事件，避免普通部分重试误触发
    汇总模型。
    """

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


def _as_utc(value: datetime) -> datetime:
    """把数据库时间转换为带 UTC 时区的时间。

    参数：
        value: SQLAlchemy 返回的时间；不同驱动可能返回带时区或不带时区的对象。

    返回：
        带 ``UTC`` 的时间。无时区值按项目约定解释为 UTC；已有其他时区的值会
        换算到同一绝对时刻，而不是简单替换时区标签。

    统一时间后，租约过期和心跳新鲜度比较就不会因为 PostgreSQL/SQLite 驱动差异
    触发 naive/aware ``datetime`` 的运行时异常。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validated_truncation_checkpoint(
    checkpoint: Mapping[str, object],
) -> dict[str, object]:
    """验证并复制拆分检查点，避免把任意不可序列化对象写入 JSON 列。"""

    if not isinstance(checkpoint, Mapping):
        raise ModelReviewConflictError("截断恢复检查点格式无效")
    copied = dict(checkpoint)
    try:
        encoded = json.dumps(
            copied,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ModelReviewConflictError("截断恢复检查点不可序列化") from exc
    if len(encoded) > _MAX_TRUNCATION_CHECKPOINT_BYTES:
        raise ModelReviewCheckpointTooLargeError()
    return copied


_MAX_TRUNCATION_CHECKPOINT_BYTES = 4 * 1024 * 1024


def _locked_owned_task(
    session: Session,
    lease: ReviewTaskLease,
    now: datetime,
) -> ReviewTaskRecord:
    """锁定并验证租约所属的任务。

    这是所有“修改运行中任务”操作共用的所有权检查。除了 ID 关联外，还会
    校验状态、Worker、失败尝试次数、CI 轮询代次和租约过期时间；任一条件
    失败都视为租约丢失。

    参数：
        session: 已在事务中的 SQLAlchemy 会话。
        lease: 调用方持有的租约快照。
        now: 本次操作统一使用的当前 UTC 时间。

    返回：
        被 ``FOR UPDATE`` 锁定且通过所有权检查的任务 ORM 对象；调用方可以在
        同一事务中安全修改它。

    异常：
        TaskLeaseLostError: 任务不存在、状态不是 ``running``、Worker/运行、失败
        尝试次数或 CI 轮询代次不匹配，或租约为空/已过期。

    这个私有方法故意集中所有权条件，避免续租、成功推进和失败上报各自漏掉
    某个检查而产生旧 Worker 覆盖新 Worker 的竞态。
    """
    statement = (
        select(ReviewTaskRecord)
        .join(
            ReviewRunRecord,
            ReviewRunRecord.id == ReviewTaskRecord.review_run_id,
        )
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
            or_(
                ReviewRunRecord.workflow_status.is_(None),
                ReviewRunRecord.workflow_status != ExecutionStatus.PAUSED.value,
            ),
            ReviewRunRecord.execution_status == ExecutionStatus.RUNNING.value,
            ReviewTaskRecord.lease_expires_at.is_not(None),
            ReviewTaskRecord.lease_expires_at > now,
        )
        .options(
            Load(ReviewTaskRecord).load_only(
                ReviewTaskRecord.id,
                raiseload=True,
            )
        )
        .with_for_update()
    )
    task = session.scalar(statement)
    if task is None:
        raise TaskLeaseLostError("the worker no longer owns this review task")
    return task


def _locked_owned_task_with_run(
    session: Session,
    lease: ReviewTaskLease,
    now: datetime,
    *,
    include_repository_policy: bool = False,
) -> tuple[ReviewTaskRecord, ReviewRunRecord]:
    """通过一次命中索引的 JOIN 查询锁定所属任务及其运行记录。"""

    statement = (
        select(ReviewTaskRecord, ReviewRunRecord)
        .join(
            ReviewRunRecord,
            ReviewRunRecord.id == ReviewTaskRecord.review_run_id,
        )
        .where(
            ReviewTaskRecord.id == lease.task_id,
            ReviewTaskRecord.review_run_id == lease.review_run_id,
            ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
            ReviewTaskRecord.lease_owner == lease.worker_id,
            ReviewTaskRecord.attempt_count == lease.attempt_count,
            ReviewTaskRecord.model_attempt_count == lease.model_attempt_count,
            ReviewTaskRecord.ci_poll_count == lease.ci_poll_count,
            ReviewTaskRecord.claimed_from_status == lease.claimed_from_status.value,
            # 暂停是独立的人工门。即使旧数据在暂停时错误地保留了
            # ``execution_status=running``，也不能让旧 Worker 继续写入
            # 批次、预算或进度；与 _locked_owned_task 保持同一所有权条件。
            or_(
                ReviewTaskRecord.workflow_status.is_(None),
                ReviewTaskRecord.workflow_status != ExecutionStatus.PAUSED.value,
            ),
            or_(
                ReviewRunRecord.workflow_status.is_(None),
                ReviewRunRecord.workflow_status != ExecutionStatus.PAUSED.value,
            ),
            ReviewRunRecord.execution_status == ExecutionStatus.RUNNING.value,
            ReviewTaskRecord.lease_expires_at.is_not(None),
            ReviewTaskRecord.lease_expires_at > now,
        )
        .options(
            *_task_run_mutation_load_options(
                include_repository_policy=include_repository_policy,
            )
        )
        .with_for_update()
    )
    row = session.execute(statement).one_or_none()
    if row is None:
        raise TaskLeaseLostError("the worker no longer owns this review task")
    return row[0], row[1]


def _set_owned_status(
    task: ReviewTaskRecord,
    run: ReviewRunRecord,
    status: ExecutionStatus,
    now: datetime,
) -> None:
    task.execution_status = status.value
    task.workflow_paused_from = None
    task.lease_owner = None
    task.lease_expires_at = None
    task.claimed_from_status = None
    task.updated_at = now
    run.execution_status = status.value
    run.workflow_paused_from = None
    run.updated_at = now


def _set_workflow_status(
    task: ReviewTaskRecord,
    run: ReviewRunRecord,
    status: ExecutionStatus,
    now: datetime,
) -> None:
    """更新新 DAG 状态，不改变兼容队列使用的 execution_status。"""

    task.workflow_status = status.value
    run.workflow_status = status.value
    if status is not ExecutionStatus.PAUSED:
        task.workflow_paused_from = None
        run.workflow_paused_from = None
    task.updated_at = now
    run.updated_at = now


def _reschedule_or_fail(
    self: QueueStorage,
    session: Session,
    task: ReviewTaskRecord,
    run: ReviewRunRecord,
    now: datetime,
    error: SafeError,
    *,
    event_suffix: str,
) -> None:
    """在当前事务内选择重试或最终失败，并追加对应 Outbox 事件。

    ``attempt_count`` 已在领取时递增，因此达到 ``max_attempts`` 就直接失败；
    否则把任务放回队列，并按指数退避计算下一次可用时间。调用者负责在外层
    事务中提交或回滚。

    参数：
        session: 当前外层事务会话。
        task: 已锁定且确认属于当前操作的运行中任务。
        now: 本次状态变化统一使用的时间。
        error: 要拆分写入结构化错误字段的安全错误对象。
        event_suffix: 附加到事件唯一键的尝试/恢复标识，防止同一状态事件重复。

    副作用：
        清除租约并更新任务和关联运行；未耗尽尝试时设置下一次
        ``available_at``，耗尽时设置 ``failed``；最后把事件对象加入当前会话。

    异常：
        TaskQueueError: 找不到关联运行。此方法不提交事务，异常由调用方负责回滚。
    """
    claimed_from = ExecutionStatus(
        task.claimed_from_status or ExecutionStatus.QUEUED.value
    )
    is_model_stage = (
        claimed_from is ExecutionStatus.READY_FOR_REVIEW
        and task.model_attempt_count > 0
    )
    batch_retry_managed = (
        is_model_stage
        and error.code
        in {ErrorCode.MODEL_BATCH_BUSY, ErrorCode.RETRIEVAL_INDEX_PENDING}
        and error.details.get("batch_retry_managed") is True
    )
    if batch_retry_managed:
        # 批次忙碌或等待代码索引表示本次领取没有发出审查模型请求。其他批次错误
        # （超时、解析失败等）确实已经完成了一次模型尝试，不能回退
        # model_attempt_count，否则会绕过任务级重试上限或改变后续阶段判断。
        task.model_attempt_count = max(0, task.model_attempt_count - 1)
    active_attempt_count = (
        task.model_attempt_count if is_model_stage else task.attempt_count
    )
    task.last_error = error.safe_message[:4000]
    task.last_error_code = error.code.value
    task.last_error_retryable = error.retryable
    task.last_error_details = dict(error.details)
    task.lease_owner = None
    task.lease_expires_at = None
    task.claimed_from_status = None
    task.updated_at = now
    if not error.retryable or (
        active_attempt_count >= task.max_attempts and not batch_retry_managed
    ):
        task.execution_status = ExecutionStatus.FAILED.value
        run.execution_status = ExecutionStatus.FAILED.value
        _set_workflow_status(task, run, ExecutionStatus.FAILED, now)
        event_type = "review.task.failed"
        event_key = f"failed:{event_suffix}"
        event_payload: dict[str, object] | None = None
    else:
        retry_status = (
            claimed_from
            if claimed_from
            in {
                ExecutionStatus.QUEUED,
                ExecutionStatus.WAITING_FOR_CI,
                ExecutionStatus.READY_FOR_REVIEW,
            }
            else ExecutionStatus.QUEUED
        )
        task.execution_status = retry_status.value
        run.execution_status = retry_status.value
        retry_at = _batch_retry_at(error, now)
        default_retry_at = now + _retry_delay(self, active_attempt_count)
        # 批次忙碌/退避时间来自数据库中的持久化状态，不能被普通任务的
        # 5/10/20 秒退避提前覆盖；同时保留默认退避作为最小间隔，避免
        # 已经过期或时钟轻微回拨时立即忙轮询。
        task.available_at = max(
            default_retry_at,
            retry_at if retry_at is not None else default_retry_at,
        )
        event_type = "review.task.retry_scheduled"
        event_key = f"retry:{event_suffix}"
        event_payload = {
            "retry_at": task.available_at.isoformat(),
            "retry_delay_seconds": max(
                0,
                int((task.available_at - now).total_seconds()),
            ),
        }
    run.updated_at = now
    _add_event(
        self,
        session,
        task,
        event_type,
        event_key,
        now,
        error=error,
        extra_payload=event_payload,
    )


def _retry_delay(self: QueueStorage, attempt_count: int) -> timedelta:
    """根据已消耗的尝试次数计算指数退避时长。

    参数：
        attempt_count: 领取时已经递增后的尝试次数；第一次失败传 1。

    返回：
        ``base * 2 ** (attempt_count - 1)`` 秒，但不会超过配置的 cap。即默认
        产生 5、10、20 秒等延迟，直到上限 300 秒。

    负数或零不会产生负延迟：指数使用 ``max(0, attempt_count - 1)``，便于
    数据修复或测试传入边界值时保持安全。
    """
    # 先限制指数再做幂运算，避免损坏数据中的超大计数造成巨大整数。
    exponent = min(8, max(0, attempt_count - 1))
    seconds = min(
        self._retry_cap_seconds,
        self._retry_base_seconds * (2**exponent),
    )
    return timedelta(seconds=seconds)


def _batch_retry_at(
    error: SafeError,
    now: datetime,
) -> datetime | None:
    """读取批次级错误携带的下一次可尝试时间。

    只有 ``MODEL_BATCH_BUSY`` 且明确标记为批次管理的错误才允许影响任务
    调度；其他错误详情即使包含同名字段也不会把任务任意推迟。
    """

    if (
        error.code
        not in {ErrorCode.MODEL_BATCH_BUSY, ErrorCode.RETRIEVAL_INDEX_PENDING}
        or error.details.get("batch_retry_managed") is not True
    ):
        return None
    raw_retry_at = error.details.get("retry_at")
    if not isinstance(raw_retry_at, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw_retry_at)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    if parsed <= _as_utc(now):
        return None
    return parsed


def _add_event(
    self: QueueStorage,
    session: Session,
    task: ReviewTaskRecord | None,
    event_type: str,
    key_suffix: str,
    occurred_at: datetime,
    *,
    error: SafeError | None = None,
    extra_payload: Mapping[str, object] | None = None,
    aggregate_id: str | None = None,
) -> None:
    """在当前事务中追加一条不可重复的任务状态 Outbox 事件。

    事件只携带运行 ID、任务 ID 和尝试次数等非敏感元数据；具体发布器可以在
    后续阶段读取 ``outbox_events``，而不会影响任务状态事务的原子性。

    参数：
        session: 当前状态事务使用的会话；事件只加入会话，不在此处单独提交。
        task: 事件关联的任务记录。
        event_type: 稳定的事件类型，例如 ``review.task.running``。
        key_suffix: 与任务 ID 拼接成唯一 ``event_key`` 的后缀。
        occurred_at: 事件发生时间，由调用方统一提供。

    副作用：
        向会话加入一条尚未发布的 ``OutboxEventRecord``，发布尝试次数初始化为
        0。外部发布器稍后可以读取并投影到 SSE、通知或其他系统。

    该方法不会访问网络，也不会把 ``last_error`` 放入事件 payload，避免事件
    总线携带可能敏感的异常文本。
    """
    payload: dict[str, object] = {}
    if task is not None:
        payload.update(
            {
                "review_run_id": task.review_run_id,
                "review_task_id": task.id,
                "attempt_count": task.attempt_count,
                "model_attempt_count": task.model_attempt_count,
                "ci_poll_count": task.ci_poll_count,
            }
        )
    if aggregate_id is None:
        aggregate_id = task.review_run_id if task is not None else "unknown"
    if error is not None:
        payload.update(
            {
                "error_code": error.code.value,
                "error_message": redact_sensitive(error.safe_message),
                "error_retryable": error.retryable,
            }
        )
    if extra_payload is not None:
        payload.update(extra_payload)
    event_id = str(self._uuid_factory())
    event_identity = (
        f"{event_type}:{task.id if task is not None else aggregate_id}:"
        f"{key_suffix}:{event_id}"
    ).encode()
    session.add(
        OutboxEventRecord(
            id=event_id,
            # 状态事务本身保证同一次转换只提交一次；事件 ID 区分人工重试后
            # 计数器重新从 1 开始的全新转换，避免旧事件键阻断整个 Worker。
            event_key=f"review.task.event:{sha256(event_identity).hexdigest()}",
            aggregate_type="review_run",
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
            occurred_at=occurred_at,
            publish_attempts=0,
        )
    )
