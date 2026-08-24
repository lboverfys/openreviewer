"""认证运维 Dashboard 使用的只读模型。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from domain.enums import ExecutionStatus, WorkerStatus


class DashboardPersistenceError(RuntimeError):
    """无法从持久化存储读取 Dashboard 数据。"""


def as_utc(value: datetime) -> datetime:
    """把数据库返回的时间统一转换为带 UTC 时区的时间。

    某些数据库驱动可能返回没有时区信息的 ``datetime``；项目内部统一使用
    UTC，避免 Dashboard 在比较心跳时间时混用 naive 和 aware 时间对象。

    参数：
        value: 数据库或注入时钟产生的时间。没有 ``tzinfo`` 时，项目约定其
            已经代表 UTC，而不是服务器本地时间。

    返回：
        带 UTC 时区信息的 ``datetime``。已有时区的值会换算到同一绝对时刻。

    该函数不读取系统时区，也不修改传入对象。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ReviewListItem:
    review_run_id: str
    review_task_id: str
    repository: str
    pull_request_number: int
    head_sha: str
    execution_status: ExecutionStatus
    attempt_count: int
    max_attempts: int
    last_error: str | None
    last_error_code: str | None
    last_error_retryable: bool | None
    last_error_details: Mapping[str, object] | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredWorkerHeartbeat:
    worker_id: str
    status: WorkerStatus
    current_task_id: str | None
    started_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class DashboardData:
    total_reviews: int
    status_counts: Mapping[ExecutionStatus, int]
    recent_reviews: tuple[ReviewListItem, ...]
    latest_worker: StoredWorkerHeartbeat | None


class DashboardRepository(Protocol):
    def load(self, limit: int) -> DashboardData:
        """从持久化层一次性读取 Dashboard 所需的原始数据。

        参数：
            limit: 最多返回多少条最近审查记录；服务层保证范围为 1 到 100。

        返回：
            包含总数、按状态计数、最近任务和最新 Worker 心跳的 ``DashboardData``。

        异常：
            DashboardPersistenceError: 数据库查询或记录转换失败。

        协议只约定数据形状，不负责判断 Worker 是否在线；在线状态需要结合服务层
        的当前时钟和窗口计算。
        """
        ...


@dataclass(frozen=True, slots=True)
class WorkerSnapshot:
    configured: bool
    online: bool
    worker_id: str | None
    status: WorkerStatus | None
    current_task_id: str | None
    started_at: datetime | None
    last_seen_at: datetime | None


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    generated_at: datetime
    total_reviews: int
    status_counts: Mapping[ExecutionStatus, int]
    worker: WorkerSnapshot
    recent_reviews: tuple[ReviewListItem, ...]


class DashboardService:
    def __init__(
        self,
        repository: DashboardRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        worker_online_window: timedelta = timedelta(seconds=15),
    ) -> None:
        """初始化 Dashboard 用例及 Worker 在线判定窗口。

        ``clock`` 可以注入测试时钟；生产环境默认使用当前 UTC 时间。Worker
        最近一次心跳超过 ``worker_online_window`` 后会显示为离线。

        参数：
            repository: 实现 ``DashboardRepository`` 的只读持久化适配器。
            clock: 可选当前时间函数；不传时使用系统 UTC 时间。
            worker_online_window: 心跳仍被视为在线的最大年龄，默认 15 秒。

        异常：
            ValueError: 在线窗口不大于零。

        构造函数只保存依赖，不会立即查询数据库。
        """
        if worker_online_window.total_seconds() <= 0:
            raise ValueError("worker_online_window must be positive")
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))
        self._worker_online_window = worker_online_window

    def snapshot(self, limit: int = 50) -> DashboardSnapshot:
        """组装一个可直接返回给 API 和 SSE 的完整 Dashboard 快照。

        方法会限制查询数量、补齐所有执行状态的计数，并把数据库中的心跳转换
        成“已配置/在线/离线”的展示模型。底层读取失败会由仓储层转换成
        ``DashboardPersistenceError``，不会伪造空数据掩盖故障。

        参数：
            limit: 最近任务最大条数，必须在 1 到 100 之间。

        返回：
            带生成时间、完整状态计数、Worker 展示状态和最近任务的不可变快照。
            即使某种执行状态在数据库中没有记录，返回映射里也会包含值为 0 的键。

        异常：
            ValueError: ``limit`` 越界。
            DashboardPersistenceError: 仓储读取失败或数据库值无法转换为领域枚举。

        没有任何心跳记录时，``configured`` 和 ``online`` 都为 ``False``；有记录但
        最后心跳超出窗口时，仅 ``online`` 为 ``False``。
        """
        if not 1 <= limit <= 100:
            raise ValueError("dashboard limit must be between 1 and 100")
        now = self._clock().astimezone(UTC)
        data = self._repository.load(limit)
        heartbeat = data.latest_worker
        if heartbeat is None:
            worker = WorkerSnapshot(
                configured=False,
                online=False,
                worker_id=None,
                status=None,
                current_task_id=None,
                started_at=None,
                last_seen_at=None,
            )
        else:
            worker = WorkerSnapshot(
                configured=True,
                online=now - as_utc(heartbeat.last_seen_at)
                <= self._worker_online_window,
                worker_id=heartbeat.worker_id,
                status=heartbeat.status,
                current_task_id=heartbeat.current_task_id,
                started_at=as_utc(heartbeat.started_at),
                last_seen_at=as_utc(heartbeat.last_seen_at),
            )
        complete_counts = {
            status: int(data.status_counts.get(status, 0))
            for status in ExecutionStatus
        }
        return DashboardSnapshot(
            generated_at=now,
            total_reviews=data.total_reviews,
            status_counts=complete_counts,
            worker=worker,
            recent_reviews=data.recent_reviews,
        )
