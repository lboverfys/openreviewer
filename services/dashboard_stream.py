"""跨 SSE 连接复用 Dashboard 变化检测和快照加载。"""

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import TypeVar

SnapshotT = TypeVar("SnapshotT")


class DashboardStreamUnavailable(RuntimeError):
    """当前共享轮询周期无法读取 Dashboard。"""


@dataclass(frozen=True, slots=True)
class DashboardStreamUpdate[SnapshotT]:
    change_token: str
    snapshot: SnapshotT


class DashboardStreamCoordinator[SnapshotT]:
    """把同一进程内多个 SSE 客户端合并为一次有界数据库轮询。"""

    def __init__(
        self,
        change_token_loader: Callable[[], str],
        snapshot_loader: Callable[[], SnapshotT],
        *,
        poll_interval_seconds: float = 2.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("dashboard stream poll interval must be positive")
        self._change_token_loader = change_token_loader
        self._snapshot_loader = snapshot_loader
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock or time.monotonic
        self._lock = Lock()
        self._next_poll_at = 0.0
        self._change_token: str | None = None
        self._snapshot: SnapshotT | None = None
        self._unavailable = False

    def poll(self) -> DashboardStreamUpdate[SnapshotT]:
        """返回共享状态；一个轮询周期内最多执行一次变化查询。"""

        with self._lock:
            now = self._clock()
            if now >= self._next_poll_at:
                self._next_poll_at = now + self._poll_interval_seconds
                try:
                    change_token = self._change_token_loader()
                    snapshot = self._snapshot
                    if snapshot is None or change_token != self._change_token:
                        snapshot = self._snapshot_loader()
                    self._change_token = change_token
                    self._snapshot = snapshot
                    self._unavailable = False
                except Exception as exc:
                    self._unavailable = True
                    raise DashboardStreamUnavailable(
                        "dashboard stream data is temporarily unavailable"
                    ) from exc

            if self._unavailable or self._change_token is None or self._snapshot is None:
                raise DashboardStreamUnavailable(
                    "dashboard stream data is temporarily unavailable"
                )
            return DashboardStreamUpdate(
                change_token=self._change_token,
                snapshot=self._snapshot,
            )


class DashboardStreamRegistry[SnapshotT]:
    """以有界 LRU 方式复用不同资源范围的 Dashboard 协调器。

    资源范围来自登录配置，理论上可以持续增加；如果只使用普通字典，API
    进程会永久保留每个范围及其最近快照。注册表只负责后续复用，已经被 SSE
    连接持有的协调器即使被淘汰也仍然有效，淘汰只影响下一次访问。
    """

    def __init__(self, *, capacity: int = 64) -> None:
        if isinstance(capacity, bool) or capacity < 1:
            raise ValueError("dashboard stream registry capacity must be positive")
        self._capacity = capacity
        self._streams: OrderedDict[
            str, DashboardStreamCoordinator[SnapshotT]
        ] = OrderedDict()
        self._lock = Lock()

    @property
    def capacity(self) -> int:
        """返回注册表的最大条目数。"""

        return self._capacity

    def get_or_create(
        self,
        key: str,
        factory: Callable[[], DashboardStreamCoordinator[SnapshotT]],
    ) -> DashboardStreamCoordinator[SnapshotT]:
        """读取并触碰条目，不存在时原子创建并淘汰最久未使用项。"""

        if not key:
            raise ValueError("dashboard stream registry key must not be empty")
        with self._lock:
            stream = self._streams.get(key)
            if stream is not None:
                self._streams.move_to_end(key)
                return stream
            stream = factory()
            self._streams[key] = stream
            self._streams.move_to_end(key)
            if len(self._streams) > self._capacity:
                self._streams.popitem(last=False)
            return stream

    def __len__(self) -> int:
        """返回当前缓存条目数，主要用于观测和测试。"""

        with self._lock:
            return len(self._streams)

    def keys(self) -> tuple[str, ...]:
        """返回从最旧到最新的缓存键快照。"""

        with self._lock:
            return tuple(self._streams)
