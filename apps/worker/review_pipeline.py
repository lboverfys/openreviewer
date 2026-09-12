"""审查阶段推进。"""

from __future__ import annotations

import time
from inspect import Parameter, signature
from typing import TYPE_CHECKING

from apps.worker.batches import (
    _model_batch_retry_delay,
    _safe_unsupported_parameters_payload,
)
from apps.worker.heartbeat import (
    _LeaseCursor,
    _propagate_task_lease_loss,
    _raise_if_lease_lost,
)
from apps.worker.logging_context import LOGGER
from domain.enums import ExecutionStatus
from domain.model_review import materialize_findings
from domain.security import ErrorCode, SafeApplicationError, SafeError
from services.ai_settings import ActiveAiRuntime
from services.model_review import (
    combine_model_review_results,
    plan_model_review_batches,
    remap_model_review_result,
)
from services.task_queue import ModelBatchBusyError, TaskQueueError

if TYPE_CHECKING:
    from apps.worker.runtime import WorkerRuntime


def _advance_to_supported_boundary(
    self: WorkerRuntime,
    cursor: _LeaseCursor,
    ai_runtime: ActiveAiRuntime | None = None,
) -> ExecutionStatus:
    """把任务推进到当前版本真正支持的边界。

    配置了 GitHub 上下文读取器时，先一次性延长上下文处理租约，在数据库事务外
    获取 PR、文件和 CI；读取期间由独立心跳线程刷新忙碌状态，随后用短事务保存
    快照并推进状态。
    测试或旧调用方未注入读取器时仍保留兼容的等待边界。

    参数：
        cursor: 保存当前有效租约并能在外部请求之间续期的游标。

    副作用：
        GitHub 路径会保存上下文，并进入等待 CI、可审查、取消或已替代状态；
        模型路径会按配置分批调用，并在全部结果持久化后写成 ``completed``。

    异常：
        TaskLeaseLostError/TaskQueueError: 租约失效或数据库状态转换失败，
        由 ``run_once`` 交给重试/失败处理。
    """

    if cursor.lease.claimed_from_status is ExecutionStatus.READY_FOR_REVIEW:
        if cursor.lease.review_plan_id is not None:
            if ai_runtime is not None and ai_runtime.agent_workflow is not None:
                return self._run_fixed_agent_workflow(cursor, ai_runtime)
            model_reviewer = (
                ai_runtime.reviewer if ai_runtime is not None else self._model_reviewer
            )
            if model_reviewer is None:
                raise TaskQueueError("Worker 未配置模型审查适配器")
            cursor.renew(self._settings.model_review_lease_duration)
            model_input = self._queue.load_model_review_input(cursor.lease)
            model_settings = (
                ai_runtime.model_settings if ai_runtime is not None else None
            )
            if model_settings is not None and model_input.units:
                batches = plan_model_review_batches(
                    model_input,
                    model_settings,
                )
                self._queue.record_model_progress(
                    cursor.lease,
                    "batches_planned",
                    {
                        "batch_count": len(batches),
                        "file_count": len(model_input.units),
                        "context_window_tokens": (model_settings.context_window_tokens),
                        "max_output_tokens": model_settings.max_output_tokens,
                        "input_budget_tokens": (
                            model_settings.batch_input_budget_tokens
                        ),
                        "context_input_budget_tokens": (
                            model_settings.input_budget_tokens
                        ),
                        "max_batch_input_tokens": (
                            model_settings.max_batch_input_tokens
                        ),
                        "reasoning_effort": (model_settings.reasoning_effort.value),
                        "provider": model_settings.provider.value,
                        "api_protocol": (model_settings.resolved_api_protocol.value),
                        "model": model_settings.model,
                    },
                )
                batch_results = []
                persistent_batches = getattr(
                    self._queue,
                    "ensure_model_batches",
                    None,
                )
                stored_batches = (
                    persistent_batches(cursor.lease, batches)
                    if callable(persistent_batches)
                    else ()
                )
                stored_by_number = {item.batch_number: item for item in stored_batches}
                for batch in batches:
                    _raise_if_lease_lost(cursor)
                    cursor.renew(self._settings.model_review_lease_duration)
                    files = batch.files
                    stored = stored_by_number.get(batch.number)
                    batch_result = (
                        stored.result
                        if stored is not None
                        and getattr(stored.status, "value", stored.status)
                        == "succeeded"
                        and stored.result is not None
                        else None
                    )
                    if batch_result is not None:
                        # 已成功批次从数据库恢复，不再次请求外部模型。
                        self._queue.record_model_progress(
                            cursor.lease,
                            "batch_completed",
                            {
                                "batch_number": batch.number,
                                "batch_count": batch.total,
                                "file_count": len(files),
                                "resumed": True,
                                "input_tokens": batch_result.usage.total_input_tokens,
                                "output_tokens": batch_result.usage.output_tokens,
                                "reasoning_tokens": batch_result.usage.reasoning_output_tokens,
                                "duration_ms": batch_result.duration_ms,
                                "finding_count": len(batch_result.output.findings),
                                "provider_request_id": batch_result.provider_request_id,
                            },
                        )
                        batch_results.append(batch_result)
                        continue

                    allowed_attempts = model_settings.max_retries + 1
                    if stored is not None and stored.attempt_count >= allowed_attempts:
                        raise SafeApplicationError(
                            SafeError(
                                code=ErrorCode.MODEL_SERVER_ERROR,
                                safe_message="模型批次已达到最大重试次数",
                                retryable=False,
                                details={
                                    "agent": "default",
                                    "batch_number": batch.number,
                                    "attempt_count": stored.attempt_count,
                                    "max_retries": model_settings.max_retries,
                                    "batch_retry_managed": True,
                                },
                            )
                        )

                    claim_batch = getattr(self._queue, "claim_model_batch", None)
                    claimed_batch = None
                    if callable(claim_batch):
                        try:
                            claim_parameters = signature(
                                claim_batch
                            ).parameters.values()
                            supports_agent = any(
                                parameter.name == "agent"
                                or parameter.kind is Parameter.VAR_KEYWORD
                                for parameter in claim_parameters
                            )
                        except (TypeError, ValueError):
                            supports_agent = True
                        try:
                            claim_kwargs: dict[str, object] = {
                                "lease_duration": self._settings.model_review_lease_duration,
                            }
                            if supports_agent:
                                claim_kwargs["agent"] = "default"
                            claimed_batch = claim_batch(
                                cursor.lease,
                                batch.number,
                                **claim_kwargs,
                            )
                        except TypeError as claim_error:
                            if supports_agent or "agent" not in str(claim_error):
                                raise
                            # 兼容尚未增加 ``agent`` 关键字的旧测试队列。
                            try:
                                claimed_batch = claim_batch(
                                    cursor.lease,
                                    batch.number,
                                    lease_duration=self._settings.model_review_lease_duration,
                                )
                            except ModelBatchBusyError as exc:
                                source_error = SafeError.from_exception(exc)
                                raise SafeApplicationError(
                                    SafeError(
                                        code=source_error.code,
                                        safe_message=source_error.safe_message,
                                        retryable=True,
                                        details={
                                            **dict(source_error.details),
                                            "agent": "default",
                                            "batch_number": batch.number,
                                            "batch_retry_managed": True,
                                        },
                                    )
                                ) from exc
                        except ModelBatchBusyError as exc:
                            source_error = SafeError.from_exception(exc)
                            raise SafeApplicationError(
                                SafeError(
                                    code=source_error.code,
                                    safe_message=source_error.safe_message,
                                    retryable=True,
                                    details={
                                        **dict(source_error.details),
                                        "agent": "default",
                                        "batch_number": batch.number,
                                        "batch_retry_managed": True,
                                    },
                                )
                            ) from exc
                        if (
                            claimed_batch.status.value == "succeeded"
                            and claimed_batch.result is not None
                        ):
                            batch_results.append(claimed_batch.result)
                            self._queue.record_model_progress(
                                cursor.lease,
                                "batch_completed",
                                {
                                    "batch_number": batch.number,
                                    "batch_count": batch.total,
                                    "file_count": len(files),
                                    "resumed": True,
                                    "input_tokens": claimed_batch.result.usage.total_input_tokens,
                                    "output_tokens": claimed_batch.result.usage.output_tokens,
                                    "reasoning_tokens": claimed_batch.result.usage.reasoning_output_tokens,
                                    "duration_ms": claimed_batch.result.duration_ms,
                                    "finding_count": len(
                                        claimed_batch.result.output.findings
                                    ),
                                    "provider_request_id": claimed_batch.result.provider_request_id,
                                },
                            )
                            continue
                    self._queue.record_model_progress(
                        cursor.lease,
                        "batch_started",
                        {
                            "batch_number": batch.number,
                            "batch_count": batch.total,
                            "file_count": len(files),
                            "first_file": files[0],
                            "last_file": files[-1],
                            "estimated_input_tokens": batch.estimated_input_tokens,
                            "fragmented": batch.fragmented,
                        },
                    )
                    cursor.register_model_batch(
                        "default",
                        batch.number,
                        self._settings.model_review_lease_duration,
                    )
                    request_started = time.monotonic()
                    try:
                        self._queue.record_model_progress(
                            cursor.lease,
                            "request_started",
                            {
                                "batch_number": batch.number,
                                "batch_count": batch.total,
                                "estimated_input_tokens": batch.estimated_input_tokens,
                                "provider": model_settings.provider.value,
                                "api_protocol": model_settings.resolved_api_protocol.value,
                                "model": model_settings.model,
                                "reasoning_effort": model_settings.reasoning_effort.value,
                            },
                        )
                        _raise_if_lease_lost(cursor)
                        _raise_if_lease_lost(cursor)
                        batch_result = remap_model_review_result(
                            self._review_with_repository_limit(
                                cursor,
                                model_reviewer,
                                batch.review_input,
                            ),
                            batch,
                        )
                        _raise_if_lease_lost(cursor)
                        complete_batch = getattr(
                            self._queue,
                            "complete_model_batch",
                            None,
                        )
                        if callable(complete_batch):
                            complete_kwargs: dict[str, object] = {}
                            if claimed_batch is not None:
                                complete_kwargs["expected_attempt_count"] = (
                                    claimed_batch.attempt_count
                                )
                            try:
                                complete_batch(
                                    cursor.lease,
                                    batch.number,
                                    batch_result,
                                    **complete_kwargs,
                                )
                            except TypeError:
                                # 兼容尚未增加批次代次参数的旧测试/适配器；
                                # 生产 SQL 队列支持该参数并会执行 CAS 校验。
                                if not complete_kwargs:
                                    raise
                                complete_batch(
                                    cursor.lease,
                                    batch.number,
                                    batch_result,
                                )
                    except Exception as exc:
                        source_error = SafeError.from_exception(exc)
                        # 旧 Worker 失去批次租约后不能再尝试失败回写；
                        # 让 run_once 的租约恢复分支接管该任务。
                        if source_error.code is ErrorCode.TASK_LEASE_LOST:
                            cursor.mark_lease_lost()
                            raise
                        safe_error = SafeError(
                            code=source_error.code,
                            safe_message=source_error.safe_message,
                            retryable=(
                                source_error.retryable
                                and (
                                    claimed_batch is None
                                    or claimed_batch.attempt_count < allowed_attempts
                                )
                            ),
                            details={
                                **dict(source_error.details),
                                "agent": "default",
                                "batch_number": batch.number,
                                "batch_retry_managed": True,
                            },
                        )
                        elapsed_ms = max(
                            0,
                            int((time.monotonic() - request_started) * 1000),
                        )
                        error_duration = safe_error.details.get("duration_ms")
                        failure_payload: dict[str, object] = {
                            "batch_number": batch.number,
                            "batch_count": batch.total,
                            "file_count": len(files),
                            "estimated_input_tokens": batch.estimated_input_tokens,
                            "duration_ms": (
                                error_duration
                                if isinstance(error_duration, int)
                                and not isinstance(error_duration, bool)
                                and error_duration >= 0
                                else elapsed_ms
                            ),
                            "error_code": safe_error.code.value,
                            "error_message": safe_error.safe_message,
                            "error_retryable": safe_error.retryable,
                        }
                        for detail_name in (
                            "status_code",
                            "provider_request_id",
                            "provider",
                            "api_protocol",
                            "model",
                        ):
                            detail_value = safe_error.details.get(detail_name)
                            if detail_value is not None:
                                failure_payload[detail_name] = detail_value
                        unsupported_parameters = _safe_unsupported_parameters_payload(
                            safe_error.details
                        )
                        if unsupported_parameters is not None:
                            failure_payload["unsupported_parameters"] = (
                                unsupported_parameters
                            )
                        # 进程心跳失效会同步锁存 cursor；即使模型本身只
                        # 抛出普通异常，也不能让旧实例继续写批次失败状态。
                        _raise_if_lease_lost(cursor)
                        try:
                            fail_batch = getattr(self._queue, "fail_model_batch", None)
                            if callable(fail_batch):
                                fail_kwargs: dict[str, object] = {
                                    "agent": "default",
                                    "retry_delay": _model_batch_retry_delay(
                                        safe_error,
                                        getattr(claimed_batch, "attempt_count", 1),
                                    ),
                                }
                                if claimed_batch is not None:
                                    fail_kwargs["expected_attempt_count"] = (
                                        claimed_batch.attempt_count
                                    )
                                try:
                                    fail_batch(
                                        cursor.lease,
                                        batch.number,
                                        safe_error,
                                        **fail_kwargs,
                                    )
                                except TypeError:
                                    # 兼容旧队列签名；新 SQL 队列不会走此
                                    # 分支，因此仍能阻止过期批次的旧回写。
                                    if "expected_attempt_count" not in fail_kwargs:
                                        raise
                                    fail_kwargs.pop("expected_attempt_count", None)
                                    fail_batch(
                                        cursor.lease,
                                        batch.number,
                                        safe_error,
                                        **fail_kwargs,
                                    )
                        except TypeError:
                            # 兼容旧队列签名，同时保留原始模型错误。
                            try:
                                fail_batch = getattr(
                                    self._queue, "fail_model_batch", None
                                )
                                if callable(fail_batch):
                                    fail_batch(cursor.lease, batch.number, safe_error)
                            except Exception as fallback_error:
                                _propagate_task_lease_loss(
                                    cursor,
                                    fallback_error,
                                )
                                LOGGER.exception(
                                    "任务 %s 的模型批次失败状态无法持久化",
                                    cursor.lease.task_id,
                                )
                        except Exception as persistence_error:
                            _propagate_task_lease_loss(
                                cursor,
                                persistence_error,
                            )
                            LOGGER.exception(
                                "任务 %s 的模型批次失败状态无法持久化",
                                cursor.lease.task_id,
                            )
                        _raise_if_lease_lost(cursor)
                        try:
                            self._queue.record_model_progress(
                                cursor.lease,
                                "batch_failed",
                                failure_payload,
                            )
                        except Exception as progress_error:
                            _propagate_task_lease_loss(
                                cursor,
                                progress_error,
                            )
                            LOGGER.exception(
                                "任务 %s 的模型批次失败事件无法持久化",
                                cursor.lease.task_id,
                            )
                        raise SafeApplicationError(safe_error) from exc
                    finally:
                        cursor.unregister_model_batch("default", batch.number)
                    batch_results.append(batch_result)
                    self._queue.record_model_progress(
                        cursor.lease,
                        "request_completed",
                        {
                            "batch_number": batch.number,
                            "batch_count": batch.total,
                            "response_status": batch_result.response_status,
                            "duration_ms": batch_result.duration_ms,
                            "provider_request_id": batch_result.provider_request_id,
                        },
                    )
                    self._queue.record_model_progress(
                        cursor.lease,
                        "batch_completed",
                        {
                            "batch_number": batch.number,
                            "batch_count": batch.total,
                            "file_count": len(files),
                            "input_tokens": batch_result.usage.total_input_tokens,
                            "output_tokens": batch_result.usage.output_tokens,
                            "reasoning_tokens": batch_result.usage.reasoning_output_tokens,
                            "duration_ms": batch_result.duration_ms,
                            "finding_count": len(batch_result.output.findings),
                            "provider_request_id": batch_result.provider_request_id,
                        },
                    )
                model_result = combine_model_review_results(
                    model_input,
                    tuple(batch_results),
                )
                _raise_if_lease_lost(cursor)
            else:
                _raise_if_lease_lost(cursor)
                _raise_if_lease_lost(cursor)
                model_result = self._review_with_repository_limit(
                    cursor,
                    model_reviewer,
                    model_input,
                )
            _raise_if_lease_lost(cursor)
            findings = materialize_findings(model_input, model_result.output)
            findings = self._verify_findings(cursor, model_input, findings)
            stored_model = self._queue.store_model_review(
                cursor.lease,
                model_input,
                model_result,
                findings,
                configuration_revision=(
                    ai_runtime.revision if ai_runtime is not None else None
                ),
            )
            return stored_model.execution_status
        planner = ai_runtime.planner if ai_runtime is not None else self._planner
        if self._rule_loader is None or planner is None:
            raise TaskQueueError("Worker 未配置 Review Plan 依赖")
        cursor.renew(self._settings.github_context_lease_duration)
        planning_input = self._queue.load_planning_input(cursor.lease)
        rules = self._rule_loader.load(
            planning_input.target,
            planning_input.files,
        )
        plan = planner.plan(
            planning_input.target,
            planning_input.files,
            rules,
        )
        stored = self._queue.store_review_plan(cursor.lease, rules, plan)
        return stored.execution_status

    if self._context_loader is None:
        self._queue.mark_waiting_for_ci(cursor.lease)
        return ExecutionStatus.WAITING_FOR_CI

    cursor.renew(self._settings.github_context_lease_duration)
    target = self._queue.load_target(cursor.lease)
    context = self._context_loader.load(target)
    return self._queue.store_github_context(
        cursor.lease,
        context,
        ci_poll_interval=self._settings.ci_poll_interval,
        ci_wait_timeout=self._settings.ci_wait_timeout,
    )
