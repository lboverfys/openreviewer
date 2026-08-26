"""单并发数据库 Worker 的进程入口。"""

from dataclasses import dataclass
from datetime import timedelta
import logging
import os
import signal
import socket
from threading import Event, Thread

from domain.enums import ExecutionStatus, WorkerStatus
from domain.model_review import materialize_findings
from domain.security import SafeError, install_redacting_log_filters
from persistence.database import Database
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from services.ai_settings import (
    ActiveAiRuntime,
    AiRuntimeProvider,
    AiSecretCipher,
    AiSettingsService,
    SqlAlchemyAiRuntimeProvider,
)
from services.task_queue import ReviewTaskLease, ReviewTaskQueue, TaskQueueError
from services.github import GitHubApiClient
from services.github_auth import GitHubAppSettings, GitHubAppTokenProvider
from services.github_context import GitHubReviewContextLoader, ReviewContextLoader
from services.github_rules import GitHubRepositoryRuleLoader, RepositoryRuleLoader
from services.review_planning import ReviewPlanner
from services.model_review import (
    ModelReviewer,
    combine_model_review_results,
    plan_model_review_batches,
)


LOGGER = logging.getLogger("openreviewer.worker")


def _positive_float(value: str, name: str) -> float:
    """解析一个必须大于零的浮点配置值。

    Worker 的轮询间隔和租约时长都来自环境变量；集中校验可以把空值、非数字
    或零值在进程启动时暴露，而不是运行到队列逻辑后才出现难以定位的行为。

    参数：
        value: 待解析的环境变量文本。
        name: 配置项名称，只用于生成可定位的错误信息。

    返回：
        大于零的浮点秒数；保留小数以支持短轮询或测试中的精细时间间隔。

    异常：
        ValueError: 文本不是合法数字，或解析结果小于等于零。
    """
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    worker_id: str
    poll_interval: timedelta
    lease_duration: timedelta
    ci_poll_interval: timedelta = timedelta(seconds=30)
    ci_wait_timeout: timedelta = timedelta(hours=1)
    github_context_lease_duration: timedelta = timedelta(minutes=10)
    model_review_lease_duration: timedelta = timedelta(minutes=10)

    def __post_init__(self) -> None:
        if not self.worker_id or len(self.worker_id) > 200:
            raise ValueError("worker ID must contain 1 to 200 characters")
        if self.poll_interval.total_seconds() <= 0:
            raise ValueError("worker poll interval must be positive")
        if self.lease_duration <= self.poll_interval * 2:
            raise ValueError("worker lease duration must exceed twice the poll interval")
        if self.ci_poll_interval.total_seconds() <= 0:
            raise ValueError("CI poll interval must be positive")
        if self.ci_wait_timeout <= self.ci_poll_interval:
            raise ValueError("CI wait timeout must exceed the poll interval")
        if self.github_context_lease_duration <= self.lease_duration:
            raise ValueError("GitHub context lease must exceed the normal lease")
        if self.model_review_lease_duration <= self.lease_duration:
            raise ValueError("model review lease must exceed the normal lease")

    @classmethod
    def from_environment(cls) -> "WorkerSettings":
        """读取并校验 Worker ID、轮询间隔和租约时长。

        租约必须长于两倍轮询间隔，给 Worker 留出至少一次恢复/续租机会；如果
        配置不满足这个关系，启动直接失败，避免任务频繁误判为过期。

        返回：
            包含稳定 Worker ID、轮询间隔和租约时长的不可变配置对象。

        异常：
            ValueError: Worker ID 为空/超过 200 字符，数值配置不是正数，或租约
            不大于两倍轮询间隔。

        配置来源：
            ``OPENREVIEWER_WORKER_ID`` 未设置时使用“主机名:进程号”作为临时 ID；
            Compose 会显式设置固定 ID，以便数据库中的心跳在容器重启后继续更新
            同一行。轮询默认 2 秒，租约默认 30 秒。
        """
        worker_id = os.environ.get(
            "OPENREVIEWER_WORKER_ID",
            f"{socket.gethostname()}:{os.getpid()}",
        ).strip()
        if not worker_id or len(worker_id) > 200:
            raise ValueError("OPENREVIEWER_WORKER_ID must contain 1 to 200 characters")
        poll_seconds = _positive_float(
            os.environ.get("OPENREVIEWER_WORKER_POLL_SECONDS", "2"),
            "OPENREVIEWER_WORKER_POLL_SECONDS",
        )
        lease_seconds = _positive_float(
            os.environ.get("OPENREVIEWER_WORKER_LEASE_SECONDS", "30"),
            "OPENREVIEWER_WORKER_LEASE_SECONDS",
        )
        if lease_seconds <= poll_seconds * 2:
            raise ValueError("worker lease duration must exceed twice the poll interval")
        ci_poll_seconds = _positive_float(
            os.environ.get("OPENREVIEWER_CI_POLL_SECONDS", "30"),
            "OPENREVIEWER_CI_POLL_SECONDS",
        )
        ci_wait_seconds = _positive_float(
            os.environ.get("OPENREVIEWER_CI_WAIT_TIMEOUT_SECONDS", "3600"),
            "OPENREVIEWER_CI_WAIT_TIMEOUT_SECONDS",
        )
        if ci_wait_seconds <= ci_poll_seconds:
            raise ValueError("CI wait timeout must exceed the poll interval")
        context_lease_seconds = _positive_float(
            os.environ.get("OPENREVIEWER_GITHUB_CONTEXT_LEASE_SECONDS", "600"),
            "OPENREVIEWER_GITHUB_CONTEXT_LEASE_SECONDS",
        )
        if context_lease_seconds <= lease_seconds:
            raise ValueError("GitHub context lease must exceed the normal lease")
        model_lease_seconds = _positive_float(
            os.environ.get("OPENREVIEWER_MODEL_REVIEW_LEASE_SECONDS", "600"),
            "OPENREVIEWER_MODEL_REVIEW_LEASE_SECONDS",
        )
        if model_lease_seconds <= lease_seconds:
            raise ValueError("model review lease must exceed the normal lease")
        return cls(
            worker_id=worker_id,
            poll_interval=timedelta(seconds=poll_seconds),
            lease_duration=timedelta(seconds=lease_seconds),
            ci_poll_interval=timedelta(seconds=ci_poll_seconds),
            ci_wait_timeout=timedelta(seconds=ci_wait_seconds),
            github_context_lease_duration=timedelta(
                seconds=context_lease_seconds
            ),
            model_review_lease_duration=timedelta(seconds=model_lease_seconds),
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
        self._duration = duration
        self.lease = lease

    def renew(self, duration: timedelta | None = None) -> None:
        self.lease = self._queue.renew_lease(
            self.lease,
            duration or self._duration,
        )


class _BusyHeartbeat:
    """在 GitHub 外部读取期间定时刷新 Worker 忙碌心跳。"""

    def __init__(
        self,
        queue: ReviewTaskQueue,
        worker_id: str,
        task_id: str,
        interval: timedelta,
    ) -> None:
        self._queue = queue
        self._worker_id = worker_id
        self._task_id = task_id
        # 健康检查默认允许 15 秒，最长 5 秒一次可以覆盖慢速外部请求。
        self._interval_seconds = min(
            5.0,
            max(0.5, interval.total_seconds()),
        )
        self._stop_event = Event()
        self._thread = Thread(
            target=self._run,
            name=f"openreviewer-heartbeat-{worker_id}",
            daemon=True,
        )

    def start(self) -> None:
        """启动独立心跳线程；线程只使用队列公开的短事务接口。"""

        self._thread.start()

    def stop(self) -> None:
        """停止并等待心跳线程退出，避免恢复 idle 时发生写入竞态。"""

        self._stop_event.set()
        self._thread.join(timeout=self._interval_seconds + 1.0)

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                self._queue.record_heartbeat(
                    self._worker_id,
                    WorkerStatus.BUSY,
                    self._task_id,
                )
            except TaskQueueError:
                LOGGER.exception("Worker 忙碌心跳刷新失败")


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
        stop_event: Event | None = None,
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
        self._stop_event = stop_event or Event()

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
        self._queue.record_heartbeat(worker_id, WorkerStatus.STARTING)
        LOGGER.info("Worker 已启动，等待数据库任务")
        try:
            while not self._stop_event.is_set():
                try:
                    self.run_once()
                except TaskQueueError:
                    # 单次队列事务失败不应终止常驻进程；等待下一轮后重试，
                    # 同时保留完整堆栈供详情页之外的运维日志定位。
                    LOGGER.exception("Worker 本轮队列处理失败，将继续轮询")
                self._stop_event.wait(self._settings.poll_interval.total_seconds())
        finally:
            try:
                self._queue.record_heartbeat(worker_id, WorkerStatus.STOPPING)
            except TaskQueueError:
                LOGGER.exception("Worker 停止状态写入失败")
            LOGGER.info("Worker 已停止")

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
        recovered = self._queue.recover_expired_leases()
        if recovered:
            LOGGER.warning("已恢复 %s 个租约超时任务", recovered)

        self._queue.record_heartbeat(worker_id, WorkerStatus.IDLE)
        ai_runtime = (
            self._ai_runtime_provider.current()
            if self._ai_runtime_provider is not None
            else None
        )
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
            return False

        self._queue.record_heartbeat(worker_id, WorkerStatus.BUSY, lease.task_id)
        cursor = _LeaseCursor(self._queue, lease, self._settings.lease_duration)
        busy_heartbeat = (
            _BusyHeartbeat(
                self._queue,
                worker_id,
                lease.task_id,
                self._settings.poll_interval,
            )
            if (
                self._context_loader is not None
                or self._rule_loader is not None
                or self._model_reviewer is not None
                or self._ai_runtime_provider is not None
            )
            else None
        )
        if busy_heartbeat is not None:
            busy_heartbeat.start()
        try:
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
                self._queue.retry_or_fail(cursor.lease, safe_error)
            except TaskQueueError as persistence_error:
                persisted_error = SafeError.from_exception(persistence_error)
                LOGGER.error(
                    "任务 %s 的失败状态无法持久化，错误码=%s",
                    lease.task_id,
                    persisted_error.code.value,
                )
        finally:
            if busy_heartbeat is not None:
                busy_heartbeat.stop()
            self._queue.record_heartbeat(worker_id, WorkerStatus.IDLE)
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
            尚未接入模型，因此不会写成 ``completed``。

        异常：
            TaskLeaseLostError/TaskQueueError: 租约失效或数据库状态转换失败，
            由 ``run_once`` 交给重试/失败处理。
        """

        if cursor.lease.claimed_from_status is ExecutionStatus.READY_FOR_REVIEW:
            if cursor.lease.review_plan_id is not None:
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
                                model_settings.input_budget_tokens
                            ),
                            "provider": model_settings.provider.value,
                            "api_protocol": (
                                model_settings.resolved_api_protocol.value
                            ),
                            "model": model_settings.model,
                        },
                    )
                    batch_results = []
                    for batch in batches:
                        cursor.renew(self._settings.model_review_lease_duration)
                        files = batch.files
                        self._queue.record_model_progress(
                            cursor.lease,
                            "batch_started",
                            {
                                "batch_number": batch.number,
                                "batch_count": batch.total,
                                "file_count": len(files),
                                "first_file": files[0],
                                "last_file": files[-1],
                                "estimated_input_tokens": (
                                    batch.estimated_input_tokens
                                ),
                                "fragmented": batch.fragmented,
                            },
                        )
                        batch_result = model_reviewer.review(batch.review_input)
                        batch_results.append(batch_result)
                        self._queue.record_model_progress(
                            cursor.lease,
                            "batch_completed",
                            {
                                "batch_number": batch.number,
                                "batch_count": batch.total,
                                "file_count": len(files),
                                "input_tokens": (
                                    batch_result.usage.total_input_tokens
                                ),
                                "output_tokens": batch_result.usage.output_tokens,
                                "reasoning_tokens": (
                                    batch_result.usage.reasoning_output_tokens
                                ),
                                "duration_ms": batch_result.duration_ms,
                                "finding_count": len(
                                    batch_result.output.findings
                                ),
                            },
                        )
                    model_result = combine_model_review_results(
                        model_input,
                        tuple(batch_results),
                    )
                else:
                    model_result = model_reviewer.review(model_input)
                findings = materialize_findings(model_input, model_result.output)
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
        )
        database = Database.from_environment()
        ai_runtime_provider = SqlAlchemyAiRuntimeProvider(
            AiSettingsService(
                database.sessions,
                AiSecretCipher.from_environment(),
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

    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)
    try:
        runtime.run()
    finally:
        ai_runtime_provider.close()
        github_api.close()
        database.dispose()


if __name__ == "__main__":
    main()
