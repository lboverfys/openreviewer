import json
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import Any, cast

import pytest

from apps.worker.main import (
    WorkerRuntime,
    _agent_conclusion_payload,
    _BusyHeartbeat,
    _dump_truncation_checkpoint,
    _LeaseCursor,
    _load_truncation_checkpoint,
    _PersistentBatchedReviewer,
    _safe_unsupported_parameters_payload,
    _workflow_result,
)
from apps.worker.settings import WorkerSettings
from domain.enums import (
    ExecutionStatus,
    ModelApiProtocol,
    ModelBatchStatus,
    ModelCallStatus,
    ModelProvider,
    ModelReviewVerdict,
    ReviewAgent,
    WorkerStatus,
)
from domain.model_review import (
    ModelReviewInput,
    ModelReviewOutput,
    ModelReviewResult,
    ModelTokenUsage,
)
from domain.security import ErrorCode, SafeApplicationError, SafeError
from domain.workflow import (
    WorkflowAction,
    WorkflowTransitionError,
    next_automatic_stage,
    transition,
)
from persistence.review_management import _project_agent_progress
from services.agent_workflow import (
    PARALLEL_AGENTS,
    AgentExecution,
    FixedAgentWorkflow,
    WorkflowExecution,
    _execution_context,
    _PartialAgentReviewError,
    scope_model_review_input,
)
from services.ai_settings import ActiveAiRuntime
from services.model_review import ModelReviewBatch, ModelReviewer, ModelServiceSettings
from services.review_management import StoredReviewEvent
from services.task_queue import (
    ModelBatchBusyError,
    ModelReviewCheckpointTooLargeError,
    ReviewTaskLease,
    ReviewTaskQueue,
    StoredModelBatch,
    TaskLeaseLostError,
    TaskQueueError,
)
from tests.unit.test_model_review import make_model_input, make_output


class StaticReviewer:
    def __init__(
        self,
        result: ModelReviewResult,
        *,
        name: str,
        calls: list[str],
        inputs: list | None = None,
    ) -> None:
        self.result = result
        self.name = name
        self.calls = calls
        self.inputs = inputs

    def review(self, review_input):
        self.calls.append(self.name)
        if self.inputs is not None:
            self.inputs.append(review_input)
        return self.result

    def close(self) -> None:
        return None


class RecordingReviewer:
    """记录意外的模型调用，供租约失败路径断言。"""

    def __init__(self) -> None:
        self.calls = 0

    def review(self, _review_input):
        self.calls += 1
        raise AssertionError("该测试路径不应调用模型")

    def close(self) -> None:
        return None


def test_fixed_workflow_propagates_task_lease_loss() -> None:
    calls: list[ReviewAgent] = []

    class LeaseLostReviewer:
        def review(self, review_input):
            calls.append(review_input.review_agent)
            raise TaskLeaseLostError()

        def close(self) -> None:
            return None

    reviewer = LeaseLostReviewer()
    reviewers = {
        agent: reviewer
        for agent in (
            ReviewAgent.SECURITY,
            ReviewAgent.CONVENTION,
            ReviewAgent.LOGIC,
        )
    }

    with pytest.raises(TaskLeaseLostError):
        FixedAgentWorkflow(reviewers, max_concurrency=1).run(make_model_input())
    assert calls == [ReviewAgent.SECURITY]


def test_parallel_workflow_cancels_queued_agents_after_lease_loss() -> None:
    """并发路径发现租约失效后，排队 Agent 不应再进入模型适配器。"""

    calls: list[ReviewAgent] = []
    convention_started = Event()
    release_convention = Event()
    security_failed = Event()
    raised: list[BaseException] = []

    class LeaseAwareReviewer:
        def review(self, review_input):
            agent = review_input.review_agent
            calls.append(agent)
            if agent is ReviewAgent.SECURITY:
                security_failed.set()
                raise TaskLeaseLostError()
            if agent is ReviewAgent.CONVENTION:
                convention_started.set()
                release_convention.wait(5)
            return model_result("1")

        def close(self) -> None:
            return None

    reviewer = LeaseAwareReviewer()
    workflow = FixedAgentWorkflow(
        {agent: reviewer for agent in PARALLEL_AGENTS},
        max_concurrency=2,
    )

    def run_workflow() -> None:
        try:
            workflow.run(make_model_input())
        except BaseException as exc:  # noqa: BLE001 - assert worker propagation
            raised.append(exc)

    thread = Thread(target=run_workflow)
    thread.start()
    assert security_failed.wait(2)
    # 让已运行的第二路 Agent 退出，避免测试线程等待超时；第三路应保持
    # 在队列中并被取消/短路。
    convention_started.wait(2)
    release_convention.set()
    thread.join(5)

    assert not thread.is_alive()
    assert raised and isinstance(raised[0], TaskLeaseLostError)
    assert ReviewAgent.LOGIC not in calls


class _OneShotEvent:
    """让忙碌心跳测试执行恰好一轮而不等待真实时间。"""

    def __init__(self) -> None:
        self._wait_count = 0

    def wait(self, _timeout: float) -> bool:
        self._wait_count += 1
        return self._wait_count >= 2


class _HeartbeatQueue:
    def __init__(self) -> None:
        self.renewed_durations: list[timedelta] = []
        self.renewed_batches: list[tuple[str, int, timedelta]] = []
        self.bulk_renew_calls = 0

    def renew_lease(
        self,
        lease: ReviewTaskLease,
        duration: timedelta,
    ) -> ReviewTaskLease:
        self.renewed_durations.append(duration)
        return lease

    def record_heartbeat(
        self,
        _worker_id: str,
        _status: object,
        _current_task_id: str | None,
        *,
        instance_id: str | None = None,
    ) -> None:
        return None

    def renew_model_batch(
        self,
        _lease: ReviewTaskLease,
        batch_number: int,
        *,
        agent: str = "default",
        lease_duration: timedelta,
    ) -> None:
        self.renewed_batches.append((agent, batch_number, lease_duration))

    def renew_model_batches(self, _lease, batches) -> None:
        self.bulk_renew_calls += 1
        self.renewed_batches.extend(
            (item.agent, item.batch_number, item.lease_duration)
            for item in batches
        )


class _BusyBatchQueue(_HeartbeatQueue):
    def record_model_progress(self, *_args, **_kwargs) -> None:
        return None

    def ensure_model_batches(self, *_args, **_kwargs):
        return ()

    def claim_model_batch(self, *_args, **_kwargs):
        raise ModelBatchBusyError(
            retry_at=datetime(2026, 8, 31, 0, 10, tzinfo=UTC),
        )

    def reserve_model_budget(self, *_args, **_kwargs):
        raise AssertionError("busy batch must not reserve a model request")

    def settle_model_budget(self, *_args, **_kwargs):
        raise AssertionError("busy batch must not settle a model request")


class _LostHeartbeatQueue(_HeartbeatQueue):
    def renew_lease(self, lease: ReviewTaskLease, duration: timedelta) -> ReviewTaskLease:
        raise TaskLeaseLostError()


class _OwnershipLostHeartbeatQueue(_HeartbeatQueue):
    def record_heartbeat(self, *_args, **_kwargs) -> None:
        raise TaskLeaseLostError("worker heartbeat ownership was lost")


class _ShutdownHeartbeatQueue:
    def __init__(self) -> None:
        self.statuses: list[WorkerStatus] = []

    def start_heartbeat(self, _worker_id: str, _instance_id: str) -> None:
        return None

    def record_heartbeat(
        self,
        _worker_id: str,
        status: WorkerStatus,
        _current_task_id: str | None,
        *,
        instance_id: str | None = None,
    ) -> None:
        self.statuses.append(status)


class _LingeringHeartbeat:
    def __init__(self, stopped: bool) -> None:
        self.stopped = stopped
        self.stop_calls = 0

    def stop(self) -> bool:
        self.stop_calls += 1
        return self.stopped

    def is_alive(self) -> bool:
        return not self.stopped


