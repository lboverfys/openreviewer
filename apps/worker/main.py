"""单并发数据库 Worker 的进程入口。"""

import json
import logging
import os
import signal
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from inspect import Parameter, signature
from threading import Event, Lock, RLock, Thread
from typing import NoReturn
from uuid import uuid4

from apps.worker.settings import WorkerSettings
from domain.enums import (
    ExecutionStatus,
    ModelCallStatus,
    ModelReviewVerdict,
    ReviewAgent,
    WorkerStatus,
)
from domain.model_review import (
    MaterializedFinding,
    ModelReviewInput,
    ModelReviewOutput,
    ModelReviewResult,
    ModelTokenUsage,
    materialize_findings,
)
from domain.repository_policy import RepositoryRequestLimitError
from domain.review_planning import ReviewUnit
from domain.security import (
    ErrorCode,
    SafeApplicationError,
    SafeError,
    install_redacting_log_filters,
)
from persistence.database import Database
from persistence.operations import SqlAlchemyOperationsRepository
from persistence.retrieval import RetrievalRepository
from persistence.retrieval_runtime import RetrievalRuntimeRepository
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from services.agent_settings import AgentSettingsService
from services.agent_workflow import WorkflowExecution, _PartialAgentReviewError
from services.ai_settings import (
    ActiveAiRuntime,
    AiRuntimeProvider,
    AiSecretCipher,
    AiSettingsService,
    SqlAlchemyAiRuntimeProvider,
)
from services.evidence_verification import (
    EvidenceVerifier,
    GitHubEvidenceVerifier,
    apply_evidence_verification,
)
from services.github import GitHubApiClient
from services.github_access import GitHubAccessPolicy
from services.github_auth import (
    GITHUB_READ_TOKEN_SCOPE,
    GitHubAppSettings,
    GitHubAppTokenProvider,
)
from services.github_code_sources import GitHubCodeSourceLoader
from services.github_context import GitHubReviewContextLoader, ReviewContextLoader
from services.github_rules import GitHubRepositoryRuleLoader, RepositoryRuleLoader
from services.model_budget import model_request_scope
from services.model_review import (
    ModelReviewer,
    ModelServiceSettings,
    combine_model_review_results,
    plan_model_review_batches,
    remap_model_review_result,
)
from services.operations import (
    OperationsError,
    OperationsService,
    OperationsSettings,
    WorkerMaintenance,
)
from services.rag import ManagedMarkdownKnowledgeBase, MarkdownKnowledgeBase
from services.retrieval import HybridRetrievalService, RetrievalSettingsService
from services.review_planning import ReviewPlanner
from services.task_queue import (
    ModelBatchBusyError,
    ModelBatchLease,
    ModelReviewCheckpointTooLargeError,
    ReviewTaskLease,
    ReviewTaskQueue,
    TaskLeaseLostError,
    TaskQueueError,
)
from services.telemetry import TelemetryHttpServer

LOGGER = logging.getLogger("openreviewer.worker")


