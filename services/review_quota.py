"""审查任务创建配额的策略与安全错误。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class ReviewQuotaExceededError(RuntimeError):
    """账号、仓库或全局任务创建配额已耗尽。"""

    def __init__(self, scope: str, limit: int, retry_after_seconds: int) -> None:
        super().__init__(f"review quota exceeded for {scope}")
        self.scope = scope
        self.limit = limit
        self.retry_after_seconds = max(1, retry_after_seconds)


@dataclass(frozen=True, slots=True)
class ReviewQuotaPolicy:
    """有界的小时/日任务创建额度。"""

    user_hourly: int = 20
    repository_hourly: int = 50
    global_hourly: int = 200
    user_daily: int = 100
    repository_daily: int = 500
    global_daily: int = 2_000

    def __post_init__(self) -> None:
        values = (
            self.user_hourly,
            self.repository_hourly,
            self.global_hourly,
            self.user_daily,
            self.repository_daily,
            self.global_daily,
        )
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in values):
            raise ValueError("review quota limits must be positive integers")
        if self.user_hourly > self.user_daily:
            raise ValueError("daily user quota must cover hourly user quota")
        if self.repository_hourly > self.repository_daily:
            raise ValueError("daily repository quota must cover hourly repository quota")
        if self.global_hourly > self.global_daily:
            raise ValueError("daily global quota must cover hourly global quota")

    @classmethod
    def from_environment(
        cls,
        values: Mapping[str, str] | None = None,
    ) -> ReviewQuotaPolicy:
        source = values if values is not None else os.environ

        def read(name: str, default: int, maximum: int) -> int:
            raw = source.get(name, str(default)).strip()
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be a positive integer") from exc
            if not 1 <= value <= maximum:
                raise ValueError(f"{name} is outside the allowed range")
            return value

        return cls(
            user_hourly=read("OPENREVIEWER_QUOTA_USER_HOURLY", 20, 100_000),
            repository_hourly=read(
                "OPENREVIEWER_QUOTA_REPOSITORY_HOURLY", 50, 100_000
            ),
            global_hourly=read("OPENREVIEWER_QUOTA_GLOBAL_HOURLY", 200, 1_000_000),
            user_daily=read("OPENREVIEWER_QUOTA_USER_DAILY", 100, 1_000_000),
            repository_daily=read(
                "OPENREVIEWER_QUOTA_REPOSITORY_DAILY", 500, 1_000_000
            ),
            global_daily=read("OPENREVIEWER_QUOTA_GLOBAL_DAILY", 2_000, 5_000_000),
        )


def quota_windows(now: datetime) -> tuple[tuple[str, datetime], ...]:
    """返回当前 UTC 小时和自然日窗口的稳定起点。"""

    current = now.astimezone(UTC)
    hour = current.replace(minute=0, second=0, microsecond=0)
    day = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return (("hour", hour), ("day", day))


def retry_after_for(window: str, window_start: datetime, now: datetime) -> int:
    duration = timedelta(hours=1) if window == "hour" else timedelta(days=1)
    normalized_start = (
        window_start.replace(tzinfo=UTC)
        if window_start.tzinfo is None
        else window_start.astimezone(UTC)
    )
    return max(
        1,
        int((normalized_start + duration - now.astimezone(UTC)).total_seconds()) + 1,
    )