def test_busy_heartbeat_does_not_shorten_model_stage_lease() -> None:
    normal_duration = timedelta(seconds=30)
    model_duration = timedelta(seconds=600)
    lease = ReviewTaskLease(
        task_id="task-1",
        review_run_id="run-1",
        worker_id="worker-1",
        attempt_count=1,
        model_attempt_count=1,
        lease_expires_at=datetime(2026, 8, 31, tzinfo=UTC) + normal_duration,
        claimed_from_status=ExecutionStatus.READY_FOR_REVIEW,
        review_plan_id="plan-1",
    )
    queue = _HeartbeatQueue()
    cursor = _LeaseCursor(
        cast(ReviewTaskQueue, queue),
        lease,
        normal_duration,
    )

    # 模型阶段先升级到长租约；随后的一轮忙碌心跳必须继续使用同一时长。
    cursor.renew(model_duration)
    heartbeat = _BusyHeartbeat(
        cast(ReviewTaskQueue, queue),
        "worker-1",
        "task-1",
        "instance-1",
        timedelta(seconds=1),
        cursor,
    )
    heartbeat._stop_event = cast(Event, _OneShotEvent())
    heartbeat._run()

    assert queue.renewed_durations == [model_duration, model_duration]


def test_busy_heartbeat_renews_active_model_batch() -> None:
    duration = timedelta(seconds=600)
    lease = ReviewTaskLease(
        task_id="task-1",
        review_run_id="run-1",
        worker_id="worker-1",
        attempt_count=1,
        model_attempt_count=1,
        lease_expires_at=datetime(2026, 8, 31, tzinfo=UTC) + duration,
        claimed_from_status=ExecutionStatus.READY_FOR_REVIEW,
        review_plan_id="plan-1",
    )
    queue = _HeartbeatQueue()
    cursor = _LeaseCursor(cast(ReviewTaskQueue, queue), lease, duration)
    cursor.register_model_batch("security", 2, duration)
    heartbeat = _BusyHeartbeat(
        cast(ReviewTaskQueue, queue),
        "worker-1",
        "task-1",
        "instance-1",
        timedelta(seconds=1),
        cursor,
    )
    heartbeat._stop_event = cast(Event, _OneShotEvent())
    heartbeat._run()

    assert queue.renewed_batches == [("security", 2, duration)]
    assert queue.bulk_renew_calls == 1


def test_busy_heartbeat_stops_after_lease_loss() -> None:
    duration = timedelta(seconds=600)
    lease = ReviewTaskLease(
        task_id="task-1",
        review_run_id="run-1",
        worker_id="worker-1",
        attempt_count=1,
        model_attempt_count=1,
        lease_expires_at=datetime(2026, 8, 31, tzinfo=UTC) + duration,
        claimed_from_status=ExecutionStatus.READY_FOR_REVIEW,
        review_plan_id="plan-1",
    )
    queue = _LostHeartbeatQueue()
    cursor = _LeaseCursor(cast(ReviewTaskQueue, queue), lease, duration)
    heartbeat = _BusyHeartbeat(
        cast(ReviewTaskQueue, queue),
        "worker-1",
        "task-1",
        "instance-1",
        timedelta(seconds=1),
        cursor,
    )
    stop_event = _OneShotEvent()
    heartbeat._stop_event = cast(Event, stop_event)
    heartbeat._run()

    # 租约丢失后应立即退出，而不是按周期继续刷数据库。
    assert stop_event._wait_count == 1
    with pytest.raises(TaskLeaseLostError):
        cursor.raise_if_lease_lost()


def test_busy_heartbeat_stops_after_worker_instance_is_replaced() -> None:
    duration = timedelta(seconds=600)
    lease = ReviewTaskLease(
        task_id="task-1",
        review_run_id="run-1",
        worker_id="worker-1",
        attempt_count=1,
        model_attempt_count=1,
        lease_expires_at=datetime(2026, 8, 31, tzinfo=UTC) + duration,
        claimed_from_status=ExecutionStatus.READY_FOR_REVIEW,
        review_plan_id="plan-1",
    )
    queue = _OwnershipLostHeartbeatQueue()
    cursor = _LeaseCursor(cast(ReviewTaskQueue, queue), lease, duration)
    ownership_lost = Event()
    heartbeat = _BusyHeartbeat(
        cast(ReviewTaskQueue, queue),
        "worker-1",
        "task-1",
        "instance-old",
        timedelta(seconds=1),
        cursor,
        ownership_lost.set,
    )
    stop_event = _OneShotEvent()
    heartbeat._stop_event = cast(Event, stop_event)
    heartbeat._run()

    assert stop_event._wait_count == 1
    assert ownership_lost.is_set()
    with pytest.raises(TaskLeaseLostError):
        cursor.raise_if_lease_lost()


def test_worker_shutdown_skips_stopping_while_late_busy_write_is_possible() -> None:
    queue = _ShutdownHeartbeatQueue()
    stop_event = Event()
    stop_event.set()
    runtime = WorkerRuntime(
        cast(ReviewTaskQueue, queue),
        WorkerSettings(
            worker_id="worker-1",
            poll_interval=timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        ),
        stop_event=stop_event,
    )
    lingering = _LingeringHeartbeat(stopped=False)
    runtime._lingering_heartbeats.append(cast(_BusyHeartbeat, lingering))

    runtime.run()

    assert lingering.stop_calls == 1
    assert WorkerStatus.STOPPING not in queue.statuses


def test_worker_shutdown_writes_stopping_after_lingering_heartbeat_exits() -> None:
    queue = _ShutdownHeartbeatQueue()
    stop_event = Event()
    stop_event.set()
    runtime = WorkerRuntime(
        cast(ReviewTaskQueue, queue),
        WorkerSettings(
            worker_id="worker-1",
            poll_interval=timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        ),
        stop_event=stop_event,
    )
    lingering = _LingeringHeartbeat(stopped=True)
    runtime._lingering_heartbeats.append(cast(_BusyHeartbeat, lingering))

    runtime.run()

    assert lingering.stop_calls == 1
    assert queue.statuses == [WorkerStatus.STOPPING]


def test_run_once_does_not_retry_after_task_lease_is_lost() -> None:
    class Queue:
        def __init__(self) -> None:
            self.retry_calls = 0

        def start_heartbeat(self, _worker_id: str, _instance_id: str) -> None:
            return None

        def recover_expired_leases(self) -> int:
            return 0

        def record_heartbeat(
            self,
            _worker_id: str,
            _status: WorkerStatus,
            _current_task_id: str | None = None,
            *,
            instance_id: str | None = None,
        ) -> None:
            return None

        def claim_next(self, _worker_id: str, lease_duration: timedelta, **_kwargs):
            now = datetime(2026, 8, 31, tzinfo=UTC)
            return ReviewTaskLease(
                task_id="task-1",
                review_run_id="run-1",
                worker_id="worker-1",
                attempt_count=1,
                model_attempt_count=0,
                lease_expires_at=now + lease_duration,
                claimed_from_status=ExecutionStatus.QUEUED,
            )

        def retry_or_fail(self, _lease, _error) -> None:
            self.retry_calls += 1

    class LeaseLostRuntime(WorkerRuntime):
        def _advance_to_supported_boundary(self, _cursor, _ai_runtime=None):
            raise TaskLeaseLostError()

    queue = Queue()
    runtime = LeaseLostRuntime(
        cast(ReviewTaskQueue, queue),
        WorkerSettings(
            worker_id="worker-1",
            poll_interval=timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        ),
    )

    assert runtime.run_once() is True
    assert queue.retry_calls == 0
    assert not runtime.stop_event.is_set()


