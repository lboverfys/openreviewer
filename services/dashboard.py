"""认证运维 Dashboard 使用的只读模型。"""

import base64
import binascii
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol, overload

from domain.enums import ExecutionStatus, WorkerStatus
from domain.workers import DASHBOARD_WORKER_PREVIEW_LIMIT, WORKER_ONLINE_WINDOW
from services.rbac import ResourceScope


class DashboardPersistenceError(RuntimeError):
    """无法从持久化存储读取 Dashboard 数据。"""


def _effective_scope(scope: ResourceScope | None) -> ResourceScope | None:
    """把管理员的显式全量范围归一为旧仓储协议使用的 ``None``。"""

    return None if scope is None or scope.unrestricted else scope


@dataclass(frozen=True, slots=True)
class ReviewCursor:
    created_at: datetime
    review_run_id: str


def encode_review_cursor(created_at: datetime, review_run_id: str) -> str:
    """把稳定排序键编码为不透明、URL 安全的下一页游标。"""

    payload = json.dumps(
        {
            "v": 1,
            "created_at": as_utc(created_at).isoformat(timespec="microseconds"),
            "review_run_id": review_run_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_review_cursor(value: str) -> ReviewCursor:
    """严格解析游标；畸形、过长或未知版本均拒绝。"""

    if not value or len(value) > 512:
        raise ValueError("review cursor is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        decoded = json.loads(payload.decode("utf-8"))
        if not isinstance(decoded, dict) or set(decoded) != {
            "v",
            "created_at",
            "review_run_id",
        }:
            raise ValueError
        if decoded["v"] != 1 or not isinstance(decoded["review_run_id"], str):
            raise ValueError
        review_run_id = decoded["review_run_id"]
        if not 1 <= len(review_run_id) <= 36:
            raise ValueError
        created_at = datetime.fromisoformat(decoded["created_at"])
        if created_at.tzinfo is None:
            raise ValueError
    except (
        UnicodeError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise ValueError("review cursor is invalid") from exc
    return ReviewCursor(
        created_at=created_at.astimezone(UTC),
        review_run_id=review_run_id,
    )


@overload
def as_utc(value: datetime) -> datetime: ...


@overload
def as_utc(value: None) -> None: ...


def as_utc(value: datetime | None) -> datetime | None:
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
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def required_utc(value: datetime | None, field_name: str) -> datetime:
    """规范化必填数据库时间；缺失时拒绝返回不完整读模型。"""

    normalized = as_utc(value)
    if normalized is None:
        raise ValueError(f"{field_name} must not be null")
    return normalized


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
    review_conclusion: str | None
    coverage_status: str
    model_review_completed_at: datetime | None
    finding_count: int
    unreviewed_finding_count: int
    model_attempt_count: int
    created_at: datetime
    updated_at: datetime
    workflow_status: ExecutionStatus = ExecutionStatus.QUEUED
    snapshot_review: bool = False
    pr_title: str | None = None
    pr_author_login: str | None = None
    pr_html_url: str | None = None
    head_repository: str | None = None
    head_ref: str | None = None
    base_repository: str | None = None
    base_ref: str | None = None


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
    workers: tuple[StoredWorkerHeartbeat, ...]
    has_more: bool = False
    worker_online_count: int | None = None
    worker_busy_count: int | None = None


@dataclass(frozen=True, slots=True)
class DashboardChangeState:
    latest_event_id: str | None
    latest_event_at: datetime | None
    workers: tuple[StoredWorkerHeartbeat, ...]
    worker_online_count: int | None = None
    worker_busy_count: int | None = None


class DashboardRepository(Protocol):
    def load(
        self,
        limit: int,
        cursor: ReviewCursor | None = None,
        scope: ResourceScope | None = None,
        *,
        execution_status: ExecutionStatus | None = None,
        query: str = "",
        include_overview: bool = True,
        worker_cutoff: datetime | None = None,
    ) -> DashboardData:
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

    def change_state(
        self,
        *,
        scope: ResourceScope | None = None,
        worker_cutoff: datetime | None = None,
    ) -> DashboardChangeState:
        """读取可表示 Dashboard 可见变更的轻量索引状态。"""

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
    workers: tuple[WorkerSnapshot, ...]
    recent_reviews: tuple[ReviewListItem, ...]
    next_cursor: str | None
    worker_online_count: int | None = None
    worker_busy_count: int | None = None


class DashboardService:
    def __init__(
        self,
        repository: DashboardRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        worker_online_window: timedelta = WORKER_ONLINE_WINDOW,
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

    def snapshot(
        self,
        limit: int = 50,
        cursor: str | None = None,
        scope: ResourceScope | None = None,
        *,
        execution_status: ExecutionStatus | None = None,
        query: str = "",
        include_overview: bool = True,
    ) -> DashboardSnapshot:
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
        decoded_cursor = decode_review_cursor(cursor) if cursor is not None else None
        effective_scope = _effective_scope(scope)
        data = self._repository.load(
            limit, decoded_cursor, effective_scope,
            execution_status=execution_status, query=query,
            include_overview=include_overview,
            worker_cutoff=now - self._worker_online_window,
        )
        workers = tuple(
            WorkerSnapshot(
                configured=True,
                online=(heartbeat.status is not WorkerStatus.STOPPING and
                        now - as_utc(heartbeat.last_seen_at) <= self._worker_online_window),
                worker_id=heartbeat.worker_id,
                status=heartbeat.status,
                current_task_id=heartbeat.current_task_id,
                started_at=as_utc(heartbeat.started_at),
                last_seen_at=as_utc(heartbeat.last_seen_at),
            )
            for heartbeat in data.workers
        )
        worker = (
            next((item for item in workers if item.online), workers[0])
            if workers
            else WorkerSnapshot(
                configured=False,
                online=False,
                worker_id=None,
                status=None,
                current_task_id=None,
                started_at=None,
                last_seen_at=None,
            )
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
            workers=tuple(item for item in workers if item.online)[:DASHBOARD_WORKER_PREVIEW_LIMIT],
            worker_online_count=(data.worker_online_count if data.worker_online_count is not None
                                 else sum(item.online for item in workers)),
            worker_busy_count=(data.worker_busy_count if data.worker_busy_count is not None
                               else sum(item.online and item.status is WorkerStatus.BUSY for item in workers)),
            recent_reviews=data.recent_reviews,
            next_cursor=(
                encode_review_cursor(
                    data.recent_reviews[-1].created_at,
                    data.recent_reviews[-1].review_run_id,
                )
                if data.has_more and data.recent_reviews
                else None
            ),
        )

    def change_token(self, scope: ResourceScope | None = None) -> str:
        """返回轻量变化令牌，并按在线窗口刷新 Worker 离线判定。"""

        effective_scope = _effective_scope(scope)
        now = self._clock().astimezone(UTC)
        state = self._repository.change_state(scope=effective_scope, worker_cutoff=now - self._worker_online_window)
        worker_state = [
            {
                "worker_id": worker.worker_id,
                "status": worker.status.value,
                "current_task_id": worker.current_task_id,
                "started_at": as_utc(worker.started_at).isoformat(),
                "online": (worker.status is not WorkerStatus.STOPPING and
                           now - as_utc(worker.last_seen_at) <= self._worker_online_window),
            }
            for worker in sorted(state.workers, key=lambda item: item.worker_id)
        ]
        raw = json.dumps(
            {
                "latest_event_id": state.latest_event_id,
                "latest_event_at": (
                    as_utc(state.latest_event_at).isoformat()
                    if state.latest_event_at
                    else None
                ),
                "workers": worker_state,
                "worker_online_count": state.worker_online_count,
                "worker_busy_count": state.worker_busy_count,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(raw.encode("utf-8")).hexdigest()[:24]
