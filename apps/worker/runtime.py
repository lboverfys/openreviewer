"""Worker runtime 职责模块。"""

import os
from collections.abc import Callable
from threading import Event, Thread
from uuid import uuid4

from apps.worker.agent_pipeline import _run_fixed_agent_workflow
from apps.worker.heartbeat import (
    _BusyHeartbeat,
    _LeaseCursor,
    _propagate_task_lease_loss,
    _record_worker_heartbeat,
    _start_worker_heartbeat,
)
from apps.worker.logging_context import LOGGER
from apps.worker.review_pipeline import _advance_to_supported_boundary
from apps.worker.settings import WorkerSettings
from domain.enums import ExecutionStatus, WorkerStatus
from domain.model_review import (
    MaterializedFinding,
    ModelReviewInput,
    ModelReviewResult,
)
from domain.repository_policy import RepositoryRequestLimitError
from domain.security import ErrorCode, SafeError
from services.ai_settings import ActiveAiRuntime, AiRuntimeProvider
from services.evidence_verification import EvidenceVerifier, apply_evidence_verification
from services.github_context import ReviewContextLoader
from services.github_rules import RepositoryRuleLoader
from services.model_budget import model_budget_scope, model_request_scope
from services.model_review import (
    ModelReviewer,
)
from services.operations import OperationsError, WorkerMaintenance
from services.rag import MarkdownKnowledgeBase
from services.retrieval import HybridRetrievalService
from services.review_planning import ReviewPlanner
from services.task_queue import (
    ReviewTaskQueue,
    TaskLeaseLostError,
    TaskQueueError,
)


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
        profile_loader: Callable[[str], ActiveAiRuntime] | None = None,
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
        self._profile_loader = profile_loader
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
                    LOGGER.exception("Worker 上一轮忙碌心跳退出后的 IDLE 写入失败")
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
            location = (
                f"{os.path.basename(frame.tb_frame.f_code.co_filename)}:{frame.tb_lineno}"
                if frame is not None
                else "unknown"
            )
            LOGGER.error(
                "代码索引处理失败，类型=%s，位置=%s，错误码=%s，说明=%s",
                type(exc).__name__,
                location,
                safe_error.code.value,
                safe_error.safe_message,
            )
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
            profile_getter = getattr(self._queue, "task_profile_id", None)
            profile_id = profile_getter(cursor.lease) if profile_getter else None
            if profile_id:
                if self._profile_loader is None:
                    raise TaskQueueError("Worker 尚未配置审查方案加载器")
                ai_runtime = self._profile_loader(profile_id)
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
                elif (
                    safe_error.details.get("budget_reason")
                    == "repository_monthly_budget"
                ):
                    pause_budget = getattr(
                        self._queue, "pause_for_monthly_budget", None
                    )
                    if pause_budget is not None:
                        pause_budget(cursor.lease, safe_error)
                    else:
                        self._queue.retry_or_fail(cursor.lease, safe_error)
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
            if (
                ai_runtime is not None
                and ai_runtime.profile_id is not None
                and ai_runtime.agent_workflow is not None
            ):
                ai_runtime.agent_workflow.close()
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
        self, cursor: _LeaseCursor, ai_runtime: ActiveAiRuntime | None = None
    ) -> ExecutionStatus:
        return _advance_to_supported_boundary(self, cursor, ai_runtime)

    def _repository_request_guard(
        self,
        cursor: _LeaseCursor,
        model_input: ModelReviewInput,
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
        self,
        cursor: _LeaseCursor,
        reviewer: ModelReviewer,
        model_input: ModelReviewInput,
    ) -> ModelReviewResult:
        accountant_factory = getattr(self._queue, "monthly_accountant", None)
        accountant = (
            accountant_factory(lambda: cursor.lease, "default")
            if accountant_factory
            else None
        )
        with (
            model_request_scope(self._repository_request_guard(cursor, model_input)),
            model_budget_scope(accountant),
        ):
            return reviewer.review(model_input)

    def _run_fixed_agent_workflow(
        self, cursor: _LeaseCursor, ai_runtime: ActiveAiRuntime
    ) -> ExecutionStatus:
        return _run_fixed_agent_workflow(self, cursor, ai_runtime)