def test_run_once_skips_idle_after_worker_ownership_is_lost() -> None:
    """进程 token 被接管后，旧实例不得写回 idle 或继续轮询。"""

    class Queue:
        def __init__(self) -> None:
            self.statuses: list[WorkerStatus] = []

        def start_heartbeat(self, _worker_id: str, _instance_id: str) -> None:
            return None

        def recover_expired_leases(self) -> int:
            return 0

        def record_heartbeat(
            self,
            _worker_id: str,
            status: WorkerStatus,
            _current_task_id: str | None = None,
            *,
            instance_id: str | None = None,
        ) -> None:
            self.statuses.append(status)

        def claim_next(self, _worker_id: str, lease_duration: timedelta, **_kwargs):
            now = datetime(2026, 8, 31, tzinfo=UTC)
            return ReviewTaskLease(
                task_id="task-1",
                review_run_id="run-1",
                worker_id="worker-1",
                attempt_count=1,
                model_attempt_count=0,
                lease_expires_at=now + lease_duration,
                claimed_from_status=ExecutionStatus.QUEUED,
            )

        def retry_or_fail(self, *_args, **_kwargs) -> None:
            raise AssertionError("进程所有权丢失后不应上报任务失败")

    class OwnershipLostRuntime(WorkerRuntime):
        def _advance_to_supported_boundary(self, _cursor, _ai_runtime=None):
            self._mark_worker_ownership_lost()
            raise TaskLeaseLostError()

    queue = Queue()
    runtime = OwnershipLostRuntime(
        cast(ReviewTaskQueue, queue),
        WorkerSettings(
            worker_id="worker-1",
            poll_interval=timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        ),
        model_reviewer=cast(ModelReviewer, RecordingReviewer()),
    )

    assert runtime.run_once() is True
    assert runtime.stop_event.is_set()
    # 只有领取前的 idle 和领取后的 busy；收尾 idle 必须被跳过。
    assert queue.statuses == [WorkerStatus.IDLE, WorkerStatus.BUSY]


def test_model_batch_busy_error_preserves_retry_time() -> None:
    retry_at = datetime(2026, 8, 31, 0, 10, tzinfo=UTC)
    error = ModelBatchBusyError(retry_at=retry_at)

    assert error.error.code is ErrorCode.MODEL_BATCH_BUSY
    assert error.error.retryable is True
    assert error.error.details["batch_retry_managed"] is True
    assert error.error.details["retry_at"] == retry_at.isoformat()


def test_persistent_reviewer_marks_busy_as_batch_retry() -> None:
    duration = timedelta(seconds=600)
    lease = ReviewTaskLease(
        task_id="task-1",
        review_run_id="run-1",
        worker_id="worker-1",
        attempt_count=1,
        model_attempt_count=1,
        lease_expires_at=datetime(2026, 8, 31, tzinfo=UTC) + duration,
        claimed_from_status=ExecutionStatus.READY_FOR_REVIEW,
        review_plan_id="plan-1",
    )
    queue = _BusyBatchQueue()
    cursor = _LeaseCursor(cast(ReviewTaskQueue, queue), lease, duration)
    reviewer = RecordingReviewer()
    wrapped = _PersistentBatchedReviewer(
        cast(ReviewTaskQueue, queue),
        cursor,
        ReviewAgent.SECURITY,
        reviewer,
        ModelServiceSettings(
            provider=ModelProvider.OPENAI,
            model="test-model",
            api_key="test-key",
        ),
        duration,
    )

    with pytest.raises(SafeApplicationError) as raised:
        wrapped.review(make_model_input())

    assert raised.value.error.code is ErrorCode.MODEL_BATCH_BUSY
    assert raised.value.error.details["batch_retry_managed"] is True
    assert raised.value.error.details["agent"] == ReviewAgent.SECURITY.value
    assert reviewer.calls == 0


def test_persistent_reviewer_propagates_batch_lease_loss_without_failure_write() -> None:
    """批次租约失效时，旧 Worker 不应再写失败状态或失败事件。"""

    duration = timedelta(seconds=600)
    lease = ReviewTaskLease(
        task_id="task-1",
        review_run_id="run-1",
        worker_id="worker-1",
        attempt_count=1,
        model_attempt_count=1,
        lease_expires_at=datetime(2026, 8, 31, tzinfo=UTC) + duration,
        claimed_from_status=ExecutionStatus.READY_FOR_REVIEW,
        review_plan_id="plan-1",
    )

    class Queue(_HeartbeatQueue):
        def __init__(self) -> None:
            super().__init__()
            self.failed_calls = 0
            self.progress_phases: list[str] = []

        def record_model_progress(self, _lease, phase, _payload, **_kwargs) -> None:
            self.progress_phases.append(phase)

        def ensure_model_batches(self, *_args, **_kwargs):
            return ()

        def claim_model_batch(self, *_args, **_kwargs):
            return StoredModelBatch(
                id="batch-1",
                review_plan_id="plan-1",
                agent=ReviewAgent.SECURITY.value,
                batch_number=1,
                batch_count=1,
                unit_keys=("d" * 64,),
                estimated_input_tokens=10,
                status=ModelBatchStatus.RUNNING,
                attempt_count=1,
                request_fingerprint=None,
                result=None,
                error_code=None,
                error_message=None,
            )

        def complete_model_batch(self, *_args, **_kwargs):
            raise TaskLeaseLostError("批次租约已被其他 Worker 接管")

        def fail_model_batch(self, *_args, **_kwargs):
            self.failed_calls += 1

        def reserve_model_budget(self, *_args, **_kwargs):
            raise AssertionError("测试审查器不应实际预留预算")

        def settle_model_budget(self, *_args, **_kwargs):
            raise AssertionError("测试审查器不应实际结算预算")

    queue = Queue()
    cursor = _LeaseCursor(cast(ReviewTaskQueue, queue), lease, duration)
    wrapped = _PersistentBatchedReviewer(
        cast(ReviewTaskQueue, queue),
        cursor,
        ReviewAgent.SECURITY,
        StaticReviewer(model_result("1"), name="security", calls=[]),
        ModelServiceSettings(
            provider=ModelProvider.OPENAI,
            model="test-model",
            api_key="test-key",
        ),
        duration,
    )

    with pytest.raises(TaskLeaseLostError):
        wrapped.review(make_model_input())

    assert queue.failed_calls == 0
    assert "batch_failed" not in queue.progress_phases
    with pytest.raises(TaskLeaseLostError):
        cursor.renew_model_batches()
    assert queue.bulk_renew_calls == 0


@pytest.mark.parametrize("failure_stage", ("batch", "progress"))
def test_persistent_reviewer_does_not_swallow_lease_loss_during_failure_write(
    failure_stage: str,
) -> None:
    """失败状态或失败事件回写丢租约时，不能降级成普通模型失败。"""

    duration = timedelta(seconds=600)
    lease = ReviewTaskLease(
        task_id="task-1",
        review_run_id="run-1",
        worker_id="worker-1",
        attempt_count=1,
        model_attempt_count=1,
        lease_expires_at=datetime(2026, 8, 31, tzinfo=UTC) + duration,
        claimed_from_status=ExecutionStatus.READY_FOR_REVIEW,
        review_plan_id="plan-1",
    )

    class Queue(_HeartbeatQueue):
        def __init__(self) -> None:
            super().__init__()
            self.failed_calls = 0
            self.progress_phases: list[str] = []
            self.batch_failed_attempts = 0

        def record_model_progress(self, _lease, phase, _payload, **_kwargs) -> None:
            self.progress_phases.append(phase)
            if phase == "batch_failed":
                self.batch_failed_attempts += 1
                if failure_stage == "progress":
                    raise TaskLeaseLostError("失败事件写入时批次租约已被接管")

        def ensure_model_batches(self, *_args, **_kwargs):
            return ()

        def claim_model_batch(self, *_args, **_kwargs):
            return StoredModelBatch(
                id="batch-1",
                review_plan_id="plan-1",
                agent=ReviewAgent.SECURITY.value,
                batch_number=1,
                batch_count=1,
                unit_keys=("d" * 64,),
                estimated_input_tokens=10,
                status=ModelBatchStatus.RUNNING,
                attempt_count=1,
                request_fingerprint=None,
                result=None,
                error_code=None,
                error_message=None,
            )

        def fail_model_batch(self, *_args, **_kwargs):
            self.failed_calls += 1
            if failure_stage == "batch":
                raise TaskLeaseLostError("失败回写时批次租约已被接管")

        def reserve_model_budget(self, *_args, **_kwargs):
            raise AssertionError("测试审查器不应实际预留预算")

        def settle_model_budget(self, *_args, **_kwargs):
            raise AssertionError("测试审查器不应实际结算预算")

    class FailingReviewer:
        def review(self, _review_input):
            raise RuntimeError("模拟模型失败")

        def close(self) -> None:
            return None

    queue = Queue()
    cursor = _LeaseCursor(cast(ReviewTaskQueue, queue), lease, duration)
    wrapped = _PersistentBatchedReviewer(
        cast(ReviewTaskQueue, queue),
        cursor,
        ReviewAgent.SECURITY,
        FailingReviewer(),
        ModelServiceSettings(
            provider=ModelProvider.OPENAI,
            model="test-model",
            api_key="test-key",
        ),
        duration,
    )

    with pytest.raises(TaskLeaseLostError):
        wrapped.review(make_model_input())

    assert queue.failed_calls == 1
    assert queue.batch_failed_attempts == (0 if failure_stage == "batch" else 1)
    with pytest.raises(TaskLeaseLostError):
        cursor.raise_if_lease_lost()


