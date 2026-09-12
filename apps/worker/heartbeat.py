"""Worker heartbeat 职责模块。"""

from collections.abc import Callable
from datetime import timedelta
from inspect import Parameter, signature
from threading import Event, Lock, RLock, Thread

from apps.worker.logging_context import LOGGER
from domain.enums import WorkerStatus
from domain.security import ErrorCode, SafeError
from services.task_queue import (
    ModelBatchLease,
    ReviewTaskLease,
    ReviewTaskQueue,
    TaskLeaseLostError,
    TaskQueueError,
)


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
            parameter.name == "instance_id" or parameter.kind is Parameter.VAR_KEYWORD
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
            requested_duration = self._active_duration if duration is None else duration
            # 阶段租约只允许延长，不允许被并发心跳或旧调用路径缩短。
            effective_duration = max(self._active_duration, requested_duration)
            try:
                self.lease = self._queue.renew_lease(
                    self.lease,
                    effective_duration,
                )
            except TaskQueueError as exc:
                if SafeError.from_exception(exc).code is ErrorCode.TASK_LEASE_LOST:
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
                if SafeError.from_exception(exc).code is ErrorCode.TASK_LEASE_LOST:
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
                parameter.name == "agent" or parameter.kind is Parameter.VAR_KEYWORD
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
                    if SafeError.from_exception(exc).code is ErrorCode.TASK_LEASE_LOST:
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
                    if SafeError.from_exception(exc).code is ErrorCode.TASK_LEASE_LOST:
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
                    LOGGER.exception(
                        "Worker 任务租约已丢失，当前模型请求不会再写入结果"
                    )
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
