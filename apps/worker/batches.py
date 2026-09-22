"""Worker batches 职责模块。"""

import json
import time
import traceback
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from typing import NoReturn

from apps.worker.heartbeat import (
    _LeaseCursor,
    _propagate_task_lease_loss,
    _raise_if_lease_lost,
)
from apps.worker.logging_context import LOGGER
from domain.enums import ReviewAgent
from domain.logging import log_context
from domain.model_review import ModelReviewInput, ModelReviewResult
from domain.review_planning import ReviewUnit
from domain.security import ErrorCode, SafeApplicationError, SafeError
from services.agent_workflow import _PartialAgentReviewError
from services.model_budget import model_budget_scope, model_request_scope
from services.model_review import (
    ModelReviewBatch,
    ModelReviewer,
    ModelServiceSettings,
    combine_model_review_results,
    plan_model_review_batches,
    remap_model_review_result,
)
from services.task_queue import (
    ModelBatchBusyError,
    ModelReviewCheckpointTooLargeError,
    ModelReviewInputError,
    ReviewTaskQueue,
)


class _PersistentBatchedReviewer:
    """把单个 Agent 的模型调用包裹成可恢复的持久化批次。"""

    def __init__(
        self,
        queue: ReviewTaskQueue,
        lease_cursor: _LeaseCursor,
        agent: ReviewAgent,
        reviewer: ModelReviewer,
        settings: ModelServiceSettings,
        lease_duration: timedelta,
        request_guard: Callable[[], None] | None = None,
    ) -> None:
        self._queue = queue
        self._lease_cursor = lease_cursor
        self._agent = agent
        self._reviewer = reviewer
        self._settings = settings
        self._lease_duration = lease_duration
        self._request_guard = request_guard

    def _raise_partial_batch_error(
        self,
        review_input: ModelReviewInput,
        results: list[ModelReviewResult],
        completed_batches: list[int],
        failed_batch: int,
        error: BaseException,
    ) -> NoReturn:
        """批次失败时把已经完成的结果交给工作流继续做部分汇总。"""

        safe_error = SafeError.from_exception(error)
        if safe_error.code is ErrorCode.TASK_LEASE_LOST or not results:
            raise error
        try:
            partial_result = combine_model_review_results(
                review_input,
                tuple(results),
            )
        except Exception:
            # 结果合并失败时保留原始批次错误，不能用辅助路径遮蔽根因。
            raise error from None
        raise _PartialAgentReviewError(
            safe_error,
            partial_result=partial_result,
            completed_batches=tuple(completed_batches),
            failed_batch=failed_batch,
        ) from error

    def review(self, review_input: ModelReviewInput) -> ModelReviewResult:
        accountant_factory = getattr(self._queue, "monthly_accountant", None)
        accountant = (
            accountant_factory(lambda: self._lease_cursor.lease, self._agent.value)
            if accountant_factory
            else None
        )
        egress_factory = getattr(self._queue, "model_egress_context", None)
        egress = (egress_factory(review_input.repository, review_input.review_run_id)
                  if egress_factory else nullcontext())
        with log_context(agent=self._agent.value), egress, model_request_scope(self._request_guard), model_budget_scope(accountant):
            from services.egress import check_paths, check_texts
            from services.review_reuse import (
                restore_reused,
                reusable_payload,
                reuse_identity,
            )

            check_paths(tuple(unit.file for unit in review_input.units)
                        + tuple(rule.path for rule in review_input.rules)
                        + tuple(item.file for item in review_input.context_evidence)
                        + tuple(review_input.knowledge_versions))
            check_texts(tuple(unit.patch for unit in review_input.units)
                        + tuple(rule.content for rule in review_input.rules)
                        + tuple(item.content for item in review_input.context_evidence)
                        + review_input.knowledge_references)
            identity = reuse_identity(review_input, self._settings)
            loader = getattr(self._queue, "load_reused_review", None)
            writer = getattr(self._queue, "store_reusable_review", None)
            existing = self._queue.load_model_batches(self._lease_cursor.lease, agent=self._agent.value) if identity else ()
            if len(existing) == 1 and existing[0].status.value == "succeeded" and existing[0].result is not None and existing[0].result.reused_from_run_id:
                # 当前任务中已经封存的整 Agent 复用结果优先恢复，不能改为普通分批后制造定义冲突。
                _raise_if_lease_lost(self._lease_cursor)
                return existing[0].result
            if identity and loader and not existing:
                saved = loader(self._lease_cursor.lease, identity.key)
                if saved and saved[1] != review_input.head_sha:
                    result = restore_reused(saved[2], identity, saved[0], review_input.head_sha)
                    lease = self._lease_cursor.lease
                    self._queue.ensure_model_batches(lease, (ModelReviewBatch(1, 1, review_input, 0),), agent=self._agent.value)
                    claimed = self._queue.claim_model_batch(lease, 1, agent=self._agent.value, lease_duration=self._lease_duration)
                    stored = self._queue.complete_model_batch(lease, 1, result, agent=self._agent.value, expected_attempt_count=claimed.attempt_count)
                    result = stored.result or result
                    self._queue.record_model_progress(self._lease_cursor.lease, "incremental_reused",
                        {"agent": self._agent.value, "source_run_id": saved[0],
                         "input_hash": identity.key, "reused_input_tokens": result.reused_input_tokens,
                         "model_requests": 0}, agent=self._agent.value)
                    return result
            result = self._review_batches(review_input)
            if identity and writer:
                payload = reusable_payload(result, identity, review_input.head_sha)
                if payload is not None:
                    writer(self._lease_cursor.lease, identity.key, review_input.head_sha, payload)
            return result

    def _review_batches(self, review_input: ModelReviewInput) -> ModelReviewResult:
        _raise_if_lease_lost(self._lease_cursor)
        # 正式批次的截断恢复由 Worker 缩小输入；供应商适配器不得把同一
        # 大请求改成 8K 后再次发送。旧的直接适配器调用仍保留兼容行为。
        review_input = review_input.model_copy(update={"allow_truncation_retry": False})
        try:
            batches = plan_model_review_batches(review_input, self._settings)
        except ValueError as exc:
            capacity_error = str(exc).startswith((
                "model input budget", "repository rules leave",
                "planned model batch exceeds", "model review batch count exceeds",
            ))
            raise ModelReviewInputError(
                "审查输入无法按当前容量分批，请检查模型的上下文和单批输入上限"
                if capacity_error else
                "审查输入准备失败，请检查关联文件顺序、输入结构与配置快照"
            ) from exc
        if not batches:
            _raise_if_lease_lost(self._lease_cursor)
            # 在最后一刻再检查一次，避免租约已丢失后调用模型。
            _raise_if_lease_lost(self._lease_cursor)
            return self._reviewer.review(review_input)
        _raise_if_lease_lost(self._lease_cursor)
        self._queue.record_model_progress(
            self._lease_cursor.lease,
            "batches_planned",
            {
                "agent": self._agent.value,
                "batch_count": len(batches),
                "file_count": len(review_input.units),
                "context_window_tokens": self._settings.context_window_tokens,
                "input_budget_tokens": self._settings.batch_input_budget_tokens,
                "context_input_budget_tokens": self._settings.input_budget_tokens,
                "max_batch_input_tokens": self._settings.max_batch_input_tokens,
                "reasoning_effort": self._settings.reasoning_effort.value,
                "provider": self._settings.provider.value,
                "api_protocol": self._settings.resolved_api_protocol.value,
                "model": self._settings.model,
            },
            agent=self._agent.value,
        )
        _raise_if_lease_lost(self._lease_cursor)
        stored = self._queue.ensure_model_batches(
            self._lease_cursor.lease,
            batches,
            agent=self._agent.value,
        )
        stored_by_number = {item.batch_number: item for item in stored}
        results: list[ModelReviewResult] = []
        completed_batches: list[int] = []
        for batch in batches:
            _raise_if_lease_lost(self._lease_cursor)
            self._lease_cursor.renew(self._lease_duration)
            current_lease = self._lease_cursor.lease
            saved = stored_by_number.get(batch.number)
            if (
                saved is not None
                and saved.status.value == "succeeded"
                and saved.result is not None
            ):
                result = saved.result
                _raise_if_lease_lost(self._lease_cursor)
                self._queue.record_model_progress(
                    current_lease,
                    "batch_completed",
                    {
                        "agent": self._agent.value,
                        "batch_number": batch.number,
                        "batch_count": batch.total,
                        "file_count": len(batch.files),
                        "resumed": True,
                        "input_tokens": result.usage.total_input_tokens,
                        "output_tokens": result.usage.output_tokens,
                        "reasoning_tokens": result.usage.reasoning_output_tokens,
                        "duration_ms": result.duration_ms,
                        "finding_count": len(result.output.findings),
                        "provider_request_id": result.provider_request_id,
                    },
                    agent=self._agent.value,
                )
                results.append(result)
                completed_batches.append(batch.number)
                continue
            allowed_attempts = self._settings.max_retries + 1
            if saved is not None and saved.attempt_count >= allowed_attempts:
                error = SafeApplicationError(
                    SafeError(
                        code=ErrorCode.MODEL_SERVER_ERROR,
                        safe_message="模型批次已达到最大重试次数",
                        retryable=False,
                        details={
                            "agent": self._agent.value,
                            "batch_number": batch.number,
                            "attempt_count": saved.attempt_count,
                            "max_retries": self._settings.max_retries,
                            "batch_retry_managed": True,
                        },
                    )
                )
                self._raise_partial_batch_error(
                    review_input,
                    results,
                    completed_batches,
                    batch.number,
                    error,
                )
            try:
                _raise_if_lease_lost(self._lease_cursor)
                claimed_batch = self._queue.claim_model_batch(
                    current_lease,
                    batch.number,
                    agent=self._agent.value,
                    lease_duration=self._lease_duration,
                )
            except ModelBatchBusyError as exc:
                # 批次由另一个 Worker 执行时不能把它当成普通任务失败；保留
                # 批次级标记和队列提供的 retry_at，让外层只重新排队等待。
                source_error = SafeError.from_exception(exc)
                safe_error = SafeError(
                    code=source_error.code,
                    safe_message=source_error.safe_message,
                    retryable=True,
                    details={
                        **dict(source_error.details),
                        "agent": self._agent.value,
                        "batch_number": batch.number,
                        "batch_retry_managed": True,
                    },
                )
                error = SafeApplicationError(safe_error)
                self._raise_partial_batch_error(
                    review_input,
                    results,
                    completed_batches,
                    batch.number,
                    error,
                )
            if (
                claimed_batch.status.value == "succeeded"
                and claimed_batch.result is not None
            ):
                # 另一个 Worker 可能在本地快照之后完成了批次；读取其结果，
                # 不再重复发起外部模型请求。
                results.append(claimed_batch.result)
                completed_batches.append(batch.number)
                _raise_if_lease_lost(self._lease_cursor)
                self._queue.record_model_progress(
                    current_lease,
                    "batch_completed",
                    {
                        "agent": self._agent.value,
                        "batch_number": batch.number,
                        "batch_count": batch.total,
                        "file_count": len(batch.files),
                        "resumed": True,
                        "input_tokens": claimed_batch.result.usage.total_input_tokens,
                        "output_tokens": claimed_batch.result.usage.output_tokens,
                        "reasoning_tokens": claimed_batch.result.usage.reasoning_output_tokens,
                        "duration_ms": claimed_batch.result.duration_ms,
                        "finding_count": len(claimed_batch.result.output.findings),
                        "provider_request_id": claimed_batch.result.provider_request_id,
                    },
                    agent=self._agent.value,
                )
                continue
            batch_checkpoint_key = _truncation_input_key(batch.review_input)
            checkpoint_results, checkpoint_split_nodes = _load_truncation_checkpoint(
                getattr(claimed_batch, "checkpoint", None),
                root_key=batch_checkpoint_key,
            )
            if not checkpoint_results and saved is not None:
                checkpoint_results, checkpoint_split_nodes = (
                    _load_truncation_checkpoint(
                        getattr(saved, "checkpoint", None),
                        root_key=batch_checkpoint_key,
                    )
                )
            checkpoint_persistence_disabled = False

            def persist_checkpoint(
                key: str,
                checkpoint_result: ModelReviewResult | None = None,
                *,
                split: bool = False,
                _batch_number: int = batch.number,
                _checkpoint_results: dict[str, ModelReviewResult] = checkpoint_results,
                _checkpoint_split_nodes: set[str] = checkpoint_split_nodes,
                _attempt_count: int = claimed_batch.attempt_count,
                _root_key: str = batch_checkpoint_key,
            ) -> None:
                nonlocal checkpoint_persistence_disabled
                if checkpoint_result is not None:
                    _checkpoint_results[key] = checkpoint_result
                # ``None`` 是递归器用于标记“该节点已截断并已拆分”的哨兵；
                # 直接成功的模型结果始终是 ModelReviewResult。
                if split or checkpoint_result is None:
                    _checkpoint_split_nodes.add(key)
                checkpoint_writer = getattr(
                    self._queue,
                    "checkpoint_model_batch",
                    None,
                )
                if not callable(checkpoint_writer):
                    # 兼容尚未实现检查点接口的测试/旧队列；生产 SQL 队列
                    # 始终支持该接口，成功子批次不会因此丢失恢复能力。
                    return
                if checkpoint_persistence_disabled:
                    return
                try:
                    checkpoint_writer(
                        self._lease_cursor.lease,
                        _batch_number,
                        _dump_truncation_checkpoint(
                            _checkpoint_results,
                            _checkpoint_split_nodes,
                            root_key=_root_key,
                        ),
                        agent=self._agent.value,
                        expected_attempt_count=_attempt_count,
                    )
                except ModelReviewCheckpointTooLargeError:
                    # 检查点只是优化项；超过有界 JSON 列时继续使用内存中的
                    # 子结果，不能把已经成功的模型请求改报为失败。
                    checkpoint_persistence_disabled = True
                    LOGGER.warning(
                        "Agent %s 批次 %s 截断检查点超过安全大小，"
                        "本次调用降级为不持久化检查点",
                        self._agent.value,
                        _batch_number,
                    )

            _raise_if_lease_lost(self._lease_cursor)
            self._queue.record_model_progress(
                current_lease,
                "batch_started",
                {
                    "agent": self._agent.value,
                    "batch_number": batch.number,
                    "batch_count": batch.total,
                    "file_count": len(batch.files),
                    "first_file": batch.files[0],
                    "last_file": batch.files[-1],
                    "estimated_input_tokens": batch.estimated_input_tokens,
                    "fragmented": batch.fragmented,
                },
                agent=self._agent.value,
            )
            self._lease_cursor.register_model_batch(
                self._agent.value,
                batch.number,
                self._lease_duration,
            )
            request_started = time.monotonic()
            try:
                _raise_if_lease_lost(self._lease_cursor)
                self._queue.record_model_progress(
                    current_lease,
                    "request_started",
                    {
                        "agent": self._agent.value,
                        "batch_number": batch.number,
                        "batch_count": batch.total,
                        "estimated_input_tokens": batch.estimated_input_tokens,
                        "provider": self._settings.provider.value,
                        "api_protocol": self._settings.resolved_api_protocol.value,
                        "model": self._settings.model,
                        "reasoning_effort": self._settings.reasoning_effort.value,
                    },
                    agent=self._agent.value,
                )
                _raise_if_lease_lost(self._lease_cursor)
                result = remap_model_review_result(
                    self._review_with_truncation_split(
                        batch.review_input.model_copy(update={"evaluation_batch_number": batch.number}),
                        checkpoint_results=checkpoint_results,
                        checkpoint_split_nodes=checkpoint_split_nodes,
                        on_checkpoint=persist_checkpoint,
                    ),
                    batch,
                )
                _raise_if_lease_lost(self._lease_cursor)
                self._queue.complete_model_batch(
                    self._lease_cursor.lease,
                    batch.number,
                    result,
                    agent=self._agent.value,
                    expected_attempt_count=claimed_batch.attempt_count,
                )
            except Exception as exc:
                source_error = SafeError.from_exception(exc)
                if source_error.code is ErrorCode.WORKER_UNEXPECTED_ERROR:
                    # 仅记录代码位置，避免异常文本携带模型正文、SQL 参数或密钥。
                    LOGGER.error("Agent %s 批次 %s 未预期异常 %s，代码位置=%s",
                                 self._agent.value, batch.number, type(exc).__name__,
                                 [(frame.name, frame.lineno) for frame in traceback.extract_tb(exc.__traceback__)[-8:]])
                # 租约失效不是模型请求失败。此时旧 Worker 已经无权把批次
                # 标记为失败或写入进度；直接向外传播，避免额外的数据库写入
                # 和把租约错误伪装成普通 Agent 失败。
                if source_error.code is ErrorCode.TASK_LEASE_LOST:
                    self._lease_cursor.mark_lease_lost()
                    raise
                safe_error = SafeError(
                    code=source_error.code,
                    safe_message=source_error.safe_message,
                    retryable=(
                        source_error.retryable
                        and claimed_batch.attempt_count < allowed_attempts
                    ),
                    details={
                        **dict(source_error.details),
                        "agent": self._agent.value,
                        "batch_number": batch.number,
                        "attempt_count": claimed_batch.attempt_count,
                        "max_retries": self._settings.max_retries,
                        "batch_retry_managed": True,
                    },
                )
                elapsed_ms = max(
                    0,
                    int((time.monotonic() - request_started) * 1000),
                )
                failure_payload: dict[str, object] = {
                    "agent": self._agent.value,
                    "batch_number": batch.number,
                    "batch_count": batch.total,
                    "file_count": len(batch.files),
                    "duration_ms": (
                        safe_error.details.get("duration_ms")
                        if isinstance(safe_error.details.get("duration_ms"), int)
                        else elapsed_ms
                    ),
                    "error_code": safe_error.code.value,
                    "error_message": safe_error.safe_message,
                    "error_retryable": safe_error.retryable,
                }
                for name in (
                    "status_code",
                    "provider_request_id",
                    "provider",
                    "api_protocol",
                    "model",
                ):
                    if safe_error.details.get(name) is not None:
                        failure_payload[name] = safe_error.details[name]
                unsupported_parameters = _safe_unsupported_parameters_payload(
                    safe_error.details
                )
                if unsupported_parameters is not None:
                    failure_payload["unsupported_parameters"] = unsupported_parameters
                # 心跳线程可能在模型异常返回前已经观察到任务/进程租约失效。
                # 在失败状态写入前再次检查，避免稳定 worker_id 仍匹配时由旧
                # 实例把批次改成 failed；队列本身只校验任务租约，不能替代这
                # 个进程内的失效标记。
                _raise_if_lease_lost(self._lease_cursor)
                try:
                    self._queue.fail_model_batch(
                        self._lease_cursor.lease,
                        batch.number,
                        safe_error,
                        agent=self._agent.value,
                        expected_attempt_count=claimed_batch.attempt_count,
                        retry_delay=_model_batch_retry_delay(
                            safe_error,
                            claimed_batch.attempt_count,
                        ),
                    )
                except Exception as persistence_error:
                    _propagate_task_lease_loss(
                        self._lease_cursor,
                        persistence_error,
                    )
                    # 持久化失败不能遮蔽原始模型错误；任务租约恢复流程会在
                    # 后续扫描中接管仍处于 RUNNING 的批次。
                    LOGGER.exception(
                        "Agent %s 批次 %s 失败状态无法持久化",
                        self._agent.value,
                        batch.number,
                    )
                _raise_if_lease_lost(self._lease_cursor)
                try:
                    self._queue.record_model_progress(
                        self._lease_cursor.lease,
                        "batch_failed",
                        failure_payload,
                        agent=self._agent.value,
                    )
                except Exception as progress_error:
                    _propagate_task_lease_loss(
                        self._lease_cursor,
                        progress_error,
                    )
                    LOGGER.exception(
                        "Agent %s 批次 %s 失败进度无法持久化",
                        self._agent.value,
                        batch.number,
                    )
                error = SafeApplicationError(safe_error)
                self._raise_partial_batch_error(
                    review_input,
                    results,
                    completed_batches,
                    batch.number,
                    error,
                )
            finally:
                self._lease_cursor.unregister_model_batch(
                    self._agent.value,
                    batch.number,
                )
            _raise_if_lease_lost(self._lease_cursor)
            self._queue.record_model_progress(
                self._lease_cursor.lease,
                "request_completed",
                {
                    "agent": self._agent.value,
                    "batch_number": batch.number,
                    "batch_count": batch.total,
                    "response_status": result.response_status,
                    "duration_ms": result.duration_ms,
                    "provider_request_id": result.provider_request_id,
                },
                agent=self._agent.value,
            )
            _raise_if_lease_lost(self._lease_cursor)
            self._queue.record_model_progress(
                self._lease_cursor.lease,
                "batch_completed",
                {
                    "agent": self._agent.value,
                    "batch_number": batch.number,
                    "batch_count": batch.total,
                    "file_count": len(batch.files),
                    "input_tokens": result.usage.total_input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "reasoning_tokens": result.usage.reasoning_output_tokens,
                    "duration_ms": result.duration_ms,
                    "finding_count": len(result.output.findings),
                    "provider_request_id": result.provider_request_id,
                },
                agent=self._agent.value,
            )
            results.append(result)
            completed_batches.append(batch.number)
        return combine_model_review_results(review_input, tuple(results))

    def _review_with_truncation_split(
        self,
        review_input: ModelReviewInput,
        *,
        depth: int = 0,
        checkpoint_results: dict[str, ModelReviewResult] | None = None,
        checkpoint_split_nodes: set[str] | None = None,
        on_checkpoint: Callable[
            [str, ModelReviewResult | None],
            None,
        ]
        | None = None,
    ) -> ModelReviewResult:
        """输出截断后递归缩小输入，不重复发送同一请求。"""

        # 子请求的稳定身份由计划/Unit/片段指纹组成；它不依赖模型返回内容，
        # 因而可以在 Worker 重启后从批次 JSON 检查点恢复。
        key = _truncation_input_key(review_input)
        if checkpoint_results is not None:
            cached = checkpoint_results.get(key)
            if cached is not None:
                return cached

        def remember(result: ModelReviewResult) -> ModelReviewResult:
            # 根请求若直接成功，随后会以完整批次结果落库，无需额外写一份
            # 检查点；只有拆分后的子节点才需要在中途保存恢复快照。
            if (
                on_checkpoint is not None
                and checkpoint_results is not None
                and (depth > 0 or key in split_nodes)
            ):
                on_checkpoint(key, result)
            return result

        split_nodes = (
            checkpoint_split_nodes if checkpoint_split_nodes is not None else set()
        )
        if key not in split_nodes:
            try:
                return remember(
                    self._reviewer.review(
                        review_input.model_copy(
                            update={"allow_truncation_retry": False, "evaluation_split_depth": depth}
                        )
                    )
                )
            except Exception as exc:
                error = SafeError.from_exception(exc)
                if error.code is not ErrorCode.MODEL_OUTPUT_TRUNCATED or depth >= 8:
                    raise
                split_nodes.add(key)
                if on_checkpoint is not None:
                    on_checkpoint(key, None)
        units = review_input.units
        if len(units) > 1:
            middle = len(units) // 2
            child_inputs = (
                _model_input_subset(review_input, units[:middle]),
                _model_input_subset(review_input, units[middle:]),
            )
            child_results = tuple(
                self._review_with_truncation_split(
                    item,
                    depth=depth + 1,
                    checkpoint_results=checkpoint_results,
                    checkpoint_split_nodes=split_nodes,
                    on_checkpoint=on_checkpoint,
                )
                for item in child_inputs
            )
            # 只缓存叶子响应；中间结果的位置坐标可能已经经过一次映射，
            # 直接缓存会在恢复时被父批次再次映射。
            return combine_model_review_results(review_input, child_results)

        # 单个 Unit 仍截断时，让现有规划器按更小的输入预算生成代码片段。
        reduced_limit = max(
            4_096,
            min(
                self._settings.max_batch_input_tokens // 2,
                max(4_096, review_input.total_estimated_input_bytes // 4),
            ),
        )
        if reduced_limit >= self._settings.max_batch_input_tokens:
            raise SafeApplicationError(
                SafeError(
                    code=ErrorCode.MODEL_OUTPUT_TRUNCATED,
                    safe_message="模型输出被截断，无法进一步拆分批次",
                    retryable=True,
                )
            )
        smaller_settings = replace(
            self._settings,
            max_batch_input_tokens=reduced_limit,
        )
        children = plan_model_review_batches(review_input, smaller_settings)
        if len(children) <= 1:
            raise SafeApplicationError(
                SafeError(
                    code=ErrorCode.MODEL_OUTPUT_TRUNCATED,
                    safe_message="模型输出被截断，无法进一步拆分批次",
                    retryable=True,
                )
            )
        child_results = tuple(
            self._review_with_truncation_split(
                child.review_input,
                depth=depth + 1,
                checkpoint_results=checkpoint_results,
                checkpoint_split_nodes=split_nodes,
                on_checkpoint=on_checkpoint,
            )
            for child in children
        )
        return combine_model_review_results(
            review_input,
            child_results,
            batches=children,
        )

    def close(self) -> None:
        """底层适配器由运行时缓存统一关闭。"""

        return None


def _model_input_subset(
    source: ModelReviewInput,
    units: tuple[ReviewUnit, ...],
) -> ModelReviewInput:
    """构造一个只含目标 Unit 和其规则的稳定子请求。"""

    rule_paths = {path for unit in units for path in unit.rule_paths}
    rules = tuple(rule for rule in source.rules if rule.path in rule_paths)
    total_bytes = sum(unit.estimated_input_bytes for unit in units)
    if source.planner_version in {"review-planner-v2", "review-planner-v3"}:
        total_bytes += sum(rule.byte_size for rule in rules)
    return source.model_copy(
        update={
            "rules": rules,
            "units": units,
            "total_estimated_input_bytes": total_bytes,
            "allow_truncation_retry": False,
        }
    )


def _truncation_input_key(review_input: ModelReviewInput) -> str:
    """生成不含提示词正文的稳定子请求指纹。"""

    identity = {
        "plan": review_input.plan_fingerprint,
        "agent": review_input.review_agent.value
        if review_input.review_agent is not None
        else None,
        "units": [
            {
                "unit_key": unit.unit_key,
                "patch_sha256": unit.patch_sha256,
                "fragment_index": unit.fragment_index,
                "fragment_count": unit.fragment_count,
            }
            for unit in review_input.units
        ],
        "rules": [rule.path for rule in review_input.rules],
    }
    return sha256(
        json.dumps(
            identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _load_truncation_checkpoint(
    checkpoint: Mapping[str, object] | None,
    *,
    root_key: str | None = None,
) -> tuple[dict[str, ModelReviewResult], set[str]]:
    """读取并验证有界截断检查点；损坏条目会被忽略并重新请求。"""

    if not isinstance(checkpoint, Mapping):
        return {}, set()
    if checkpoint.get("version") != 1:
        return {}, set()
    stored_root_key = checkpoint.get("root_key")
    if (
        root_key is not None
        and isinstance(stored_root_key, str)
        and stored_root_key != root_key
    ):
        return {}, set()
    raw_results = checkpoint.get("results")
    raw_split_nodes = checkpoint.get("split_nodes")
    split_nodes = (
        {
            value
            for value in raw_split_nodes
            if isinstance(value, str) and len(value) == 64
        }
        if isinstance(raw_split_nodes, (list, tuple, set, frozenset))
        else set()
    )
    if not isinstance(raw_results, Mapping):
        return {}, split_nodes
    loaded: dict[str, ModelReviewResult] = {}
    for raw_key, raw_result in list(raw_results.items())[:2048]:
        if not isinstance(raw_key, str) or len(raw_key) != 64:
            continue
        try:
            loaded[raw_key] = ModelReviewResult.model_validate(raw_result)
        except (TypeError, ValueError):
            continue
    return loaded, split_nodes


def _dump_truncation_checkpoint(
    results: Mapping[str, ModelReviewResult],
    split_nodes: set[str] | frozenset[str] = frozenset(),
    *,
    root_key: str | None = None,
) -> dict[str, object]:
    """将检查点编码为受限 JSON 结构。"""

    return {
        "version": 1,
        "root_key": root_key,
        "split_nodes": sorted(value for value in split_nodes if len(value) == 64),
        "results": {
            key: result.model_dump(mode="json")
            for key, result in list(results.items())[:2048]
        },
    }


def _model_batch_retry_delay(error: SafeError, attempt_count: int) -> timedelta:
    """计算单批指数退避，并尊重供应商给出的 Retry-After。"""

    retry_after = error.details.get("retry_after_seconds")
    provider_seconds = (
        retry_after
        if isinstance(retry_after, int)
        and not isinstance(retry_after, bool)
        and retry_after >= 0
        else 0
    )
    # 先限制指数再做幂运算，避免损坏数据中的超大 attempt_count 触发巨大
    # Python 整数分配；最终退避仍保持不超过 300 秒。
    exponent = min(8, max(0, attempt_count - 1))
    exponential_seconds = min(300, 5 * (2**exponent))
    return timedelta(seconds=max(provider_seconds, exponential_seconds))


def _safe_unsupported_parameters_payload(
    details: object,
) -> list[str] | None:
    """提取可公开的中转站不兼容参数名，不传播任意错误详情。"""

    if not isinstance(details, Mapping):
        return None
    raw = details.get("unsupported_parameters")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return None
    values = sorted(
        {
            item.strip()
            for item in raw
            if isinstance(item, str)
            and 0 < len(item.strip()) <= 64
            and all(ord(character) >= 32 for character in item)
        }
    )
    return values[:16] or None