def test_persistent_reviewer_skips_failure_write_after_async_lease_loss() -> None:
    """心跳先锁存租约失效时，随后普通模型异常也不能写 failed。"""

    duration = timedelta(seconds=600)
    lease = ReviewTaskLease(
        task_id="task-1",
        review_run_id="run-1",
        worker_id="worker-1",
        attempt_count=1,
        model_attempt_count=1,
        lease_expires_at=datetime(2026, 8, 31, tzinfo=UTC) + duration,
        claimed_from_status=ExecutionStatus.READY_FOR_REVIEW,
        review_plan_id="plan-1",
    )

    class Queue(_HeartbeatQueue):
        def __init__(self) -> None:
            super().__init__()
            self.failed_calls = 0

        def record_model_progress(self, *_args, **_kwargs) -> None:
            return None

        def ensure_model_batches(self, *_args, **_kwargs):
            return ()

        def claim_model_batch(self, *_args, **_kwargs):
            return StoredModelBatch(
                id="batch-1",
                review_plan_id="plan-1",
                agent=ReviewAgent.SECURITY.value,
                batch_number=1,
                batch_count=1,
                unit_keys=("d" * 64,),
                estimated_input_tokens=10,
                status=ModelBatchStatus.RUNNING,
                attempt_count=1,
                request_fingerprint=None,
                result=None,
                error_code=None,
                error_message=None,
            )

        def fail_model_batch(self, *_args, **_kwargs) -> None:
            self.failed_calls += 1

        def reserve_model_budget(self, *_args, **_kwargs):
            return None

        def settle_model_budget(self, *_args, **_kwargs) -> None:
            return None

    queue = Queue()
    cursor = _LeaseCursor(cast(ReviewTaskQueue, queue), lease, duration)

    class FailingReviewer:
        def review(self, _review_input):
            # 模拟后台忙碌心跳在外部请求期间先发现 token 被接管。
            cursor.mark_lease_lost()
            raise RuntimeError("模型请求随后失败")

        def close(self) -> None:
            return None

    wrapped = _PersistentBatchedReviewer(
        cast(ReviewTaskQueue, queue),
        cursor,
        ReviewAgent.SECURITY,
        FailingReviewer(),
        ModelServiceSettings(
            provider=ModelProvider.OPENAI,
            model="test-model",
            api_key="test-key",
        ),
        duration,
    )

    with pytest.raises(TaskLeaseLostError):
        wrapped.review(make_model_input())

    assert queue.failed_calls == 0


def test_legacy_batch_path_skips_failure_write_after_async_lease_loss() -> None:
    """旧 default 批次路径同样必须尊重心跳锁存的失效标记。"""

    duration = timedelta(seconds=600)
    lease = ReviewTaskLease(
        task_id="task-1",
        review_run_id="run-1",
        worker_id="worker-1",
        attempt_count=1,
        model_attempt_count=1,
        lease_expires_at=datetime(2026, 8, 31, tzinfo=UTC) + duration,
        claimed_from_status=ExecutionStatus.READY_FOR_REVIEW,
        review_plan_id="plan-1",
    )

    class Queue(_HeartbeatQueue):
        def __init__(self) -> None:
            super().__init__()
            self.failed_calls = 0

        def load_model_review_input(self, _lease):
            return make_model_input()

        def record_model_progress(self, *_args, **_kwargs) -> None:
            return None

        def ensure_model_batches(self, *_args, **_kwargs):
            return ()

        def claim_model_batch(self, *_args, **_kwargs):
            return StoredModelBatch(
                id="batch-1",
                review_plan_id="plan-1",
                agent="default",
                batch_number=1,
                batch_count=1,
                unit_keys=("d" * 64,),
                estimated_input_tokens=10,
                status=ModelBatchStatus.RUNNING,
                attempt_count=1,
                request_fingerprint=None,
                result=None,
                error_code=None,
                error_message=None,
            )

        def fail_model_batch(self, *_args, **_kwargs) -> None:
            self.failed_calls += 1

        def reserve_model_budget(self, *_args, **_kwargs):
            return None

        def settle_model_budget(self, *_args, **_kwargs) -> None:
            return None

    queue = Queue()
    cursor = _LeaseCursor(cast(ReviewTaskQueue, queue), lease, duration)

    class FailingReviewer:
        def review(self, _review_input):
            cursor.mark_lease_lost()
            raise RuntimeError("模型请求随后失败")

        def close(self) -> None:
            return None

    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="test-model",
        api_key="test-key",
    )
    ai_runtime = ActiveAiRuntime(
        revision=1,
        reviewer=FailingReviewer(),
        planner=cast(Any, None),
        model_settings=settings,
    )
    runtime = WorkerRuntime(
        cast(ReviewTaskQueue, queue),
        WorkerSettings(
            worker_id="worker-1",
            poll_interval=timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
            model_review_lease_duration=duration,
        ),
    )

    with pytest.raises(TaskLeaseLostError):
        runtime._advance_to_supported_boundary(cursor, ai_runtime)

    assert queue.failed_calls == 0


def test_run_once_skips_failure_write_after_worker_ownership_loss() -> None:
    """心跳 token 失效后，普通异常也不能触发任务失败回写。"""

    class Queue:
        def __init__(self) -> None:
            self.retry_calls = 0
            self.statuses: list[WorkerStatus] = []

        def start_heartbeat(self, _worker_id: str, _instance_id: str) -> None:
            return None

        def recover_expired_leases(self) -> int:
            return 0

        def record_heartbeat(
            self,
            _worker_id: str,
            status: WorkerStatus,
            _current_task_id: str | None = None,
            *,
            instance_id: str | None = None,
        ) -> None:
            self.statuses.append(status)

        def claim_next(self, _worker_id: str, lease_duration: timedelta, **_kwargs):
            now = datetime(2026, 8, 31, tzinfo=UTC)
            return ReviewTaskLease(
                task_id="task-1",
                review_run_id="run-1",
                worker_id="worker-1",
                attempt_count=1,
                model_attempt_count=0,
                lease_expires_at=now + lease_duration,
                claimed_from_status=ExecutionStatus.QUEUED,
            )

        def retry_or_fail(self, *_args, **_kwargs) -> None:
            self.retry_calls += 1

    class OwnershipLostRuntime(WorkerRuntime):
        def _advance_to_supported_boundary(self, _cursor, _ai_runtime=None):
            self._mark_worker_ownership_lost()
            raise RuntimeError("处理期间进程 token 被接管")

    queue = Queue()
    runtime = OwnershipLostRuntime(
        cast(ReviewTaskQueue, queue),
        WorkerSettings(
            worker_id="worker-1",
            poll_interval=timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        ),
        model_reviewer=cast(ModelReviewer, RecordingReviewer()),
    )

    assert runtime.run_once() is True
    assert queue.retry_calls == 0
    assert runtime.stop_event.is_set()


