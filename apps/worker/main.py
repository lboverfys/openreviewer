"""单并发数据库 Worker 的进程入口。"""

from dataclasses import dataclass
from datetime import timedelta
import logging
import os
import signal
import socket
from threading import Event

from domain.enums import WorkerStatus
from domain.security import SafeError, install_redacting_log_filters
from persistence.database import Database
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from services.task_queue import ReviewTaskLease, ReviewTaskQueue, TaskQueueError


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
        return cls(
            worker_id=worker_id,
            poll_interval=timedelta(seconds=poll_seconds),
            lease_duration=timedelta(seconds=lease_seconds),
        )


class WorkerRuntime:
    def __init__(
        self,
        queue: ReviewTaskQueue,
        settings: WorkerSettings,
        *,
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
                self.run_once()
                self._stop_event.wait(self._settings.poll_interval.total_seconds())
        finally:
            try:
                self._queue.record_heartbeat(worker_id, WorkerStatus.STOPPING)
            except TaskQueueError:
                LOGGER.exception("Worker 停止状态写入失败")
            LOGGER.info("Worker 已停止")

    def run_once(self) -> bool:
        """执行一轮“恢复、心跳、领取、处理”的队列流程。

        返回值表示本轮是否成功领取了任务。当前 M2 处理步骤只会把任务推进到
        ``waiting_for_ci``；处理异常则交给队列按租约规则重试或标记失败。状态
        写入失败会记录日志，但不会把失去租约的任务强行改写成成功。

        返回：
            成功领取任务并完成本轮处理时返回 ``True``；队列为空返回 ``False``。
            “返回 True”只表示领取过任务，不表示审查已经完成。

        状态顺序：
            先恢复过期租约，再把 Worker 记为空闲并领取一条任务；领取后记为
            ``busy``，当前 M2 只调用 ``mark_waiting_for_ci``，最后无论成功失败都
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
        lease = self._queue.claim_next(worker_id, self._settings.lease_duration)
        if lease is None:
            return False

        self._queue.record_heartbeat(worker_id, WorkerStatus.BUSY, lease.task_id)
        try:
            self._advance_to_supported_boundary(lease)
            LOGGER.info("任务 %s 已进入 waiting_for_ci", lease.task_id)
        except Exception as exc:
            safe_error = SafeError.from_exception(exc)
            LOGGER.error(
                "任务 %s 处理失败，错误码=%s，说明=%s",
                lease.task_id,
                safe_error.code.value,
                safe_error.safe_message,
            )
            try:
                self._queue.retry_or_fail(lease, safe_error)
            except TaskQueueError as persistence_error:
                persisted_error = SafeError.from_exception(persistence_error)
                LOGGER.error(
                    "任务 %s 的失败状态无法持久化，错误码=%s",
                    lease.task_id,
                    persisted_error.code.value,
                )
        finally:
            self._queue.record_heartbeat(worker_id, WorkerStatus.IDLE)
        return True

    def _advance_to_supported_boundary(self, lease: ReviewTaskLease) -> None:
        """把任务推进到当前版本真正支持的边界。

        GitHub、CI 和模型调用尚未接入，因此这里仅调用队列的状态转换方法。这个
        看似简单的封装保留了未来插入 PR 上下文准备和审查工作流的扩展点，同时
        明确禁止把未执行的工作标记为 ``completed``。

        参数：
            lease: 当前 Worker 领取的有效租约。

        副作用：
            调用队列把任务和运行原子推进到 ``waiting_for_ci``，清除租约并写入
            状态事件。当前版本不会调用 GitHub、CI、模型或外部通知系统。

        异常：
            TaskLeaseLostError/TaskQueueError: 租约失效或数据库状态转换失败，
            由 ``run_once`` 交给重试/失败处理。
        """

        self._queue.mark_waiting_for_ci(lease)


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
    database = Database.from_environment()
    runtime = WorkerRuntime(
        SqlAlchemyReviewTaskQueue(database.sessions),
        settings,
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
        database.dispose()


if __name__ == "__main__":
    main()