def _record_worker_heartbeat(
    queue: ReviewTaskQueue,
    worker_id: str,
    status: WorkerStatus,
    current_task_id: str | None,
    instance_id: str,
) -> None:
    """向新旧队列实现写入心跳，优先使用进程 token 的 CAS 版本。"""

    recorder = queue.record_heartbeat
    try:
        parameters = signature(recorder).parameters.values()
        supports_instance = any(
            parameter.name == "instance_id"
            or parameter.kind is Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        # C 扩展或代理对象无法反射时，优先尝试新协议；真实队列实现支持该参数。
        supports_instance = True
    if supports_instance:
        recorder(
            worker_id,
            status,
            current_task_id,
            instance_id=instance_id,
        )
    else:
        recorder(worker_id, status, current_task_id)


def _start_worker_heartbeat(
    queue: ReviewTaskQueue,
    worker_id: str,
    instance_id: str,
) -> None:
    """启动时原子接管心跳；旧队列实现回退到普通起始心跳。"""

    starter = getattr(queue, "start_heartbeat", None)
    if callable(starter):
        starter(worker_id, instance_id)
        return
    _record_worker_heartbeat(
        queue,
        worker_id,
        WorkerStatus.STARTING,
        None,
        instance_id,
    )


class _LeaseCursor:
    """在多次外部请求之间保存最近一次成功续期的租约。"""

    def __init__(
        self,
        queue: ReviewTaskQueue,
        lease: ReviewTaskLease,
        duration: timedelta,
    ) -> None:
        self._queue = queue
        # 一个任务可能先以普通租约领取，随后进入 GitHub/模型等更长的阶段。
        # 心跳线程调用 ``renew()`` 时必须沿用当前阶段的最长租约，不能把
        # 已升级的模型租约重新缩短为普通任务租约。
        self._active_duration = duration
        self.lease = lease
        self._lock = RLock()
        self._active_batches: dict[tuple[str, int], timedelta] = {}
        self._lease_lost = Event()

    def renew(self, duration: timedelta | None = None) -> None:
        # 心跳线程一旦确认所有权丢失，后续主线程不应再发起任何续租写入。
        self.raise_if_lease_lost()
        with self._lock:
            self.raise_if_lease_lost()
            requested_duration = (
                self._active_duration if duration is None else duration
            )
            # 阶段租约只允许延长，不允许被并发心跳或旧调用路径缩短。
            effective_duration = max(self._active_duration, requested_duration)
            try:
                self.lease = self._queue.renew_lease(
                    self.lease,
                    effective_duration,
                )
            except TaskQueueError as exc:
                if (
                    SafeError.from_exception(exc).code
                    is ErrorCode.TASK_LEASE_LOST
                ):
                    self._lease_lost.set()
                raise
            self._active_duration = effective_duration

    def register_model_batch(
        self,
        agent: str,
        batch_number: int,
        lease_duration: timedelta,
    ) -> None:
        """登记当前外部请求对应的批次，供后台心跳续租。"""

        if lease_duration.total_seconds() <= 0:
            raise ValueError("model batch lease duration must be positive")
        with self._lock:
            key = (agent, batch_number)
            current = self._active_batches.get(key)
            self._active_batches[key] = (
                lease_duration if current is None else max(current, lease_duration)
            )

    def unregister_model_batch(self, agent: str, batch_number: int) -> None:
        with self._lock:
            self._active_batches.pop((agent, batch_number), None)

    def renew_model_batches(self) -> None:
        """续租所有正在请求的批次；旧队列实现没有该接口时兼容跳过。"""

        self.raise_if_lease_lost()
        with self._lock:
            self.raise_if_lease_lost()
            lease = self.lease
            active = tuple(self._active_batches.items())
        if not active:
            return

        bulk_renewer = getattr(self._queue, "renew_model_batches", None)
        if callable(bulk_renewer):
            try:
                bulk_renewer(
                    lease,
                    tuple(
                        ModelBatchLease(
                            agent=agent,
                            batch_number=batch_number,
                            lease_duration=duration,
                        )
                        for (agent, batch_number), duration in active
                    ),
                )
            except TaskQueueError as exc:
                if (
                    SafeError.from_exception(exc).code
                    is ErrorCode.TASK_LEASE_LOST
                ):
                    self._lease_lost.set()
                raise
            return

        # 旧队列实现只有单批接口时保留兼容路径；生产 SQL 队列始终使用上面的
        # 批量事务，避免固定三路 Agent 产生 N+1 数据库续租请求。
        renewer = getattr(self._queue, "renew_model_batch", None)
        if not callable(renewer):
            return
        try:
            parameters = signature(renewer).parameters.values()
            supports_agent = any(
                parameter.name == "agent"
                or parameter.kind is Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_agent = True
        for (agent, batch_number), duration in active:
            self.raise_if_lease_lost()
            if supports_agent:
                try:
                    renewer(
                        lease,
                        batch_number,
                        agent=agent,
                        lease_duration=duration,
                    )
                except TaskQueueError as exc:
                    if (
                        SafeError.from_exception(exc).code
                        is ErrorCode.TASK_LEASE_LOST
                    ):
                        self._lease_lost.set()
                    raise
            else:
                try:
                    renewer(
                        lease,
                        batch_number,
                        lease_duration=duration,
                    )
                except TaskQueueError as exc:
                    if (
                        SafeError.from_exception(exc).code
                        is ErrorCode.TASK_LEASE_LOST
                    ):
                        self._lease_lost.set()
                    raise

    def mark_lease_lost(self) -> None:
        self._lease_lost.set()

    @property
    def is_lease_lost(self) -> bool:
        """返回是否已经观察到任务或批次租约失效。"""

        return self._lease_lost.is_set()

    def raise_if_lease_lost(self) -> None:
        if self._lease_lost.is_set():
            raise TaskLeaseLostError()


def _raise_if_lease_lost(cursor: _LeaseCursor) -> None:
    """兼容旧测试/调用方传入的最小租约游标对象。"""

    checker = getattr(cursor, "raise_if_lease_lost", None)
    if callable(checker):
        checker()


def _propagate_task_lease_loss(cursor: object, error: BaseException) -> None:
    """失败回写异常若表示租约丢失，必须保留该信号并停止旧 Worker。"""

    safe_error = SafeError.from_exception(error)
    if safe_error.code is not ErrorCode.TASK_LEASE_LOST:
        return
    marker = getattr(cursor, "mark_lease_lost", None)
    if callable(marker):
        marker()
    raise error


class _BusyHeartbeat:
    """在 GitHub 外部读取期间定时刷新 Worker 忙碌心跳。"""

    def __init__(
        self,
        queue: ReviewTaskQueue,
        worker_id: str,
        task_id: str,
        instance_id: str,
        interval: timedelta,
        lease_cursor: _LeaseCursor | None = None,
        on_worker_ownership_lost: Callable[[], None] | None = None,
    ) -> None:
        self._queue = queue
        self._worker_id = worker_id
        self._task_id = task_id
        self._instance_id = instance_id
        self._lease_cursor = lease_cursor
        self._on_worker_ownership_lost = on_worker_ownership_lost
        # 健康检查默认允许 15 秒，最长 5 秒一次可以覆盖慢速外部请求。
        self._interval_seconds = min(
            5.0,
            max(0.5, interval.total_seconds()),
        )
        self._stop_event = Event()
        # ``stop()`` 和一次正在进行的队列写入之间需要一个明确的边界。
        # 否则主线程可能先把心跳写成 IDLE，后台线程随后才完成 BUSY 写入。
        self._stop_requested = Event()
        self._operation_lock = Lock()
        self._thread = Thread(
            target=self._run,
            name=f"openreviewer-heartbeat-{worker_id}",
            daemon=True,
        )

    def start(self) -> None:
        """启动独立心跳线程；线程只使用队列公开的短事务接口。"""

        self._thread.start()

    def stop(self) -> bool:
        """请求停止并报告线程是否已经退出。

        数据库调用本身由队列连接/语句超时约束；这里仍保留有限等待，避免
        数据库彻底失联时阻塞 Worker 关停。调用方在得到 ``False`` 时不能再
        写入 ``IDLE``，以免迟到的后台 ``BUSY`` 覆盖它；下一轮应先等待线程
        自然退出再继续领取任务。
        """

        self._stop_requested.set()
        self._stop_event.set()
        if not self._thread.is_alive():
            return True
        # 让已经进入队列写入的这一轮先完成；之后 _run 会看到 stop_requested
        # 并退出，不会再开始新的写入。
        operation_acquired = self._operation_lock.acquire(
            timeout=self._interval_seconds + 1.0,
        )
        if operation_acquired:
            self._operation_lock.release()
        self._thread.join(timeout=self._interval_seconds + 1.0)
        stopped = not self._thread.is_alive()
        if not stopped:
            LOGGER.warning(
                "Worker %s 的忙碌心跳线程未及时退出，将等待其完成后再恢复 IDLE",
                self._worker_id,
            )
        return stopped

    def is_alive(self) -> bool:
        """返回后台线程是否仍在执行最后一轮队列操作。"""

        return self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            if self._stop_requested.is_set():
                return
            # stop() 会先设置 stop_requested，再等待这把锁；因此一旦它
            # 返回成功，后台线程不可能在主线程的 IDLE 写入之后追加 BUSY。
            with self._operation_lock:
                if self._stop_requested.is_set():
                    return
                try:
                    _record_worker_heartbeat(
                        self._queue,
                        self._worker_id,
                        WorkerStatus.BUSY,
                        self._task_id,
                        self._instance_id,
                    )
                except TaskQueueError as exc:
                    queue_error = SafeError.from_exception(exc)
                    if queue_error.code is ErrorCode.TASK_LEASE_LOST:
                        if self._lease_cursor is not None:
                            self._lease_cursor.mark_lease_lost()
                        if self._on_worker_ownership_lost is not None:
                            self._on_worker_ownership_lost()
                        LOGGER.error("Worker 忙碌心跳发现进程所有权已丢失，停止刷新")
                        return
                    LOGGER.exception("Worker 忙碌心跳刷新失败")
                    continue

                try:
                    if self._lease_cursor is not None:
                        self._lease_cursor.renew()
                        self._lease_cursor.renew_model_batches()
                except TaskLeaseLostError:
                    if self._lease_cursor is not None:
                        self._lease_cursor.mark_lease_lost()
                    LOGGER.exception("Worker 任务租约已丢失，当前模型请求不会再写入结果")
                    # 继续循环只会反复写续租请求；主流程会在下一个外部请求
                    # 边界观察标记，并由队列恢复机制接管任务。
                    return
                except TaskQueueError as exc:
                    queue_error = SafeError.from_exception(exc)
                    if queue_error.code is ErrorCode.TASK_LEASE_LOST:
                        if self._lease_cursor is not None:
                            self._lease_cursor.mark_lease_lost()
                        LOGGER.error("Worker 任务租约已丢失，停止续租")
                        return
                    LOGGER.exception("Worker 模型租约续租失败")


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
        with model_request_scope(self._request_guard):
            return self._review_batches(review_input)

    def _review_batches(self, review_input: ModelReviewInput) -> ModelReviewResult:
        _raise_if_lease_lost(self._lease_cursor)
        # 正式批次的截断恢复由 Worker 缩小输入；供应商适配器不得把同一
        # 大请求改成 8K 后再次发送。旧的直接适配器调用仍保留兼容行为。
        review_input = review_input.model_copy(
            update={"allow_truncation_retry": False}
        )
        batches = plan_model_review_batches(review_input, self._settings)
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
                checkpoint_results, checkpoint_split_nodes = _load_truncation_checkpoint(
                    getattr(saved, "checkpoint", None),
                    root_key=batch_checkpoint_key,
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
                        batch.review_input,
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
                    failure_payload["unsupported_parameters"] = (
                        unsupported_parameters
                    )
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

        split_nodes = checkpoint_split_nodes if checkpoint_split_nodes is not None else set()
        if key not in split_nodes:
            try:
                return remember(
                    self._reviewer.review(
                        review_input.model_copy(
                            update={"allow_truncation_retry": False}
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
    split_nodes = {
        value
        for value in raw_split_nodes
        if isinstance(value, str) and len(value) == 64
    } if isinstance(raw_split_nodes, (list, tuple, set, frozenset)) else set()
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


def _workflow_result(
    review_input: ModelReviewInput,
    execution: WorkflowExecution,
    *,
    allow_partial: bool = False,
) -> ModelReviewResult:
    """把多 Agent 结果折叠为一条兼容记录，汇总 Agent 作为配置代表。"""

    agent_results_list: list[ModelReviewResult] = []
    for agent_execution in execution.agents:
        if agent_execution.result is not None and (
            not allow_partial
            or agent_execution.status == "completed"
            or (
                agent_execution.agent is not ReviewAgent.SUMMARY
                and agent_execution.result.status
                in {ModelCallStatus.SUCCEEDED, ModelCallStatus.SKIPPED}
            )
        ):
            agent_results_list.append(agent_execution.result)
    agent_results = tuple(agent_results_list)
    summary_result: ModelReviewResult | None = None
    if execution.summary_execution is not None and (
        not allow_partial or execution.summary_execution.status == "completed"
    ):
        summary_result = execution.summary_execution.result
    results: tuple[ModelReviewResult, ...] = agent_results + (
        (summary_result,) if summary_result is not None else ()
    )
    if allow_partial:
        # 汇总失败或某一路失败时，只保留已经返回结构化结果的调用；失败
        # 节点由 execution 的状态字段和事件单独表达，不能阻塞本地合并。
        results = tuple(
            item for item in results
            if item.status in {ModelCallStatus.SUCCEEDED, ModelCallStatus.SKIPPED}
        )
    if not results:
        raise TaskQueueError("固定 Agent 没有可持久化的模型结果")
    all_succeeded = all(
        item.status is ModelCallStatus.SUCCEEDED for item in results
    )
    all_skipped = (
        not review_input.units
        and all(item.status is ModelCallStatus.SKIPPED for item in results)
    )
    if (not allow_partial and execution.status != "completed") or not (
        all_succeeded or all_skipped
    ):
        raise TaskQueueError("固定 Agent 仅能聚合全部成功的模型结果")
    # 旧表只能表达一组供应商配置。正常四 Agent 路径以最终汇总 Agent
    # 作为代表；各路真实配置、请求 ID 和计量仍保存在批次记录与结构化事件中。
    representative = summary_result or results[0]
    aggregate_status = (
        ModelCallStatus.SKIPPED if all_skipped else ModelCallStatus.SUCCEEDED
    )
    usage = ModelTokenUsage(
        input_tokens=sum(item.usage.input_tokens for item in results),
        output_tokens=sum(item.usage.output_tokens for item in results),
        cache_read_input_tokens=sum(
            item.usage.cache_read_input_tokens for item in results
        ),
        cache_write_input_tokens=sum(
            item.usage.cache_write_input_tokens for item in results
        ),
        reasoning_output_tokens=sum(
            item.usage.reasoning_output_tokens for item in results
        ),
    )
    fingerprint = sha256(
        "|".join(item.request_fingerprint for item in results).encode("ascii")
    ).hexdigest()
    final_findings = tuple(execution.findings)
    conclusion_source = (
        summary_result.output
        if summary_result is not None
        else next(
            (
                item.output
                for item in reversed(agent_results)
                if item.output.verdict is not None
            ),
            None,
        )
    )
    if (
        all_skipped
        or conclusion_source is None
        or conclusion_source.verdict is None
        or conclusion_source.summary is None
    ):
        output = ModelReviewOutput(findings=final_findings)
    else:
        verdict = (
            ModelReviewVerdict.INSUFFICIENT_CONTEXT
            if conclusion_source.verdict
            is ModelReviewVerdict.INSUFFICIENT_CONTEXT
            else (
                ModelReviewVerdict.ISSUES_FOUND
                if final_findings
                else ModelReviewVerdict.NO_ACTIONABLE_ISSUE
            )
        )
        output = ModelReviewOutput(
            verdict=verdict,
            summary=conclusion_source.summary,
            checked_areas=conclusion_source.checked_areas,
            findings=final_findings,
        )
    estimated_costs = tuple(item.estimated_cost_microusd for item in results)
    estimated_cost_microusd = (
        sum(cost for cost in estimated_costs if cost is not None)
        if all(cost is not None for cost in estimated_costs)
        else None
    )
    return ModelReviewResult(
        provider=representative.provider,
        api_protocol=representative.api_protocol,
        model=representative.model,
        status=aggregate_status,
        prompt_version=representative.prompt_version,
        request_fingerprint=fingerprint,
        provider_response_id=(
            representative.provider_response_id if all_succeeded else None
        ),
        provider_request_id=(
            representative.provider_request_id if all_succeeded else None
        ),
        response_status=(representative.response_status if all_succeeded else None),
        duration_ms=sum(item.duration_ms for item in results),
        usage=usage,
        estimated_cost_microusd=estimated_cost_microusd,
        output=output,
    )


def _agent_conclusion_payload(execution: object | None) -> dict[str, object]:
    """提取可公开的 Agent 结论，不包含提示词、原始响应或思维链。"""

    result = getattr(execution, "result", None)
    output = getattr(result, "output", None)
    verdict = getattr(output, "verdict", None)
    return {
        "verdict": verdict.value if verdict is not None else None,
        "summary": getattr(output, "summary", None),
        "checked_areas": list(getattr(output, "checked_areas", ())),
    }


class WorkerRuntime:
    def __init__(
        self,
        queue: ReviewTaskQueue,
        settings: WorkerSettings,
        *,
        context_loader: ReviewContextLoader | None = None,
        rule_loader: RepositoryRuleLoader | None = None,
        planner: ReviewPlanner | None = None,
        model_reviewer: ModelReviewer | None = None,
        ai_runtime_provider: AiRuntimeProvider | None = None,
        knowledge_base: MarkdownKnowledgeBase | None = None,
        maintenance: WorkerMaintenance | None = None,
        evidence_verifier: EvidenceVerifier | None = None,
        retrieval_service: HybridRetrievalService | None = None,
        stop_event: Event | None = None,
        instance_id: str | None = None,
    ) -> None:
        """保存队列适配器、运行参数和可选的停止事件。

        注入 ``stop_event`` 让测试可以控制循环；生产环境由信号处理器设置同一
        事件，从而让主循环在完成当前安全边界后退出。

        参数：
            queue: 实现持久化领取、恢复、心跳和状态推进的队列边界。
            settings: 已校验的 Worker ID、轮询和租约配置。
            stop_event: 可选停止事件；不传时创建新的 ``threading.Event``。

        构造函数不访问数据库，也不启动线程；实际连接和心跳写入从 ``run`` 或
        ``run_once`` 开始。
        """
        self._queue = queue
        self._settings = settings
        self._context_loader = context_loader
        self._rule_loader = rule_loader
        self._planner = planner
        self._model_reviewer = model_reviewer
        self._ai_runtime_provider = ai_runtime_provider
        self._knowledge_base = knowledge_base
        self._maintenance = maintenance
        self._evidence_verifier = evidence_verifier
        self._retrieval_service = retrieval_service
        self._stop_event = stop_event or Event()
        # Worker 心跳 token 与任务租约是两层独立的所有权。进程 token 被新
        # 实例接管后，旧实例不能再轮询或写入 ``idle/stopping``；普通任务
        # 租约丢失则只影响当前任务，不能误停整个 Worker。
        self._worker_ownership_lost = Event()
        self._instance_id = instance_id or uuid4().hex
        if not 1 <= len(self._instance_id) <= 64:
            raise ValueError("worker instance ID must contain 1 to 64 characters")
        self._heartbeat_started = False
        self._reviews_since_index = 0
        # 极端情况下数据库调用可能超过 stop() 的有限等待窗口。保留这些
        # 线程引用，下一轮先等它们彻底退出，避免旧 BUSY 写入覆盖新任务状态。
        self._lingering_heartbeats: list[_BusyHeartbeat] = []

    def _mark_worker_ownership_lost(self) -> None:
        """锁存进程心跳所有权丢失，并请求主循环在安全边界退出。"""

        if not self._worker_ownership_lost.is_set():
            LOGGER.error(
                "Worker %s 的进程心跳所有权已被新实例接管，将停止旧实例",
                self._settings.worker_id,
            )
        self._worker_ownership_lost.set()
        self._stop_event.set()

    def _record_owned_heartbeat(
        self,
        status: WorkerStatus,
        current_task_id: str | None,
    ) -> None:
        """写入当前实例心跳；token 失效时立即停止旧 Worker。"""

        try:
            _record_worker_heartbeat(
                self._queue,
                self._settings.worker_id,
                status,
                current_task_id,
                self._instance_id,
            )
        except TaskQueueError as exc:
            if SafeError.from_exception(exc).code is ErrorCode.TASK_LEASE_LOST:
                self._mark_worker_ownership_lost()
            raise

    def _ensure_heartbeat_started(self) -> None:
        """确保本进程先接管心跳，再执行任何任务或状态写入。"""

        if self._heartbeat_started:
            return
        try:
            _start_worker_heartbeat(
                self._queue,
                self._settings.worker_id,
                self._instance_id,
            )
        except TaskQueueError as exc:
            # 理论上 start_heartbeat 会先接管 token；兼容旧队列或并发实现
            # 仍可能在这里报告所有权冲突，不能让旧实例继续轮询。
            if SafeError.from_exception(exc).code is ErrorCode.TASK_LEASE_LOST:
                self._mark_worker_ownership_lost()
            raise
        self._heartbeat_started = True

    def _drain_lingering_heartbeats(self) -> bool:
        """确认上一轮超时的心跳线程已退出后再开始新的任务。"""

        if not self._lingering_heartbeats:
            return True
        pending: list[_BusyHeartbeat] = []
        for heartbeat in self._lingering_heartbeats:
            if heartbeat.is_alive():
                pending.append(heartbeat)
                continue
            if not self._worker_ownership_lost.is_set():
                try:
                    self._record_owned_heartbeat(WorkerStatus.IDLE, None)
                except TaskQueueError:
                    # 线程已结束，不会再产生迟到 BUSY；保留日志并让
                    # 后续主循环/健康检查决定是否继续。
                    LOGGER.exception(
                        "Worker 上一轮忙碌心跳退出后的 IDLE 写入失败"
                    )
        self._lingering_heartbeats = pending
        if self._worker_ownership_lost.is_set():
            return False
        if pending:
            LOGGER.warning(
                "Worker %s 仍有忙碌心跳线程未退出，本轮暂不领取新任务",
                self._settings.worker_id,
            )
            return False
        return True

    def _stop_lingering_heartbeats_for_shutdown(self) -> bool:
        """关停前等待迟到的 BUSY 写入结束，避免覆盖 STOPPING。"""

        if not self._lingering_heartbeats:
            return True
        stopped = True
        pending: list[_BusyHeartbeat] = []
        for heartbeat in self._lingering_heartbeats:
            if heartbeat.stop():
                continue
            stopped = False
            pending.append(heartbeat)
        self._lingering_heartbeats = pending
        return stopped

    def _verify_findings(
        self,
        cursor: _LeaseCursor,
        review_input: ModelReviewInput,
        findings: tuple[MaterializedFinding, ...],
    ) -> tuple[MaterializedFinding, ...]:
        """在持久化前批量核验源码证据；任何异常都安全降级。"""

        if self._evidence_verifier is None or not findings:
            return findings
        try:
            target = self._queue.load_target(cursor.lease)
            results = self._evidence_verifier.verify(
                review_input,
                findings,
                installation_id=target.installation_id,
            )
            return apply_evidence_verification(findings, results)
        except Exception as exc:
            # 证据核验通常允许保守降级，但租约失效是所有权信号，不能被
            # 当成普通核验故障吞掉，否则后续仍可能写入过期结果。
            _propagate_task_lease_loss(cursor, exc)
            LOGGER.exception(
                "任务 %s 的源码证据核验失败，将保守标记为未核验",
                cursor.lease.task_id,
            )
            return apply_evidence_verification(findings, {})

    @property
    def stop_event(self) -> Event:
        """返回控制主循环退出的共享停止事件。

        返回：
            Worker 内部保存的 ``Event`` 对象。设置它会让 ``run`` 在当前安全点
            停止，读取它可用于测试断言或信号处理器协作。

        返回的是同一个对象而不是副本；调用方不应在任务事务中途随意清除它。
        """
        return self._stop_event

    def run(self) -> None:
        """运行 Worker 主循环并维护生命周期心跳。

        启动时写入 ``starting``，每轮先恢复过期租约再尝试领取任务；循环空闲时
        等待轮询间隔，但使用 ``Event.wait`` 让 SIGTERM/SIGINT 可以立即唤醒退出。
        无论循环如何结束，``finally`` 都会尽力写入 ``stopping`` 心跳并释放运行
        状态，便于 Dashboard 区分正常停止和失联。

        生命周期：
            进入时记录 ``starting``；每轮由 ``run_once`` 恢复过期租约、记录空闲
            心跳并尝试领取任务；没有任务时用可被停止事件唤醒的等待代替阻塞睡眠。
            收到 SIGTERM/SIGINT 后不再开始新轮次，最后写入 ``stopping``。

        异常：
            队列异常不会被这里统一吞掉；停止阶段的心跳写入失败会记录日志后继续
            退出，避免数据库故障阻止进程终止。主循环外的致命初始化错误由 ``main``
            交给进程管理器处理。
        """
        worker_id = self._settings.worker_id
        try:
            self._ensure_heartbeat_started()
            LOGGER.info("Worker 已启动，等待数据库任务")
            while not self._stop_event.is_set():
                try:
                    self.run_once()
                except TaskQueueError:
                    # 单次队列事务失败不应终止常驻进程；等待下一轮后重试，
                    # 同时保留完整堆栈供详情页之外的运维日志定位。
                    LOGGER.exception("Worker 本轮队列处理失败，将继续轮询")
                self._stop_event.wait(self._settings.poll_interval.total_seconds())
        finally:
            # run_once() 的有限等待可能留下一个仍在数据库调用中的 BUSY
            # 心跳线程。只有确认它已经退出后才能写 STOPPING，否则迟到的
            # BUSY 会把终态覆盖回去，让 Dashboard 长时间显示假忙碌。
            lingering_stopped = self._stop_lingering_heartbeats_for_shutdown()
            if self._worker_ownership_lost.is_set():
                LOGGER.info(
                    "Worker %s 已失去进程心跳所有权，跳过 STOPPING 写入",
                    worker_id,
                )
            elif lingering_stopped:
                try:
                    self._record_owned_heartbeat(WorkerStatus.STOPPING, None)
                except TaskQueueError:
                    LOGGER.exception("Worker 停止状态写入失败")
            else:
                LOGGER.error(
                    "Worker %s 仍有忙碌心跳线程未退出，跳过 STOPPING 写入以避免终态竞态",
                    worker_id,
                )
            LOGGER.info("Worker 已停止")


    def _process_retrieval_index(self) -> bool:
        if self._retrieval_service is None:
            return False
        done = Event()
        heartbeat_thread = None

        def check_progress() -> None:
            nonlocal heartbeat_thread
            if self._stop_event.is_set() or self._worker_ownership_lost.is_set():
                raise TaskLeaseLostError("索引工作进程已停止")
            if heartbeat_thread is None:
                self._record_owned_heartbeat(WorkerStatus.BUSY, None)
                heartbeat_thread = Thread(target=beat, daemon=True)
                heartbeat_thread.start()

        def beat() -> None:
            while not done.wait(self._settings.poll_interval.total_seconds()):
                try:
                    self._record_owned_heartbeat(WorkerStatus.BUSY, None)
                    if self._worker_ownership_lost.is_set():
                        return
                except TaskQueueError:
                    LOGGER.exception("索引工作进程心跳暂时失败")

        try:
            return self._retrieval_service.process_next(check_progress)
        except Exception as exc:
            safe_error = SafeError.from_exception(exc)
            frame = exc.__traceback__
            while frame is not None and frame.tb_next is not None:
                frame = frame.tb_next
            location = f"{os.path.basename(frame.tb_frame.f_code.co_filename)}:{frame.tb_lineno}" if frame is not None else "unknown"
            LOGGER.error("代码索引处理失败，类型=%s，位置=%s，错误码=%s，说明=%s", type(exc).__name__, location, safe_error.code.value, safe_error.safe_message)
            return True
        finally:
            done.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=5)
            if heartbeat_thread is not None and heartbeat_thread.is_alive():
                self._stop_event.set()
                LOGGER.error("索引心跳未及时退出，停止当前 Worker 避免旧心跳覆盖状态")
            elif not self._worker_ownership_lost.is_set():
                self._record_owned_heartbeat(WorkerStatus.IDLE, None)

    def run_once(self) -> bool:
        """执行一轮“恢复、心跳、领取、处理”的队列流程。

        返回值表示本轮是否成功领取了任务。任务会按 GitHub 当前 PR/CI 状态推进；
        处理异常则交给队列按租约规则重试或标记失败。状态
        写入失败会记录日志，但不会把失去租约的任务强行改写成成功。

        返回：
            成功领取任务并完成本轮处理时返回 ``True``；队列为空返回 ``False``。
            “返回 True”只表示领取过任务，不表示审查已经完成。

        状态顺序：
            先恢复过期租约，再把 Worker 记为空闲并领取一条任务；领取后记为
            ``busy``，读取并保存 GitHub 上下文，最后无论成功失败都
            把心跳恢复为 ``idle``。

        异常：
            队列恢复、心跳或领取阶段的异常会向上冒泡；处理阶段异常会尝试记录
            重试/失败状态，若失败上报本身也失败则只记录日志并结束本轮。
        """
        worker_id = self._settings.worker_id
        if self._worker_ownership_lost.is_set():
            return False
        self._ensure_heartbeat_started()
        if not self._drain_lingering_heartbeats():
            return False
        recovered = self._queue.recover_expired_leases()
        if recovered:
            LOGGER.warning("已恢复 %s 个租约超时任务", recovered)

        self._record_owned_heartbeat(WorkerStatus.IDLE, None)
        if self._maintenance is not None:
            try:
                self._maintenance.run_once(worker_id)
            except OperationsError:
                LOGGER.exception("Worker 本轮运维维护失败，将在下一轮重试")
        # SIGTERM 可能在恢复/维护期间到达；在真正领取前再次检查，避免关停窗口
        # 又启动一条新任务。当前已领取的任务仍由本轮安全边界负责完成或续租。
        if self._stop_event.is_set():
            return False
        # 每四次审查领取给索引一次机会，避免持续排队的审查饿死其依赖索引。
        if self._retrieval_service is not None and self._reviews_since_index >= 4:
            self._reviews_since_index = 0
            if self._process_retrieval_index():
                return True
        ai_runtime = (
            self._ai_runtime_provider.current()
            if self._ai_runtime_provider is not None
            else None
        )
        # 配置读取本身可能阻塞；心跳线程或信号处理器可在此期间请求退出，
        # 因此领取事务前必须再次检查所有权和停止信号。
        if self._worker_ownership_lost.is_set() or self._stop_event.is_set():
            return False
        lease = self._queue.claim_next(
            worker_id,
            self._settings.lease_duration,
            ai_configured=(
                ai_runtime is not None
                if self._ai_runtime_provider is not None
                else True
            ),
        )
        if lease is None:
            if self._retrieval_service is not None:
                self._reviews_since_index = 0
                return self._process_retrieval_index()
            return False

        self._reviews_since_index += 1

        # 心跳线程可能在领取事务期间发现本进程已被新实例接管。此时任务
        # 留给租约恢复流程，不再用旧 token 启动处理或写入状态。
        if self._worker_ownership_lost.is_set():
            return True

        self._record_owned_heartbeat(WorkerStatus.BUSY, lease.task_id)
        cursor = _LeaseCursor(self._queue, lease, self._settings.lease_duration)
        busy_heartbeat = (
            _BusyHeartbeat(
                self._queue,
                worker_id,
                lease.task_id,
                self._instance_id,
                self._settings.poll_interval,
                cursor,
                on_worker_ownership_lost=self._mark_worker_ownership_lost,
            )
            if (
                self._context_loader is not None
                or self._rule_loader is not None
                or self._model_reviewer is not None
                or self._ai_runtime_provider is not None
            )
            else None
        )
        try:
            # 线程启动也必须位于统一的异常边界内。极端情况下 start() 失败
            # 时，任务仍已被领取；后续流程会把它安全重试/失败并释放心跳。
            if busy_heartbeat is not None:
                busy_heartbeat.start()
            next_status = self._advance_to_supported_boundary(cursor, ai_runtime)
            LOGGER.info(
                "任务 %s 已进入 %s",
                lease.task_id,
                next_status.value,
            )
        except Exception as exc:
            safe_error = SafeError.from_exception(exc)
            LOGGER.error(
                "任务 %s 处理失败，错误码=%s，说明=%s",
                lease.task_id,
                safe_error.code.value,
                safe_error.safe_message,
            )
            try:
                lease_write_lost = (
                    safe_error.code is ErrorCode.TASK_LEASE_LOST
                    or self._worker_ownership_lost.is_set()
                    or cursor.is_lease_lost
                )
                if lease_write_lost:
                    # 租约丢失意味着另一 Worker 已接管，或恢复流程正在处理
                    # 这条任务。再次调用 retry_or_fail 只会发起一次必然失败
                    # 的所有权查询，并可能让数据库故障日志被重复放大；旧
                    # Worker 不得对任务状态做任何写入。心跳线程可能只报告了
                    # 进程 token 丢失而原始异常仍是普通模型错误，所以不能只看
                    # safe_error.code。
                    LOGGER.warning(
                        "任务 %s 的租约已丢失，跳过失败上报并交由恢复流程接管",
                        lease.task_id,
                    )
                else:
                    self._queue.retry_or_fail(cursor.lease, safe_error)
            except TaskQueueError as persistence_error:
                persisted_error = SafeError.from_exception(persistence_error)
                LOGGER.error(
                    "任务 %s 的失败状态无法持久化，错误码=%s",
                    lease.task_id,
                    persisted_error.code.value,
                )
        finally:
            heartbeat_stopped = True
            if busy_heartbeat is not None:
                heartbeat_stopped = busy_heartbeat.stop()
            if heartbeat_stopped and not self._worker_ownership_lost.is_set():
                try:
                    self._record_owned_heartbeat(WorkerStatus.IDLE, None)
                except TaskQueueError:
                    # 进程 token 可能恰好在 stop() 与收尾写之间被接管；
                    # 此时旧实例没有资格重试写入，主循环也已被回调停止。
                    LOGGER.exception(
                        "Worker %s 收尾心跳写入失败，跳过旧实例状态更新",
                        worker_id,
                    )
            else:
                # 后台线程仍可能完成一轮 BUSY 写入；此时写 IDLE 反而会被
                # 迟到结果覆盖。下一轮主循环会在安全边界再次确认线程已退出。
                if busy_heartbeat is not None:
                    self._lingering_heartbeats.append(busy_heartbeat)
                LOGGER.warning(
                    "Worker %s 暂不写入 IDLE，等待忙碌心跳线程退出",
                    worker_id,
                )
        return True

    def _advance_to_supported_boundary(
        self,
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
                    ai_runtime.reviewer
                    if ai_runtime is not None
                    else self._model_reviewer
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
                            "context_window_tokens": (
                                model_settings.context_window_tokens
                            ),
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
                            "reasoning_effort": (
                                model_settings.reasoning_effort.value
                            ),
                            "provider": model_settings.provider.value,
                            "api_protocol": (
                                model_settings.resolved_api_protocol.value
                            ),
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
                    stored_by_number = {
                        item.batch_number: item for item in stored_batches
                    }
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
                        if (
                            stored is not None
                            and stored.attempt_count >= allowed_attempts
                        ):
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
                                        "finding_count": len(claimed_batch.result.output.findings),
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
                                    cursor, model_reviewer, batch.review_input,
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
                                    fail_batch = getattr(self._queue, "fail_model_batch", None)
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
                        cursor, model_reviewer, model_input,
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

    def _repository_request_guard(
        self, cursor: _LeaseCursor, model_input: ModelReviewInput,
        exhausted: Event | None = None,
    ) -> Callable[[], None] | None:
        policy = model_input.repository_policy
        if policy is None or policy.max_model_requests is None:
            return None
        limit = policy.max_model_requests

        def reserve() -> None:
            try:
                self._queue.reserve_repository_request(cursor.lease, limit)
            except RepositoryRequestLimitError:
                if exhausted is not None:
                    exhausted.set()
                raise
        return reserve

    def _review_with_repository_limit(
        self, cursor: _LeaseCursor, reviewer: ModelReviewer, model_input: ModelReviewInput,
    ) -> ModelReviewResult:
        with model_request_scope(self._repository_request_guard(cursor, model_input)):
            return reviewer.review(model_input)

    def _run_fixed_agent_workflow(
        self,
        cursor: _LeaseCursor,
        ai_runtime: ActiveAiRuntime,
    ) -> ExecutionStatus:
        """执行三路独立 Agent 的可恢复批次并写入统一结果。"""

        # 固定工作流在读取输入、构造引用和首个批次领取前可能耗时；先把
        # 普通领取租约升级为模型阶段租约，避免忙碌心跳在这段窗口内续成短租约。
        cursor.renew(self._settings.model_review_lease_duration)
        model_input = self._queue.load_model_review_input(cursor.lease)
        budget_exhausted = Event()
        request_guard = self._repository_request_guard(cursor, model_input, budget_exhausted)
        if self._retrieval_service is not None:
            self._queue.record_model_progress(cursor.lease, "retrieval_started", {"agent": "workflow"}, agent="workflow")
            model_input = self._retrieval_service.review_context(model_input, lambda: cursor.renew(self._settings.model_review_lease_duration))
            self._queue.record_model_progress(cursor.lease, "retrieval_completed", {"agent": "workflow", "context_count": len(model_input.context_evidence)}, agent="workflow")
        if cursor.lease.model_attempt_count > 1:
            self._queue.record_model_progress(
                cursor.lease,
                "retry_started",
                {
                    "agent": "workflow",
                    "model_attempt_count": cursor.lease.model_attempt_count,
                    "retry_scope": "failed_node",
                },
                agent="workflow",
            )
        base_workflow = ai_runtime.agent_workflow
        if base_workflow is None:
            raise TaskQueueError("固定 Agent 工作流未配置")
        reviewers = base_workflow.reviewers
        settings_by_agent = base_workflow.agent_settings
        wrapped = {
            agent: _PersistentBatchedReviewer(
                self._queue,
                cursor,
                agent,
                reviewer,
                settings_by_agent[agent],
                self._settings.model_review_lease_duration,
                request_guard=request_guard,
            )
            for agent, reviewer in reviewers.items()
            if agent in {ReviewAgent.SECURITY, ReviewAgent.CONVENTION, ReviewAgent.LOGIC}
            and agent in settings_by_agent
        }
        summary_reviewer = base_workflow.summary_reviewer
        summary_settings = settings_by_agent.get(ReviewAgent.SUMMARY)
        wrapped_summary: ModelReviewer | None
        if summary_reviewer is not None and summary_settings is not None:
            wrapped_summary = _PersistentBatchedReviewer(
                self._queue,
                cursor,
                ReviewAgent.SUMMARY,
                summary_reviewer,
                summary_settings,
                self._settings.model_review_lease_duration,
                request_guard=request_guard,
            )
        else:
            wrapped_summary = None
        from services.agent_workflow import FixedAgentWorkflow

        workflow = FixedAgentWorkflow(
            wrapped,
            summary_reviewer=wrapped_summary,
            max_concurrency=base_workflow.max_concurrency,
        )
        reference_map: dict[ReviewAgent, tuple[str, ...]] = {
            agent: () for agent in ReviewAgent
        }
        if self._knowledge_base is not None:
            # 首次调用在循环外完成有界文件读取并缓存；下面四次检索只做内存匹配。
            knowledge_chunks = self._knowledge_base.chunks()
            policy = model_input.repository_policy
            if policy is not None and policy.knowledge_sources is not None:
                allowed_sources = frozenset(policy.knowledge_sources)
                knowledge_chunks = tuple(
                    chunk for chunk in knowledge_chunks if chunk.source in allowed_sources
                )
            common_query = " ".join(
                (
                    model_input.repository,
                    *(unit.file for unit in model_input.units[:32]),
                )
            )
            responsibilities = {
                ReviewAgent.SECURITY: (
                    "security authorization authentication secrets 安全 鉴权 权限 密钥"
                ),
                ReviewAgent.CONVENTION: (
                    "coding convention maintainability style 规范 编码 可维护性"
                ),
                ReviewAgent.LOGIC: (
                    "logic reliability business database correctness 逻辑 可靠性 数据库"
                ),
                ReviewAgent.SUMMARY: (
                    "historical findings security coding database 汇总 历史 问题"
                ),
            }
            for agent, responsibility in responsibilities.items():
                citations = self._knowledge_base.search(
                    f"{common_query} {responsibility}",
                    limit=8,
                    chunks=knowledge_chunks,
                )
                reference_map[agent] = tuple(
                    (
                        f"{item.source}#{item.heading}@{item.version}: "
                        f"{item.excerpt}"
                    )[:2_000]
                    for item in citations
                )
        execution = workflow.run(
            model_input,
            references=reference_map,
            on_aggregating=lambda: self._queue.mark_model_aggregating(
                cursor.lease
            ),
            allow_partial_aggregation=True,
            local_aggregation=True,
            force_summary=getattr(cursor.lease, "force_summary", False),
        )
        if budget_exhausted.is_set():
            # 已完成批次保留；额度耗尽不能以部分结果进入批准或发布。
            raise RepositoryRequestLimitError()
        # 心跳线程可能在最后一个模型请求期间发现租约已被接管；即使编排器
        # 返回了完整结果，也不能让旧 Worker 覆盖新 Worker 的持久化结果。
        _raise_if_lease_lost(cursor)
        for item in execution.agents:
            _raise_if_lease_lost(cursor)
            phase = (
                "agent_completed"
                if item.status == "completed"
                else "agent_not_applicable"
                if item.status == "not_applicable"
                else "agent_failed"
            )
            self._queue.record_model_progress(
                cursor.lease,
                phase,
                {
                    "agent": item.agent.value,
                    "status": item.status,
                    "duration_ms": item.duration_ms,
                    "finding_count": item.finding_count,
                    "applicable_unit_count": item.applicable_unit_count,
                    "references": list(item.references),
                    "error": item.error,
                    **_agent_conclusion_payload(item),
                },
                agent=item.agent.value,
            )
        if execution.summary_execution is not None:
            item = execution.summary_execution
            _raise_if_lease_lost(cursor)
            self._queue.record_model_progress(
                cursor.lease,
                "agent_completed" if item.status == "completed" else "agent_failed",
                {
                    "agent": item.agent.value,
                    "status": item.status,
                    "duration_ms": item.duration_ms,
                    "finding_count": item.finding_count,
                    "references": list(item.references),
                    "error": item.error,
                    **_agent_conclusion_payload(item),
                },
                agent=item.agent.value,
            )
        # 汇总事件必须区分真实调用、跳过和本地确定性合并；不能在上游
        # 失败时无条件写 ``summary_completed``。
        _raise_if_lease_lost(cursor)
        if execution.summary_status == "completed":
            summary_phase = "summary_completed"
        elif execution.summary_status == "failed":
            summary_phase = "summary_failed"
        else:
            summary_phase = "summary_skipped"
        self._queue.record_model_progress(
            cursor.lease,
            summary_phase,
            {
                "agent": "summary",
                "phase": "workflow_completed",
                "agent_status": execution.summary_status,
                "agent_count": len(execution.agents),
                "finding_count": len(execution.findings),
                "aggregation_status": execution.aggregation_status,
                "summary_status": execution.summary_status,
                **_agent_conclusion_payload(execution.summary_execution),
            },
            agent="summary",
        )
        if execution.aggregation_status in {"local", "completed"}:
            self._queue.record_model_progress(
                cursor.lease,
                "aggregation_completed",
                {
                    "agent": "summary",
                    "aggregation_status": execution.aggregation_status,
                    "summary_status": execution.summary_status,
                    "finding_count": len(execution.findings),
                    "partial_result": execution.partial_result,
                },
                agent="summary",
            )
        if execution.partial_result:
            self._queue.record_model_progress(
                cursor.lease,
                "workflow_partial",
                {
                    "agent": "workflow",
                    "coverage_status": execution.coverage_status,
                    "failed_agents": [item.value for item in execution.failed_agents],
                    "failed_batches": [
                        {"agent": agent, "batch_number": number}
                        for agent, number in execution.failed_batches
                    ],
                    "finding_count": len(execution.findings),
                    "summary_status": execution.summary_status,
                },
                agent="workflow",
            )
            # 成功 Agent 已经在批次表中落盘；这里再把可验证 Finding 写入
            # 详情读模型。失败节点仍保持可重试，不改变旧 status=failed 语义。
            try:
                combined = _workflow_result(
                    model_input,
                    execution,
                    allow_partial=True,
                )
                findings = materialize_findings(model_input, combined.output)
                findings = self._verify_findings(cursor, model_input, findings)
                stored = self._queue.store_model_review(
                    cursor.lease,
                    model_input,
                    combined,
                    findings,
                    configuration_revision=ai_runtime.revision,
                    partial=True,
                )
                return stored.execution_status
            except TypeError:
                # 兼容外部注入的旧队列实现；没有 partial 参数时继续走
                # 旧错误路径，避免测试替身因签名不同而崩溃。
                pass
        if execution.status != "completed":
            failed_executions = tuple(
                item
                for item in (
                    *execution.agents,
                    execution.summary_execution,
                )
                if item is not None and item.safe_error is not None
            )
            if failed_executions:
                safe_error = failed_executions[0].safe_error
                if safe_error is not None:
                    raise SafeApplicationError(safe_error)
            raise TaskQueueError(execution.summary)
        # FixedAgentWorkflow 的候选已做稳定去重；统一持久化接口仍负责 SHA、
        # blob 和 verification 状态补齐。
        combined = _workflow_result(
            model_input,
            execution,
            allow_partial=execution.summary_status == "failed",
        )
        _raise_if_lease_lost(cursor)
        findings = materialize_findings(model_input, combined.output)
        findings = self._verify_findings(cursor, model_input, findings)
        stored = self._queue.store_model_review(
            cursor.lease,
            model_input,
            combined,
            findings,
            configuration_revision=ai_runtime.revision,
        )
        return stored.execution_status


def main() -> None:
    """配置生产 Worker 并启动可响应停止信号的主循环。

    启动步骤：
        1. 配置日志格式和级别；
        2. 从环境读取并校验 Worker 设置；
        3. 创建数据库连接池和持久化队列；
        4. 注册 SIGTERM/SIGINT 处理器；
        5. 运行主循环，并在退出时释放连接池。

    配置或数据库初始化失败会让进程以异常结束，交由 Compose 重启策略处理；
    运行期间的任务级错误由 ``WorkerRuntime`` 按租约规则处理。
    """
    logging.basicConfig(
        level=os.environ.get("OPENREVIEWER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    install_redacting_log_filters()
    settings = WorkerSettings.from_environment()
    github_api = GitHubApiClient()
    ai_runtime_provider: SqlAlchemyAiRuntimeProvider | None = None
    try:
        github_tokens = GitHubAppTokenProvider(
            github_api,
            GitHubAppSettings.from_environment(),
            GITHUB_READ_TOKEN_SCOPE,
            access_policy=GitHubAccessPolicy.from_environment(),
        )
        database = Database.from_environment()
        cipher = AiSecretCipher.from_environment()
        legacy_ai_settings = AiSettingsService(
            database.sessions,
            cipher,
        )
        agent_settings = AgentSettingsService(
            database.sessions,
            cipher,
        )
        ai_runtime_provider = SqlAlchemyAiRuntimeProvider(
            legacy_ai_settings,
            agent_settings,
            max_agent_concurrency=int(
                os.environ.get("OPENREVIEWER_AGENT_MAX_CONCURRENCY", "1")
            ),
        )
        operations_settings = OperationsSettings.from_environment()
        maintenance = WorkerMaintenance(
            OperationsService(
                SqlAlchemyOperationsRepository(database.sessions),
                operations_settings,
            )
        )
    except Exception:
        if ai_runtime_provider is not None:
            ai_runtime_provider.close()
        github_api.close()
        raise
    runtime = WorkerRuntime(
        SqlAlchemyReviewTaskQueue(database.sessions),
        settings,
        context_loader=GitHubReviewContextLoader(github_api, github_tokens),
        rule_loader=GitHubRepositoryRuleLoader(github_api, github_tokens),
        ai_runtime_provider=ai_runtime_provider,
        knowledge_base=ManagedMarkdownKnowledgeBase(
            database.sessions,
            os.environ.get("OPENREVIEWER_KNOWLEDGE_ROOT", "knowledge"),
        ),
        maintenance=maintenance,
        evidence_verifier=GitHubEvidenceVerifier(github_api, github_tokens),
        retrieval_service=HybridRetrievalService(
            RetrievalRepository(database.sessions),
            RetrievalSettingsService(database.sessions, cipher),
            source_loader=GitHubCodeSourceLoader(github_api, github_tokens, RetrievalRuntimeRepository(database.sessions)),
        ),
    )

    def stop_worker(_signum: int, _frame: object) -> None:
        """把操作系统停止信号转换为主循环可观察的停止事件。

        参数：
            _signum: 信号编号；当前只需要触发退出，不区分 SIGTERM/SIGINT。
            _frame: Python 信号处理器提供的当前栈帧，同样不参与业务逻辑。

        副作用：
            设置 ``runtime.stop_event``。主循环会在本轮任务边界结束后退出，
            然后写入 ``stopping`` 心跳并释放数据库连接。
        """
        runtime.stop_event.set()

    telemetry_server: TelemetryHttpServer | None = None
    try:
        telemetry_server = TelemetryHttpServer(
            settings.telemetry_host,
            settings.telemetry_port,
        )
        signal.signal(signal.SIGTERM, stop_worker)
        signal.signal(signal.SIGINT, stop_worker)
        telemetry_server.start()
        runtime.run()
    finally:
        if telemetry_server is not None:
            telemetry_server.close()
        ai_runtime_provider.close()
        github_api.close()
        database.dispose()


if __name__ == "__main__":
    main()