def model_result(
    fingerprint_digit: str,
    *,
    provider: ModelProvider = ModelProvider.OPENAI,
    protocol: ModelApiProtocol = ModelApiProtocol.CHAT_COMPLETIONS,
    model: str = "test-model",
    status: ModelCallStatus = ModelCallStatus.SUCCEEDED,
    request_id: str | None = None,
) -> ModelReviewResult:
    succeeded = status is ModelCallStatus.SUCCEEDED
    return ModelReviewResult(
        provider=provider,
        api_protocol=protocol,
        model=model,
        status=status,
        prompt_version="test",
        request_fingerprint=fingerprint_digit * 64,
        provider_request_id=request_id if succeeded else None,
        response_status=200 if succeeded else None,
        duration_ms=10,
        usage=(
            ModelTokenUsage(input_tokens=10, output_tokens=2)
            if succeeded
            else ModelTokenUsage(input_tokens=0, output_tokens=0)
        ),
        output=(
            ModelReviewOutput(
                verdict=ModelReviewVerdict.NO_ACTIONABLE_ISSUE,
                summary="当前审查范围内未发现可报告问题。",
                checked_areas=("测试范围",),
                findings=(),
            )
            if succeeded
            else ModelReviewOutput(findings=())
        ),
    )


def test_empty_batch_path_does_not_require_resource_budget() -> None:
    calls: list[str] = []
    reviewer = StaticReviewer(model_result("1"), name="security", calls=calls)

    class LeaseCursor:
        def raise_if_lease_lost(self) -> None:
            return None

    wrapped = _PersistentBatchedReviewer(
        cast(ReviewTaskQueue, object()),
        cast(_LeaseCursor, LeaseCursor()),
        ReviewAgent.SECURITY,
        reviewer,
        ModelServiceSettings(
            provider=ModelProvider.OPENAI,
            model="test-model",
            api_key="test-key",
        ),
        timedelta(minutes=10),
    )
    empty_input = make_model_input().model_copy(
        update={"units": (), "total_estimated_input_bytes": 0}
    )

    result = wrapped.review(empty_input)

    assert result.status is ModelCallStatus.SUCCEEDED
    assert calls == ["security"]


def test_fixed_dag_automatic_edges() -> None:
    assert next_automatic_stage(ExecutionStatus.CI) is ExecutionStatus.PLANNING
    assert (
        next_automatic_stage(ExecutionStatus.AGENT_BATCHES)
        is ExecutionStatus.AGGREGATING
    )
    assert next_automatic_stage(ExecutionStatus.AWAITING_APPROVAL) is None


def test_approval_can_only_open_manual_publish_gate() -> None:
    result = transition(ExecutionStatus.AWAITING_APPROVAL, WorkflowAction.APPROVE)
    assert result.after is ExecutionStatus.APPROVED
    assert next_automatic_stage(result.after) is ExecutionStatus.AWAITING_PUBLISH
    with pytest.raises(WorkflowTransitionError):
        transition(ExecutionStatus.AGENT_BATCHES, WorkflowAction.APPROVE)
    with pytest.raises(WorkflowTransitionError):
        transition(ExecutionStatus.AWAITING_APPROVAL, WorkflowAction.PUBLISH)


def test_reject_and_stage_retry_are_explicit() -> None:
    rejected = transition(ExecutionStatus.AWAITING_APPROVAL, WorkflowAction.REJECT)
    assert rejected.after is ExecutionStatus.REJECTED
    retried = transition(
        ExecutionStatus.REJECTED,
        WorkflowAction.RETRY_STAGE,
        target_stage=ExecutionStatus.AGENT_BATCHES,
    )
    assert retried.after is ExecutionStatus.AGENT_BATCHES


def test_approved_review_can_be_rejected_before_publish() -> None:
    rejected = transition(
        ExecutionStatus.AWAITING_PUBLISH,
        WorkflowAction.REJECT,
    )

    assert rejected.after is ExecutionStatus.REJECTED


def test_pause_only_accepts_persistable_resume_origins() -> None:
    paused = transition(ExecutionStatus.AGGREGATING, WorkflowAction.PAUSE)
    assert paused.after is ExecutionStatus.PAUSED

    with pytest.raises(WorkflowTransitionError):
        transition(ExecutionStatus.WAITING_FOR_CI, WorkflowAction.PAUSE)
    with pytest.raises(WorkflowTransitionError):
        transition(ExecutionStatus.PUBLISHING, WorkflowAction.PAUSE)


def test_fixed_agents_mark_aggregating_before_summary() -> None:
    calls: list[str] = []
    reviewers = {
        agent: StaticReviewer(
            model_result(str(index)),
            name=agent.value,
            calls=calls,
        )
        for index, agent in enumerate(
            (
                ReviewAgent.SECURITY,
                ReviewAgent.CONVENTION,
                ReviewAgent.LOGIC,
            ),
            start=1,
        )
    }
    summary = StaticReviewer(
        model_result("4", model="summary-model"),
        name="summary",
        calls=calls,
    )
    workflow = FixedAgentWorkflow(
        reviewers,
        summary_reviewer=summary,
        max_concurrency=3,
    )

    execution = workflow.run(
        make_model_input(),
        on_aggregating=lambda: calls.append("aggregating"),
    )

    assert execution.status == "completed"
    assert set(calls[:3]) == {"security", "convention", "logic"}
    assert calls[-2:] == ["aggregating", "summary"]


def test_non_success_agent_skips_aggregating_and_summary() -> None:
    calls: list[str] = []
    reviewers = {
        ReviewAgent.SECURITY: StaticReviewer(
            model_result("1", status=ModelCallStatus.SKIPPED),
            name="security",
            calls=calls,
        ),
        ReviewAgent.CONVENTION: StaticReviewer(
            model_result("2"),
            name="convention",
            calls=calls,
        ),
        ReviewAgent.LOGIC: StaticReviewer(
            model_result("3"),
            name="logic",
            calls=calls,
        ),
    }
    summary = StaticReviewer(
        model_result("4"),
        name="summary",
        calls=calls,
    )
    workflow = FixedAgentWorkflow(reviewers, summary_reviewer=summary)

    execution = workflow.run(
        make_model_input(),
        on_aggregating=lambda: calls.append("aggregating"),
    )

    assert execution.status == "failed"
    assert "aggregating" not in calls
    assert "summary" not in calls
    security = next(
        item for item in execution.agents if item.agent is ReviewAgent.SECURITY
    )
    assert security.status == "failed"
    assert security.error == "Agent 未返回成功结果"


def test_empty_review_completes_with_skipped_agents() -> None:
    calls: list[str] = []
    skipped_input = make_model_input().model_copy(
        update={"units": (), "total_estimated_input_bytes": 0}
    )
    reviewers = {
        agent: StaticReviewer(
            model_result(str(index), status=ModelCallStatus.SKIPPED),
            name=agent.value,
            calls=calls,
        )
        for index, agent in enumerate(
            (
                ReviewAgent.SECURITY,
                ReviewAgent.CONVENTION,
                ReviewAgent.LOGIC,
            ),
            start=1,
        )
    }
    summary = StaticReviewer(
        model_result("4", status=ModelCallStatus.SKIPPED),
        name="summary",
        calls=calls,
    )
    workflow = FixedAgentWorkflow(reviewers, summary_reviewer=summary)

    execution = workflow.run(
        skipped_input,
        on_aggregating=lambda: calls.append("aggregating"),
    )
    combined = _workflow_result(skipped_input, execution)

    assert execution.status == "completed"
    assert execution.summary_execution is None
    assert calls[-1] == "aggregating"
    assert "summary" not in calls
    assert combined.status is ModelCallStatus.SKIPPED
    assert combined.response_status is None
    assert combined.usage.total_input_tokens == 0


