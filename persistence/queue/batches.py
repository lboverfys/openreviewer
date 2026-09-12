"""batches 阶段的有界事务与数据访问。"""

from collections.abc import Mapping
from datetime import timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import SQLAlchemyError

from domain.enums import ExecutionStatus, ModelBatchStatus
from domain.model_review import ModelReviewResult
from domain.security import SafeError
from persistence.models import ModelReviewBatchRecord, ReviewPlanRecord
from persistence.queue.common import (
    _add_event,
    _as_utc,
    _locked_owned_task_with_run,
    _validated_truncation_checkpoint,
)
from persistence.queue.context import QueueStorage
from services.model_review import MAX_MODEL_REVIEW_BATCHES, ModelReviewBatch
from services.task_queue import (
    ModelBatchBusyError,
    ModelBatchLease,
    ModelReviewConflictError,
    ReviewTaskLease,
    StoredModelBatch,
    TaskLeaseLostError,
    TaskQueueError,
)


def ensure_model_batches(
    self: QueueStorage,
    lease: ReviewTaskLease,
    batches: tuple[ModelReviewBatch, ...],
    *,
    agent: str = "default",
) -> tuple[StoredModelBatch, ...]:
    """幂等保存模型批次定义；恢复时不会覆盖已完成结果。

    如果同一 Review Plan 的自动重试使用了新的单批配置，且旧批次尚未
    成功、也没有有效运行租约，则原子删除旧定义并重建；有成功结果或仍在
    执行的批次时保守报告冲突，避免丢失可复用结果或制造重复请求。
    """

    if not agent or len(agent) > 32:
        raise ValueError("model batch agent name is invalid")
    if lease.claimed_from_status is not ExecutionStatus.READY_FOR_REVIEW:
        raise ModelReviewConflictError("当前租约不属于模型审查阶段")
    if lease.review_plan_id is None:
        raise ModelReviewConflictError("模型批次缺少 Review Plan")
    if len(batches) > MAX_MODEL_REVIEW_BATCHES:
        raise ModelReviewConflictError("模型批次数量超过持久化上限")
    now = self._clock()
    numbers = tuple(batch.number for batch in batches)
    if len(numbers) != len(set(numbers)):
        raise ModelReviewConflictError("模型批次号必须唯一")
    with self._sessions() as session:
        try:
            _locked_owned_task_with_run(session, lease, now)
            plan = session.scalar(
                select(ReviewPlanRecord)
                .where(ReviewPlanRecord.id == lease.review_plan_id)
                .with_for_update()
            )
            if plan is None:
                raise ModelReviewConflictError("模型批次关联的计划不存在")
            existing_rows = list(
                session.scalars(
                    select(ModelReviewBatchRecord)
                    .where(
                        ModelReviewBatchRecord.review_plan_id == lease.review_plan_id,
                        ModelReviewBatchRecord.agent == agent,
                    )
                    .order_by(ModelReviewBatchRecord.batch_number.asc())
                    .with_for_update()
                    .limit(MAX_MODEL_REVIEW_BATCHES + 1)
                )
            )
            if len(existing_rows) > MAX_MODEL_REVIEW_BATCHES:
                raise ModelReviewConflictError("已保存的模型批次数量超过上限")
            incoming = {
                batch.number: (
                    batch.total,
                    tuple(unit.unit_key for unit in batch.review_input.units),
                    batch.estimated_input_tokens,
                )
                for batch in batches
            }
            existing = {row.batch_number: row for row in existing_rows}
            definitions_match = set(existing) == set(incoming) and all(
                (
                    row.batch_count,
                    tuple(row.unit_keys),
                    row.estimated_input_tokens,
                )
                == incoming[number]
                for number, row in existing.items()
            )
            if not definitions_match and existing_rows:
                # Agent 配置（尤其是 64K -> 32K 的单批上限）可以在一次
                # 失败后被管理员调整，导致同一计划重新规划出不同切片。旧
                # 的 FAILED/PENDING 定义已经没有可复用结果；只有在没有成功
                # 结果且没有有效运行租约时才允许整体重建，避免删除另一
                # Worker 正在执行或已经成功的批次。
                has_succeeded = any(
                    row.status == ModelBatchStatus.SUCCEEDED.value
                    for row in existing_rows
                )
                has_live_running = any(
                    row.status == ModelBatchStatus.RUNNING.value
                    and row.lease_expires_at is not None
                    and _as_utc(row.lease_expires_at) > _as_utc(now)
                    for row in existing_rows
                )
                if has_succeeded or has_live_running:
                    raise ModelReviewConflictError("模型批次定义与已保存结果不一致")
                session.execute(
                    delete(ModelReviewBatchRecord)
                    .where(
                        ModelReviewBatchRecord.review_plan_id == lease.review_plan_id,
                        ModelReviewBatchRecord.agent == agent,
                    )
                    .execution_options(synchronize_session=False)
                )
                session.flush()
                existing = {}

            for batch in batches:
                unit_keys = [unit.unit_key for unit in batch.review_input.units]
                row = existing.get(batch.number)
                if row is None:
                    row = ModelReviewBatchRecord(
                        id=str(self._uuid_factory()),
                        review_plan_id=lease.review_plan_id,
                        agent=agent,
                        batch_number=batch.number,
                        batch_count=batch.total,
                        unit_keys=unit_keys,
                        estimated_input_tokens=batch.estimated_input_tokens,
                        status=ModelBatchStatus.PENDING.value,
                        attempt_count=0,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    existing[batch.number] = row
            _add_event(
                self,
                session,
                None,
                "review.model.batches_persisted",
                f"{agent}:{lease.review_plan_id}:{len(batches)}",
                now,
                aggregate_id=lease.review_run_id,
                extra_payload={
                    "review_plan_id": lease.review_plan_id,
                    "agent": agent,
                    "batch_count": len(batches),
                },
            )
            session.commit()
            rows = sorted(existing.values(), key=lambda item: item.batch_number)
            return tuple(_stored_model_batch(row) for row in rows)
        except (TaskLeaseLostError, ModelReviewConflictError):
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError(
                "model batch definitions could not be persisted"
            ) from exc


def claim_model_batch(
    self: QueueStorage,
    lease: ReviewTaskLease,
    batch_number: int,
    *,
    agent: str = "default",
    lease_duration: timedelta,
) -> StoredModelBatch:
    """原子领取一个批次；成功批次只读返回。"""

    if lease_duration.total_seconds() <= 0:
        raise ValueError("model batch lease duration must be positive")
    if lease.review_plan_id is None:
        raise ModelReviewConflictError("模型批次缺少 Review Plan")
    now = self._clock()
    with self._sessions() as session:
        try:
            _locked_owned_task_with_run(session, lease, now)
            row = session.scalar(
                select(ModelReviewBatchRecord)
                .where(
                    ModelReviewBatchRecord.review_plan_id == lease.review_plan_id,
                    ModelReviewBatchRecord.agent == agent,
                    ModelReviewBatchRecord.batch_number == batch_number,
                )
                .with_for_update()
            )
            if row is None:
                raise ModelReviewConflictError("模型批次不存在")
            status = ModelBatchStatus(row.status)
            if status is ModelBatchStatus.SUCCEEDED:
                session.commit()
                return _stored_model_batch(row)
            if (
                status is ModelBatchStatus.RUNNING
                and row.lease_expires_at is not None
                and _as_utc(row.lease_expires_at) > _as_utc(now)
            ):
                # 即使 Worker ID 相同，也可能是同名副本或一次重入。调用方
                # 无法区分“自己已领取”和“另一个请求正在执行”，因此必须等待
                # 租约过期或结果落库，绝不能再次调用外部模型。
                expiry = _as_utc(row.lease_expires_at)
                raise ModelBatchBusyError(
                    retry_at=expiry,
                    lease_expires_at=expiry,
                )
            if row.available_at is not None and _as_utc(row.available_at) > _as_utc(
                now
            ):
                available_at = _as_utc(row.available_at)
                raise ModelBatchBusyError(
                    "模型批次尚未到重试时间",
                    retry_at=available_at,
                    available_at=available_at,
                )
            row.status = ModelBatchStatus.RUNNING.value
            row.attempt_count += 1
            row.lease_owner = lease.worker_id
            row.lease_expires_at = now + lease_duration
            row.updated_at = now
            _add_event(
                self,
                session,
                None,
                "review.model.batch_claimed",
                f"{agent}:{lease.review_plan_id}:{batch_number}:{row.attempt_count}",
                now,
                aggregate_id=lease.review_run_id,
                extra_payload={
                    "review_plan_id": lease.review_plan_id,
                    "agent": agent,
                    "batch_number": batch_number,
                    "batch_count": row.batch_count,
                    "attempt_count": row.attempt_count,
                },
            )
            session.commit()
            return _stored_model_batch(row)
        except (
            TaskLeaseLostError,
            ModelReviewConflictError,
            ModelBatchBusyError,
        ):
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError("model batch could not be claimed") from exc


def renew_model_batch(
    self: QueueStorage,
    lease: ReviewTaskLease,
    batch_number: int,
    *,
    agent: str = "default",
    lease_duration: timedelta,
) -> StoredModelBatch:
    """延长当前 Worker 持有的模型批次租约。

    批次租约与任务租约分开存储；模型请求可能长于一次心跳周期，因此必须
    在同一任务所有权检查下单独续期。方法只更新批次行，不写进度事件，避免
    长请求产生无界的审计记录。
    """

    if lease_duration.total_seconds() <= 0:
        raise ValueError("model batch lease duration must be positive")
    if lease.review_plan_id is None:
        raise ModelReviewConflictError("模型批次缺少 Review Plan")
    now = self._clock()
    renewed_until = now + lease_duration
    with self._sessions() as session:
        try:
            _locked_owned_task_with_run(session, lease, now)
            row = session.scalar(
                select(ModelReviewBatchRecord)
                .where(
                    ModelReviewBatchRecord.review_plan_id == lease.review_plan_id,
                    ModelReviewBatchRecord.agent == agent,
                    ModelReviewBatchRecord.batch_number == batch_number,
                )
                .with_for_update()
            )
            if row is None:
                raise ModelReviewConflictError("模型批次不存在")
            if row.status == ModelBatchStatus.SUCCEEDED.value:
                session.commit()
                return _stored_model_batch(row)
            if (
                row.status != ModelBatchStatus.RUNNING.value
                or row.lease_owner != lease.worker_id
                or row.lease_expires_at is None
                or _as_utc(row.lease_expires_at) <= _as_utc(now)
            ):
                raise TaskLeaseLostError("模型批次租约已失效")
            row.lease_expires_at = max(
                _as_utc(row.lease_expires_at),
                _as_utc(renewed_until),
            )
            row.updated_at = now
            session.commit()
            return _stored_model_batch(row)
        except (TaskLeaseLostError, ModelReviewConflictError):
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError("model batch lease could not be renewed") from exc


def renew_model_batches(
    self: QueueStorage,
    lease: ReviewTaskLease,
    batches: tuple[ModelBatchLease, ...],
) -> tuple[StoredModelBatch, ...]:
    """在一个事务中延长当前 Worker 持有的多个模型批次租约。

    固定工作流最多同时运行三路 Agent；批次心跳必须用一次有界查询和一次
    提交完成续租，不能在 Python 循环中逐批访问数据库形成 N+1。
    """

    if not batches:
        return ()
    if lease.review_plan_id is None:
        raise ModelReviewConflictError("模型批次缺少 Review Plan")
    for item in batches:
        if not item.agent or len(item.agent) > 32:
            raise ValueError("model batch agent name is invalid")
        if item.batch_number <= 0:
            raise ValueError("model batch number must be positive")
        if item.lease_duration.total_seconds() <= 0:
            raise ValueError("model batch lease duration must be positive")
    keys = tuple((item.agent, item.batch_number) for item in batches)
    if len(keys) != len(set(keys)):
        raise ValueError("model batch identities must be unique")
    now = self._clock()
    renewed_until = {
        (item.agent, item.batch_number): now + item.lease_duration for item in batches
    }
    predicates = tuple(
        and_(
            ModelReviewBatchRecord.agent == agent,
            ModelReviewBatchRecord.batch_number == batch_number,
        )
        for agent, batch_number in keys
    )
    with self._sessions() as session:
        try:
            _locked_owned_task_with_run(session, lease, now)
            rows = list(
                session.scalars(
                    select(ModelReviewBatchRecord)
                    .where(
                        ModelReviewBatchRecord.review_plan_id == lease.review_plan_id,
                        or_(*predicates),
                    )
                    .with_for_update()
                )
            )
            rows_by_key = {(row.agent, row.batch_number): row for row in rows}
            for key in keys:
                row = rows_by_key.get(key)
                if row is None:
                    raise ModelReviewConflictError("模型批次不存在")
                if row.status == ModelBatchStatus.SUCCEEDED.value:
                    continue
                if (
                    row.status != ModelBatchStatus.RUNNING.value
                    or row.lease_owner != lease.worker_id
                    or row.lease_expires_at is None
                    or _as_utc(row.lease_expires_at) <= _as_utc(now)
                ):
                    raise TaskLeaseLostError("模型批次租约已失效")
                row.lease_expires_at = max(
                    _as_utc(row.lease_expires_at),
                    _as_utc(renewed_until[key]),
                )
                row.updated_at = now
            session.commit()
            return tuple(_stored_model_batch(rows_by_key[key]) for key in keys)
        except (TaskLeaseLostError, ModelReviewConflictError):
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError("model batch leases could not be renewed") from exc


def complete_model_batch(
    self: QueueStorage,
    lease: ReviewTaskLease,
    batch_number: int,
    result: ModelReviewResult,
    *,
    agent: str = "default",
    expected_attempt_count: int | None = None,
) -> StoredModelBatch:
    """保存一次成功结果，并清除批次租约。

    ``expected_attempt_count`` 是领取批次时返回的代次。旧 Worker 即使和
    新 Worker 使用同一个稳定 ID，也不能在租约过期并重新领取后覆盖新结果。
    """

    if lease.review_plan_id is None:
        raise ModelReviewConflictError("模型批次缺少 Review Plan")
    now = self._clock()
    with self._sessions() as session:
        try:
            _locked_owned_task_with_run(session, lease, now)
            row = session.scalar(
                select(ModelReviewBatchRecord)
                .where(
                    ModelReviewBatchRecord.review_plan_id == lease.review_plan_id,
                    ModelReviewBatchRecord.agent == agent,
                    ModelReviewBatchRecord.batch_number == batch_number,
                )
                .with_for_update()
            )
            if row is None:
                raise ModelReviewConflictError("模型批次不存在")
            if row.status == ModelBatchStatus.SUCCEEDED.value:
                session.commit()
                return _stored_model_batch(row)
            if (
                row.status != ModelBatchStatus.RUNNING.value
                or row.lease_owner != lease.worker_id
                or row.lease_expires_at is None
                or _as_utc(row.lease_expires_at) <= _as_utc(now)
                or (
                    expected_attempt_count is not None
                    and row.attempt_count != expected_attempt_count
                )
            ):
                raise TaskLeaseLostError("模型批次租约已失效")
            row.status = ModelBatchStatus.SUCCEEDED.value
            row.lease_owner = None
            row.lease_expires_at = None
            row.request_fingerprint = result.request_fingerprint
            row.provider_request_id = result.provider_request_id
            row.response_status = result.response_status
            row.duration_ms = result.duration_ms
            row.result = result.model_dump(mode="json")
            row.error_code = None
            row.error_message = None
            row.error_details = None
            row.updated_at = now
            _add_event(
                self,
                session,
                None,
                "review.model.batch_persisted",
                f"{agent}:{lease.review_plan_id}:{batch_number}:{result.request_fingerprint}",
                now,
                aggregate_id=lease.review_run_id,
                extra_payload={
                    "review_plan_id": lease.review_plan_id,
                    "agent": agent,
                    "batch_number": batch_number,
                    "response_status": result.response_status,
                    "duration_ms": result.duration_ms,
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "provider_request_id": result.provider_request_id,
                },
            )
            session.commit()
            return _stored_model_batch(row)
        except (TaskLeaseLostError, ModelReviewConflictError):
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError("model batch result could not be persisted") from exc


def checkpoint_model_batch(
    self: QueueStorage,
    lease: ReviewTaskLease,
    batch_number: int,
    checkpoint: Mapping[str, object],
    *,
    agent: str = "default",
    expected_attempt_count: int | None = None,
) -> StoredModelBatch:
    """保存截断拆分的结构化子结果检查点。

    检查点写在批次已有的 ``error_details`` JSON 列中，避免为一次恢复性
    优化引入迁移；它只在批次仍由当前 Worker 持有时更新，过期 Worker
    不能覆盖新代次的检查点。
    """

    if lease.review_plan_id is None:
        raise ModelReviewConflictError("模型批次缺少 Review Plan")
    validated = _validated_truncation_checkpoint(checkpoint)
    now = self._clock()
    with self._sessions() as session:
        try:
            _locked_owned_task_with_run(session, lease, now)
            row = session.scalar(
                select(ModelReviewBatchRecord)
                .where(
                    ModelReviewBatchRecord.review_plan_id == lease.review_plan_id,
                    ModelReviewBatchRecord.agent == agent,
                    ModelReviewBatchRecord.batch_number == batch_number,
                )
                .with_for_update()
            )
            if row is None:
                raise ModelReviewConflictError("模型批次不存在")
            if row.status == ModelBatchStatus.SUCCEEDED.value:
                session.commit()
                return _stored_model_batch(row)
            if (
                row.status != ModelBatchStatus.RUNNING.value
                or row.lease_owner != lease.worker_id
                or row.lease_expires_at is None
                or _as_utc(row.lease_expires_at) <= _as_utc(now)
                or (
                    expected_attempt_count is not None
                    and row.attempt_count != expected_attempt_count
                )
            ):
                raise TaskLeaseLostError("模型批次租约已失效")
            details = (
                dict(row.error_details) if isinstance(row.error_details, dict) else {}
            )
            details["truncation_checkpoint"] = validated
            row.error_details = details
            row.updated_at = now
            session.commit()
            return _stored_model_batch(row)
        except (TaskLeaseLostError, ModelReviewConflictError):
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError(
                "model batch checkpoint could not be persisted"
            ) from exc


def fail_model_batch(
    self: QueueStorage,
    lease: ReviewTaskLease,
    batch_number: int,
    error: SafeError,
    *,
    agent: str = "default",
    retry_delay: timedelta | None = None,
    expected_attempt_count: int | None = None,
) -> StoredModelBatch:
    """保存单批安全错误，供阶段级重试恢复。"""

    if lease.review_plan_id is None:
        raise ModelReviewConflictError("模型批次缺少 Review Plan")
    now = self._clock()
    # 零秒是合法的立即重试退避；不能用 ``or`` 把 ``timedelta(0)``
    # 误当成“未传参数”并替换成默认退避。
    delay = (
        retry_delay
        if retry_delay is not None
        else timedelta(seconds=self._retry_base_seconds)
    )
    if delay.total_seconds() < 0:
        raise ValueError("model batch retry delay cannot be negative")
    with self._sessions() as session:
        try:
            _locked_owned_task_with_run(session, lease, now)
            row = session.scalar(
                select(ModelReviewBatchRecord)
                .where(
                    ModelReviewBatchRecord.review_plan_id == lease.review_plan_id,
                    ModelReviewBatchRecord.agent == agent,
                    ModelReviewBatchRecord.batch_number == batch_number,
                )
                .with_for_update()
            )
            if row is None:
                raise ModelReviewConflictError("模型批次不存在")
            if row.status == ModelBatchStatus.SUCCEEDED.value:
                session.commit()
                return _stored_model_batch(row)
            if row.status == ModelBatchStatus.FAILED.value:
                # 失败上报可能因调用方重入而重复到达；保留第一次的退避和
                # 错误快照，避免重复写事件或缩短退避窗口。
                session.commit()
                return _stored_model_batch(row)
            if (
                row.lease_owner != lease.worker_id
                or row.lease_expires_at is None
                or _as_utc(row.lease_expires_at) <= _as_utc(now)
                or (
                    expected_attempt_count is not None
                    and row.attempt_count != expected_attempt_count
                )
            ):
                raise TaskLeaseLostError("模型批次由其他 Worker 持有")
            row.status = ModelBatchStatus.FAILED.value
            row.lease_owner = None
            row.lease_expires_at = None
            row.available_at = now + delay
            row.error_code = error.code.value
            row.error_message = error.safe_message[:1000]
            details = dict(error.details)
            # 如果截断拆分在本次调用中已经完成了部分子批次，保留检查点，
            # 让下一次领取直接复用这些成功结果，而不是重复请求。
            previous_details = row.error_details
            if isinstance(previous_details, dict):
                checkpoint = previous_details.get("truncation_checkpoint")
                if isinstance(checkpoint, dict):
                    details["truncation_checkpoint"] = checkpoint
            row.error_details = details
            row.updated_at = now
            _add_event(
                self,
                session,
                None,
                "review.model.batch_retry_waiting",
                f"{agent}:{lease.review_plan_id}:{batch_number}:{row.attempt_count}:{error.code.value}",
                now,
                aggregate_id=lease.review_run_id,
                error=error,
                extra_payload={
                    "review_plan_id": lease.review_plan_id,
                    "agent": agent,
                    "batch_number": batch_number,
                    "attempt_count": row.attempt_count,
                    "retry_at": (now + delay).isoformat(),
                },
            )
            session.commit()
            return _stored_model_batch(row)
        except (TaskLeaseLostError, ModelReviewConflictError):
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError("model batch failure could not be persisted") from exc


def load_model_batches(
    self: QueueStorage,
    lease: ReviewTaskLease,
    *,
    agent: str = "default",
) -> tuple[StoredModelBatch, ...]:
    """一次有界查询读取一个 Agent 的所有批次。"""

    if lease.review_plan_id is None:
        raise ModelReviewConflictError("模型批次缺少 Review Plan")
    now = self._clock()
    with self._sessions() as session:
        try:
            _locked_owned_task_with_run(session, lease, now)
            rows = list(
                session.scalars(
                    select(ModelReviewBatchRecord)
                    .where(
                        ModelReviewBatchRecord.review_plan_id == lease.review_plan_id,
                        ModelReviewBatchRecord.agent == agent,
                    )
                    .order_by(ModelReviewBatchRecord.batch_number.asc())
                    .limit(MAX_MODEL_REVIEW_BATCHES + 1)
                )
            )
            if len(rows) > MAX_MODEL_REVIEW_BATCHES:
                raise ModelReviewConflictError("已保存的模型批次数量超过上限")
            return tuple(_stored_model_batch(row) for row in rows)
        except (TaskLeaseLostError, ModelReviewConflictError):
            raise
        except SQLAlchemyError as exc:
            raise TaskQueueError("model batches could not be loaded") from exc


def _stored_model_batch(row: ModelReviewBatchRecord) -> StoredModelBatch:
    result = None
    if row.result is not None:
        try:
            result = ModelReviewResult.model_validate(row.result)
        except (TypeError, ValueError) as exc:
            raise TaskQueueError("已保存的模型批次结果无效") from exc
    checkpoint = None
    if isinstance(row.error_details, dict):
        raw_checkpoint = row.error_details.get("truncation_checkpoint")
        if isinstance(raw_checkpoint, dict):
            checkpoint = dict(raw_checkpoint)
    return StoredModelBatch(
        id=row.id,
        review_plan_id=row.review_plan_id,
        agent=row.agent,
        batch_number=row.batch_number,
        batch_count=row.batch_count,
        unit_keys=tuple(str(item) for item in row.unit_keys),
        estimated_input_tokens=row.estimated_input_tokens,
        status=ModelBatchStatus(row.status),
        attempt_count=row.attempt_count,
        request_fingerprint=row.request_fingerprint,
        result=result,
        error_code=row.error_code,
        error_message=row.error_message,
        checkpoint=checkpoint,
    )