def test_fixed_agents_receive_scoped_knowledge_references() -> None:
    calls: list[str] = []
    inputs: list = []
    reviewers = {
        agent: StaticReviewer(
            model_result(str(index)),
            name=agent.value,
            calls=calls,
            inputs=inputs,
        )
        for index, agent in enumerate(
            (
                ReviewAgent.SECURITY,
                ReviewAgent.CONVENTION,
                ReviewAgent.LOGIC,
            ),
            start=1,
        )
    }
    summary = StaticReviewer(
        model_result("4"),
        name="summary",
        calls=calls,
        inputs=inputs,
    )
    references = {
        agent: (f"{agent.value}-reference",)
        for agent in ReviewAgent
    }

    FixedAgentWorkflow(reviewers, summary_reviewer=summary).run(
        make_model_input(),
        references=references,
    )

    by_reference = {
        item.knowledge_references[0]: item for item in inputs
    }
    assert set(by_reference) == {
        "security-reference",
        "convention-reference",
        "logic-reference",
        "summary-reference",
    }
    assert by_reference["summary-reference"].prior_agent_results
    for agent in ReviewAgent:
        assert by_reference[f"{agent.value}-reference"].review_agent is agent

    summary_input = by_reference["summary-reference"]
    assert summary_input.units == ()
    assert summary_input.rules == ()
    assert summary_input.total_estimated_input_bytes == 0


def test_summary_execution_context_is_bounded_and_valid_json() -> None:
    finding = make_output().findings[0].model_copy(
        update={
            "title": "问题" * 300,
            "evidence": "证据" * 600,
            "impact": "影响" * 600,
            "suggestion": "建议" * 600,
        }
    )
    output = model_result("1").output.model_copy(
        update={
            "summary": "总结" * 1_000,
            "findings": (finding,),
        }
    )
    execution = AgentExecution(
        ReviewAgent.SECURITY,
        "completed",
        model_result("1").model_copy(update={"output": output}),
        1,
        None,
    )

    encoded = _execution_context((execution,))[0]

    assert len(encoded.encode("utf-8")) <= 2_000
    payload = json.loads(encoded)
    assert payload["finding_count"] == 1
    assert payload["findings"]
    assert payload["findings"][0]["evidence"] != finding.evidence


def test_summary_findings_are_limited_to_current_plan_before_materialization() -> None:
    """汇总模型只能复用当前计划中的 unit、规则和文件位置。"""

    source = make_model_input()
    valid = make_output().findings[0]
    unknown_unit = valid.model_copy(update={"unit_key": "f" * 64})
    unknown_rule = valid.model_copy(update={"rule_reference": "OTHER.md"})
    assert valid.location is not None
    wrong_file = valid.model_copy(
        update={
            "location": valid.location.model_copy(update={"file": "src/other.py"})
        }
    )
    summary_output = ModelReviewOutput(
        verdict=ModelReviewVerdict.ISSUES_FOUND,
        summary="汇总候选",
        checked_areas=("汇总",),
        findings=(unknown_unit, unknown_rule, wrong_file, valid),
    )
    calls: list[str] = []
    reviewers = {
        agent: StaticReviewer(
            model_result(str(index)),
            name=agent.value,
            calls=calls,
        )
        for index, agent in enumerate(
            (
                ReviewAgent.SECURITY,
                ReviewAgent.CONVENTION,
                ReviewAgent.LOGIC,
            ),
            start=1,
        )
    }
    summary = StaticReviewer(
        model_result("4").model_copy(update={"output": summary_output}),
        name="summary",
        calls=calls,
    )

    execution = FixedAgentWorkflow(
        reviewers,
        summary_reviewer=summary,
    ).run(source)

    # 非法汇总候选被丢弃，合法候选仍可进入统一持久化/定位复核。
    assert execution.status == "completed"
    assert execution.findings == (valid,)
    assert execution.summary_execution is not None
    assert execution.summary_execution.finding_count == 1
    assert execution.summary_execution.result is not None
    assert execution.summary_execution.result.output.findings == (valid,)
    combined = _workflow_result(source, execution)
    assert len(combined.output.findings) == 1


def test_unsupported_parameters_payload_is_bounded_and_json_safe() -> None:
    details = {
        "unsupported_parameters": [
            " max_completion_tokens ",
            "reasoning_effort",
            "\nnot-safe",
            "x" * 200,
            123,
        ]
    }

    assert _safe_unsupported_parameters_payload(details) == [
        "max_completion_tokens",
        "reasoning_effort",
    ]
    assert _safe_unsupported_parameters_payload(
        {"unsupported_parameters": "max_tokens"}
    ) is None


def test_workflow_compatibility_result_uses_summary_configuration() -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    security = model_result("1", model="security-model")
    convention = model_result(
        "2",
        provider=ModelProvider.ANTHROPIC,
        protocol=ModelApiProtocol.MESSAGES,
        model="convention-model",
    )
    logic = model_result(
        "3",
        protocol=ModelApiProtocol.RESPONSES,
        model="logic-model",
    )
    summary = model_result(
        "4",
        provider=ModelProvider.ANTHROPIC,
        protocol=ModelApiProtocol.MESSAGES,
        model="summary-model",
        request_id="summary-request",
    )
    execution = WorkflowExecution(
        status="completed",
        agents=(
            AgentExecution(ReviewAgent.SECURITY, "completed", security, 10, None),
            AgentExecution(ReviewAgent.CONVENTION, "completed", convention, 10, None),
            AgentExecution(ReviewAgent.LOGIC, "completed", logic, 10, None),
        ),
        findings=(),
        summary="完成",
        started_at=now,
        completed_at=now,
        summary_execution=AgentExecution(
            ReviewAgent.SUMMARY,
            "completed",
            summary,
            10,
            None,
        ),
    )

    combined = _workflow_result(make_model_input(), execution)

    assert combined.provider is ModelProvider.ANTHROPIC
    assert combined.api_protocol is ModelApiProtocol.MESSAGES
    assert combined.model == "summary-model"
    assert combined.provider_request_id == "summary-request"
    assert combined.usage.input_tokens == 40
    assert combined.usage.output_tokens == 8
    assert combined.duration_ms == 40
    assert combined.output.verdict is ModelReviewVerdict.NO_ACTIONABLE_ISSUE
    assert combined.output.summary == "当前审查范围内未发现可报告问题。"


def test_agent_completion_event_exposes_only_the_structured_conclusion() -> None:
    execution = AgentExecution(
        ReviewAgent.SECURITY,
        "completed",
        model_result("1"),
        10,
        None,
    )

    payload = _agent_conclusion_payload(execution)

    assert payload == {
        "verdict": "no_actionable_issue",
        "summary": "当前审查范围内未发现可报告问题。",
        "checked_areas": ["测试范围"],
    }
    assert "findings" not in payload
    assert "raw_response" not in payload


def test_workflow_result_keeps_recovered_legacy_summary_compatible() -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    legacy_summary = model_result("4").model_copy(
        update={"output": ModelReviewOutput(findings=())}
    )
    agent_results = tuple(model_result(str(index)) for index in range(1, 4))
    execution = WorkflowExecution(
        status="completed",
        agents=tuple(
            AgentExecution(agent, "completed", result, 10, None)
            for agent, result in zip(
                (
                    ReviewAgent.SECURITY,
                    ReviewAgent.CONVENTION,
                    ReviewAgent.LOGIC,
                ),
                agent_results,
                strict=True,
            )
        ),
        findings=(),
        summary="旧批次恢复完成",
        started_at=now,
        completed_at=now,
        summary_execution=AgentExecution(
            ReviewAgent.SUMMARY,
            "completed",
            legacy_summary,
            10,
            None,
        ),
    )

    combined = _workflow_result(make_model_input(), execution)

    assert combined.output.verdict is None
    assert combined.output.summary is None
    assert combined.output.checked_areas == ()


def test_workflow_compatibility_result_rejects_partial_success() -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    skipped = model_result("1", status=ModelCallStatus.SKIPPED)
    execution = WorkflowExecution(
        status="failed",
        agents=(
            AgentExecution(ReviewAgent.SECURITY, "failed", skipped, 0, "跳过"),
        ),
        findings=(),
        summary="不完整",
        started_at=now,
        completed_at=now,
    )

    with pytest.raises(TaskQueueError):
        _workflow_result(make_model_input(), execution)


def test_truncation_split_checkpoint_reuses_completed_children() -> None:
    """截断后的子请求在 Worker 重启/重试时不得重复调用。"""

    source = make_model_input()
    second_unit = source.units[0].model_copy(
        update={
            "unit_key": "e" * 64,
            "file": "src/other.py",
            "patch_sha256": source.units[0].patch_sha256,
        }
    )
    source = source.model_copy(
        update={
            "units": (source.units[0], second_unit),
            "total_estimated_input_bytes": (
                source.units[0].estimated_input_bytes
                + second_unit.estimated_input_bytes
            ),
        }
    )

    class TruncatingReviewer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def review(self, review_input):
            self.calls.append(tuple(unit.unit_key for unit in review_input.units))
            if len(review_input.units) > 1:
                raise SafeApplicationError(
                    SafeError(
                        code=ErrorCode.MODEL_OUTPUT_TRUNCATED,
                        safe_message="输出被截断",
                        retryable=True,
                    )
                )
            return model_result(str(len(self.calls)))

        def close(self) -> None:
            return None

    reviewer = TruncatingReviewer()
    wrapped = _PersistentBatchedReviewer(
        cast(ReviewTaskQueue, object()),
        cast(_LeaseCursor, object()),
        ReviewAgent.SECURITY,
        reviewer,
        ModelServiceSettings(
            provider=ModelProvider.OPENAI,
            model="test-model",
            api_key="test-key",
        ),
        timedelta(minutes=10),
    )
    results: dict[str, ModelReviewResult] = {}
    split_nodes: set[str] = set()

    def checkpoint(key: str, result: ModelReviewResult | None) -> None:
        if result is None:
            split_nodes.add(key)
        else:
            results[key] = result

    first = wrapped._review_with_truncation_split(
        source,
        checkpoint_results=results,
        checkpoint_split_nodes=split_nodes,
        on_checkpoint=checkpoint,
    )
    assert len(reviewer.calls) == 3
    payload = _dump_truncation_checkpoint(
        results,
        split_nodes,
        root_key="root",
    )
    loaded_results, loaded_splits = _load_truncation_checkpoint(payload)
    assert loaded_results
    assert split_nodes

    retry_reviewer = TruncatingReviewer()
    retry = _PersistentBatchedReviewer(
        cast(ReviewTaskQueue, object()),
        cast(_LeaseCursor, object()),
        ReviewAgent.SECURITY,
        retry_reviewer,
        ModelServiceSettings(
            provider=ModelProvider.OPENAI,
            model="test-model",
            api_key="test-key",
        ),
        timedelta(minutes=10),
    )
    resumed = retry._review_with_truncation_split(
        source,
        checkpoint_results=loaded_results,
        checkpoint_split_nodes=loaded_splits,
    )
    assert retry_reviewer.calls == []
    assert resumed == first


def test_partial_agent_result_keeps_completed_batches_for_workflow_aggregation() -> None:
    """后置批次失败时，前置批次结果仍应进入部分汇总。"""

    source = make_model_input()
    partial = model_result("1").model_copy(update={"output": make_output()})
    partial_error = SafeError(
        code=ErrorCode.MODEL_TIMEOUT,
        safe_message="第二批模型请求超时",
        retryable=True,
        details={"batch_number": 2},
    )

    class PartialReviewer:
        def review(self, _review_input):
            raise _PartialAgentReviewError(
                partial_error,
                partial_result=partial,
                completed_batches=(1,),
                failed_batch=2,
            )

        def close(self) -> None:
            return None

    reviewers = {
        ReviewAgent.SECURITY: PartialReviewer(),
        ReviewAgent.CONVENTION: StaticReviewer(
            model_result("2"), name="convention", calls=[]
        ),
        ReviewAgent.LOGIC: StaticReviewer(model_result("3"), name="logic", calls=[]),
    }
    execution = FixedAgentWorkflow(reviewers, max_concurrency=1).run(
        source,
        allow_partial_aggregation=True,
    )

    security = next(
        item for item in execution.agents if item.agent is ReviewAgent.SECURITY
    )
    assert security.status == "failed"
    assert security.result == partial
    assert security.completed_batches == (1,)
    assert security.failed_batches == (2,)
    assert execution.partial_result is True
    assert execution.failed_agents == (ReviewAgent.SECURITY,)
    assert execution.failed_batches == ((ReviewAgent.SECURITY.value, 2),)

    combined = _workflow_result(source, execution, allow_partial=True)
    assert combined.output.findings


def test_disabled_agent_is_not_reported_as_retryable_failure() -> None:
    source = make_model_input()
    reviewers = {
        ReviewAgent.CONVENTION: StaticReviewer(
            model_result("2"), name="convention", calls=[]
        ),
        ReviewAgent.LOGIC: StaticReviewer(model_result("3"), name="logic", calls=[]),
    }

    execution = FixedAgentWorkflow(reviewers, max_concurrency=1).run(
        source,
        allow_partial_aggregation=True,
    )

    security = next(
        item for item in execution.agents if item.agent is ReviewAgent.SECURITY
    )
    assert security.status == "disabled"
    assert execution.partial_result is True
    assert execution.failed_agents == ()


def test_workflow_sends_only_units_in_each_agent_responsibility() -> None:
    source = make_model_input()
    security_unit = source.units[0].model_copy(
        update={
            "unit_key": "f" * 64,
            "file": "src/security.py",
            "review_domains": (ReviewAgent.SECURITY,),
        }
    )
    logic_unit = source.units[0].model_copy(
        update={
            "unit_key": "a" * 64,
            "file": "src/orders.py",
            "review_domains": (ReviewAgent.CONVENTION, ReviewAgent.LOGIC),
        }
    )
    source = source.model_copy(
        update={
            "units": (logic_unit, security_unit),
            "total_estimated_input_bytes": (
                security_unit.estimated_input_bytes + logic_unit.estimated_input_bytes
            ),
        }
    )

    assert tuple(unit.file for unit in scope_model_review_input(source, ReviewAgent.SECURITY).units) == (
        "src/security.py",
    )
    assert tuple(unit.file for unit in scope_model_review_input(source, ReviewAgent.LOGIC).units) == (
        "src/orders.py",
    )

    calls: dict[ReviewAgent, list[ModelReviewInput]] = {
        agent: []
        for agent in (ReviewAgent.SECURITY, ReviewAgent.CONVENTION, ReviewAgent.LOGIC)
    }
    reviewers = {
        agent: StaticReviewer(
            model_result(str(index)),
            name=agent.value,
            calls=[],
            inputs=calls[agent],
        )
        for index, agent in enumerate(calls, start=1)
    }
    execution = FixedAgentWorkflow(reviewers, max_concurrency=1).run(source)

    assert [item.file for item in calls[ReviewAgent.SECURITY][0].units] == [
        "src/security.py"
    ]
    assert [item.file for item in calls[ReviewAgent.CONVENTION][0].units] == [
        "src/orders.py"
    ]
    assert [item.file for item in calls[ReviewAgent.LOGIC][0].units] == [
        "src/orders.py"
    ]
    assert all(item.status == "completed" for item in execution.agents)


def test_workflow_marks_unmatched_agent_as_not_applicable() -> None:
    base = make_model_input()
    source = base.model_copy(
        update={
            "units": (
                base.units[0].model_copy(
                    update={"review_domains": (ReviewAgent.LOGIC,)}
                ),
            )
        }
    )
    calls: list[str] = []
    reviewers = {
        ReviewAgent.LOGIC: StaticReviewer(
            model_result("1"), name="logic", calls=calls
        )
    }

    execution = FixedAgentWorkflow(reviewers, max_concurrency=1).run(source)

    statuses = {item.agent: item.status for item in execution.agents}
    assert statuses[ReviewAgent.SECURITY] == "not_applicable"
    assert statuses[ReviewAgent.CONVENTION] == "not_applicable"
    assert statuses[ReviewAgent.LOGIC] == "completed"
    assert calls == ["logic"]
    assert execution.status == "completed"


def test_agent_progress_projection_distinguishes_disabled_from_failed() -> None:
    events = (
        StoredReviewEvent(
            id="disabled",
            event_type="review.model.agent_failed",
            payload={
                "agent": ReviewAgent.SECURITY.value,
                "status": "disabled",
            },
            occurred_at=datetime(2026, 8, 31, tzinfo=UTC),
        ),
    )

    statuses, _summaries, _aggregation, _summary, _partial, failed, batches = (
        _project_agent_progress(
            events,
            coverage_status="partial",
            model_completed=False,
        )
    )

    assert statuses[ReviewAgent.SECURITY.value] == "disabled"
    assert failed == ()
    assert batches == ()


def test_agent_progress_projection_ignores_legacy_events_after_new_attempt() -> None:
    old_time = datetime(2026, 8, 30, tzinfo=UTC)
    new_time = datetime(2026, 8, 31, tzinfo=UTC)
    events = (
        StoredReviewEvent(
            id="old-completed",
            event_type="review.model.agent_completed",
            payload={
                "agent": ReviewAgent.CONVENTION.value,
                "status": "completed",
            },
            occurred_at=old_time,
        ),
        StoredReviewEvent(
            id="new-started",
            event_type="review.model.retry_started",
            payload={
                "agent": "workflow",
                "model_attempt_count": 2,
            },
            occurred_at=new_time,
        ),
        StoredReviewEvent(
            id="new-failed",
            event_type="review.model.agent_failed",
            payload={
                "agent": ReviewAgent.SECURITY.value,
                "status": "failed",
                "model_attempt_count": 2,
            },
            occurred_at=new_time,
        ),
    )

    statuses, _summaries, _aggregation, _summary, _partial, failed, _batches = (
        _project_agent_progress(
            events,
            coverage_status="partial",
            model_completed=False,
        )
    )

    assert statuses[ReviewAgent.SECURITY.value] == "failed"
    assert statuses[ReviewAgent.CONVENTION.value] == "waiting"
    assert failed == (ReviewAgent.SECURITY.value,)


def test_persistent_reviewer_exposes_prior_batch_after_later_failure(monkeypatch) -> None:
    """持久化批次循环失败时应把前置成功结果交给工作流。"""

    source = make_model_input()
    batches = tuple(
        ModelReviewBatch(
            number=number,
            total=2,
            review_input=source,
            estimated_input_tokens=10,
        )
        for number in (1, 2)
    )

    import apps.worker.batches as worker_main

    monkeypatch.setattr(worker_main, "plan_model_review_batches", lambda *_args, **_kwargs: batches)

    class Queue(_HeartbeatQueue):
        def record_model_progress(self, *_args, **_kwargs) -> None:
            return None

        def ensure_model_batches(self, *_args, **_kwargs):
            return ()

        def claim_model_batch(self, _lease, batch_number, **_kwargs):
            return StoredModelBatch(
                id=f"batch-{batch_number}",
                review_plan_id="plan-1",
                agent=ReviewAgent.SECURITY.value,
                batch_number=batch_number,
                batch_count=2,
                unit_keys=("d" * 64,),
                estimated_input_tokens=10,
                status=ModelBatchStatus.RUNNING,
                attempt_count=1,
                request_fingerprint=None,
                result=None,
                error_code=None,
                error_message=None,
            )

        def complete_model_batch(self, *_args, **_kwargs):
            return None

        def fail_model_batch(self, *_args, **_kwargs):
            return None

        def reserve_model_budget(self, *_args, **_kwargs):
            return None

        def settle_model_budget(self, *_args, **_kwargs):
            return None

    class Reviewer:
        def __init__(self) -> None:
            self.calls = 0

        def review(self, _review_input):
            self.calls += 1
            if self.calls == 1:
                return model_result("1")
            raise RuntimeError("第二批失败")

        def close(self) -> None:
            return None

    now = datetime(2026, 8, 31, tzinfo=UTC)
    lease = ReviewTaskLease(
        task_id="task-1",
        review_run_id="run-1",
        worker_id="worker-1",
        attempt_count=1,
        model_attempt_count=1,
        lease_expires_at=now + timedelta(minutes=10),
        claimed_from_status=ExecutionStatus.READY_FOR_REVIEW,
        review_plan_id="plan-1",
    )
    queue = Queue()
    cursor = _LeaseCursor(cast(ReviewTaskQueue, queue), lease, timedelta(minutes=10))
    reviewer = Reviewer()
    wrapped = _PersistentBatchedReviewer(
        cast(ReviewTaskQueue, queue),
        cursor,
        ReviewAgent.SECURITY,
        reviewer,
        ModelServiceSettings(
            provider=ModelProvider.OPENAI,
            model="test-model",
            api_key="test-key",
        ),
        timedelta(minutes=10),
    )

    with pytest.raises(_PartialAgentReviewError) as raised:
        wrapped.review(source)

    assert reviewer.calls == 2
    assert raised.value.partial_result.status is ModelCallStatus.SUCCEEDED
    assert raised.value.completed_batches == (1,)
    assert raised.value.failed_batch == 2


def test_checkpoint_size_limit_does_not_fail_successful_model_batch(monkeypatch) -> None:
    """检查点过大时只放弃恢复快照，不能把模型成功改成批次失败。"""

    source = make_model_input()
    second_unit = source.units[0].model_copy(
        update={"unit_key": "e" * 64, "file": "src/other.py"}
    )
    source = source.model_copy(
        update={
            "units": (source.units[0], second_unit),
            "total_estimated_input_bytes": sum(
                unit.estimated_input_bytes for unit in (source.units[0], second_unit)
            ),
        }
    )
    batch = ModelReviewBatch(
        number=1,
        total=1,
        review_input=source,
        estimated_input_tokens=10,
    )

    import apps.worker.batches as worker_main

    monkeypatch.setattr(worker_main, "plan_model_review_batches", lambda *_args, **_kwargs: (batch,))

    class Queue(_HeartbeatQueue):
        def record_model_progress(self, *_args, **_kwargs) -> None:
            return None

        def ensure_model_batches(self, *_args, **_kwargs):
            return ()

        def claim_model_batch(self, _lease, _batch_number, **_kwargs):
            return StoredModelBatch(
                id="batch-1",
                review_plan_id="plan-1",
                agent=ReviewAgent.SECURITY.value,
                batch_number=1,
                batch_count=1,
                unit_keys=tuple(unit.unit_key for unit in source.units),
                estimated_input_tokens=10,
                status=ModelBatchStatus.RUNNING,
                attempt_count=1,
                request_fingerprint=None,
                result=None,
                error_code=None,
                error_message=None,
            )

        def checkpoint_model_batch(self, *_args, **_kwargs):
            raise ModelReviewCheckpointTooLargeError()

        def complete_model_batch(self, *_args, **_kwargs):
            return None

        def fail_model_batch(self, *_args, **_kwargs):
            raise AssertionError("检查点过大不应触发批次失败")

        def reserve_model_budget(self, *_args, **_kwargs):
            return None

        def settle_model_budget(self, *_args, **_kwargs):
            return None

    class Reviewer:
        def __init__(self) -> None:
            self.calls = 0

        def review(self, review_input):
            self.calls += 1
            if len(review_input.units) > 1:
                raise SafeApplicationError(
                    SafeError(
                        code=ErrorCode.MODEL_OUTPUT_TRUNCATED,
                        safe_message="输出被截断",
                        retryable=True,
                    )
                )
            return model_result("1").model_copy(
                update={"output": ModelReviewOutput(findings=())}
            )

        def close(self) -> None:
            return None

    now = datetime(2026, 8, 31, tzinfo=UTC)
    lease = ReviewTaskLease(
        task_id="task-1",
        review_run_id="run-1",
        worker_id="worker-1",
        attempt_count=1,
        model_attempt_count=1,
        lease_expires_at=now + timedelta(minutes=10),
        claimed_from_status=ExecutionStatus.READY_FOR_REVIEW,
        review_plan_id="plan-1",
    )
    queue = Queue()
    reviewer = Reviewer()
    wrapped = _PersistentBatchedReviewer(
        cast(ReviewTaskQueue, queue),
        _LeaseCursor(cast(ReviewTaskQueue, queue), lease, timedelta(minutes=10)),
        ReviewAgent.SECURITY,
        reviewer,
        ModelServiceSettings(
            provider=ModelProvider.OPENAI,
            model="test-model",
            api_key="test-key",
        ),
        timedelta(minutes=10),
    )

    result = wrapped.review(source)

    assert result.status is ModelCallStatus.SUCCEEDED
    assert reviewer.calls == 3
