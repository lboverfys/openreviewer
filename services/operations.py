"""运行就绪、指标、Outbox 发布和数据保留的应用边界。"""

import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from domain.enums import ExecutionStatus
from domain.security import redact_sensitive

# 就绪探针必须与 Alembic 当前 head 完全一致；否则新迁移后的实例会被错误摘流量。
EXPECTED_DATABASE_REVISION = "20260913_0058"
OUTBOX_LOGGER = logging.getLogger("openreviewer.outbox")
LOGGER = logging.getLogger("openreviewer.operations")


class OperationsError(RuntimeError):
    """运维状态无法从持久层安全读取或更新。"""


def _bounded_integer(
    values: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = values.get(name, str(default))
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


@dataclass(frozen=True, slots=True)
class OperationsSettings:
    """所有运维循环都使用的有界配置。"""

    worker_max_age: timedelta = timedelta(seconds=45)
    outbox_batch_size: int = 100
    outbox_lease_duration: timedelta = timedelta(seconds=30)
    outbox_retry_delay: timedelta = timedelta(seconds=15)
    cleanup_interval: timedelta = timedelta(minutes=5)
    cleanup_batch_size: int = 500
    published_outbox_retention: timedelta = timedelta(days=14)
    admin_session_retention: timedelta = timedelta(days=7)
    worker_heartbeat_retention: timedelta = timedelta(days=7)
    webhook_retention: timedelta = timedelta(days=90)
    review_retention: timedelta = timedelta(days=180)
    quota_bucket_retention: timedelta = timedelta(days=3)
    finding_evaluation_retention: timedelta = timedelta(days=730)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "OperationsSettings":
        values = os.environ if environment is None else environment
        return cls(
            worker_max_age=timedelta(
                seconds=_bounded_integer(
                    values,
                    "OPENREVIEWER_READY_WORKER_MAX_AGE_SECONDS",
                    45,
                    minimum=10,
                    maximum=600,
                )
            ),
            outbox_batch_size=_bounded_integer(
                values,
                "OPENREVIEWER_OUTBOX_BATCH_SIZE",
                100,
                minimum=1,
                maximum=500,
            ),
            outbox_lease_duration=timedelta(
                seconds=_bounded_integer(
                    values,
                    "OPENREVIEWER_OUTBOX_LEASE_SECONDS",
                    30,
                    minimum=5,
                    maximum=600,
                )
            ),
            outbox_retry_delay=timedelta(
                seconds=_bounded_integer(
                    values,
                    "OPENREVIEWER_OUTBOX_RETRY_SECONDS",
                    15,
                    minimum=1,
                    maximum=3600,
                )
            ),
            cleanup_interval=timedelta(
                seconds=_bounded_integer(
                    values,
                    "OPENREVIEWER_CLEANUP_INTERVAL_SECONDS",
                    300,
                    minimum=30,
                    maximum=86400,
                )
            ),
            cleanup_batch_size=_bounded_integer(
                values,
                "OPENREVIEWER_CLEANUP_BATCH_SIZE",
                500,
                minimum=1,
                maximum=2000,
            ),
            published_outbox_retention=timedelta(
                days=_bounded_integer(
                    values,
                    "OPENREVIEWER_OUTBOX_RETENTION_DAYS",
                    14,
                    minimum=1,
                    maximum=3650,
                )
            ),
            admin_session_retention=timedelta(
                days=_bounded_integer(
                    values,
                    "OPENREVIEWER_SESSION_RETENTION_DAYS",
                    7,
                    minimum=1,
                    maximum=3650,
                )
            ),
            worker_heartbeat_retention=timedelta(
                days=_bounded_integer(
                    values,
                    "OPENREVIEWER_HEARTBEAT_RETENTION_DAYS",
                    7,
                    minimum=1,
                    maximum=3650,
                )
            ),
            webhook_retention=timedelta(
                days=_bounded_integer(
                    values,
                    "OPENREVIEWER_WEBHOOK_RETENTION_DAYS",
                    90,
                    minimum=7,
                    maximum=3650,
                )
            ),
            review_retention=timedelta(
                days=_bounded_integer(
                    values,
                    "OPENREVIEWER_REVIEW_RETENTION_DAYS",
                    180,
                    minimum=30,
                    maximum=3650,
                )
            ),
            quota_bucket_retention=timedelta(
                days=_bounded_integer(
                    values,
                    "OPENREVIEWER_QUOTA_BUCKET_RETENTION_DAYS",
                    3,
                    minimum=2,
                    maximum=30,
                )
            ),
            finding_evaluation_retention=timedelta(
                days=_bounded_integer(
                    values,
                    "OPENREVIEWER_FINDING_EVALUATION_RETENTION_DAYS",
                    730,
                    minimum=30,
                    maximum=3650,
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    database: bool
    migration: bool
    worker: bool

    @property
    def ready(self) -> bool:
        return self.database and self.migration and self.worker

    def public_checks(self) -> dict[str, Literal["ok", "failed"]]:
        return {
            "database": "ok" if self.database else "failed",
            "migration": "ok" if self.migration else "failed",
            "worker": "ok" if self.worker else "failed",
        }


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    review_counts: Mapping[str, int]
    task_counts: Mapping[str, int]
    model_http_call_counts: Mapping[str, int]
    fresh_workers: int
    pending_outbox_events: int
    outbox_oldest_age_seconds: float
    outbox_max_publish_attempts: int
    claimable_tasks: int
    oldest_claimable_task_age_seconds: float
    retrieval_pending: int = 0
    retrieval_oldest_age_seconds: float = 0


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    id: str
    event_key: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: Mapping[str, object]
    occurred_at: datetime
    publish_attempts: int


@dataclass(frozen=True, slots=True)
class RetentionCutoffs:
    published_outbox: datetime
    admin_sessions: datetime
    worker_heartbeats: datetime
    webhooks: datetime
    reviews: datetime
    # 允许旧的仓储调用方省略新数据类；正式运维路径由 from_settings 提供。
    quota_buckets: datetime | None = None
    finding_evaluations: datetime | None = None

    @classmethod
    def from_settings(
        cls,
        settings: OperationsSettings,
        now: datetime,
    ) -> "RetentionCutoffs":
        return cls(
            published_outbox=now - settings.published_outbox_retention,
            admin_sessions=now - settings.admin_session_retention,
            worker_heartbeats=now - settings.worker_heartbeat_retention,
            webhooks=now - settings.webhook_retention,
            reviews=now - settings.review_retention,
            quota_buckets=now - settings.quota_bucket_retention,
            finding_evaluations=now - settings.finding_evaluation_retention,
        )


@dataclass(frozen=True, slots=True)
class CleanupResult:
    published_outbox_events: int = 0
    admin_sessions: int = 0
    worker_heartbeats: int = 0
    webhook_deliveries: int = 0
    review_runs: int = 0
    pull_request_versions: int = 0
    quota_buckets: int = 0
    finding_evaluations: int = 0
    retrieval_records: int = 0

    @property
    def total(self) -> int:
        return sum(
            (
                self.published_outbox_events,
                self.admin_sessions,
                self.worker_heartbeats,
                self.webhook_deliveries,
                self.review_runs,
                self.pull_request_versions,
                self.quota_buckets,
                self.finding_evaluations,
                self.retrieval_records,
            )
        )


class OperationsRepository(Protocol):
    def readiness(
        self,
        expected_revision: str,
        worker_max_age: timedelta,
    ) -> ReadinessSnapshot: ...

    def metrics(self, worker_max_age: timedelta) -> MetricsSnapshot: ...

    def claim_outbox(
        self,
        owner: str,
        *,
        batch_size: int,
        lease_duration: timedelta,
    ) -> tuple[OutboxEvent, ...]: ...

    def complete_outbox(self, owner: str, event_ids: tuple[str, ...]) -> int: ...

    def release_outbox(
        self,
        owner: str,
        event_ids: tuple[str, ...],
        *,
        retry_delay: timedelta,
        error: str,
    ) -> int: ...

    def cleanup(
        self,
        cutoffs: RetentionCutoffs,
        *,
        batch_size: int,
    ) -> CleanupResult: ...


def render_prometheus_metrics(snapshot: MetricsSnapshot) -> str:
    """把固定低基数快照渲染为 Prometheus 文本格式。"""

    lines = [
        "# HELP openreviewer_reviews Current review runs by execution status.",
        "# TYPE openreviewer_reviews gauge",
    ]
    for status in ExecutionStatus:
        lines.append(
            f'openreviewer_reviews{{status="{status.value}"}} '
            f"{snapshot.review_counts.get(status.value, 0)}"
        )
    lines.extend(
        (
            "# HELP openreviewer_tasks Current tasks by execution status.",
            "# TYPE openreviewer_tasks gauge",
        )
    )
    for status in ExecutionStatus:
        lines.append(
            f'openreviewer_tasks{{status="{status.value}"}} '
            f"{snapshot.task_counts.get(status.value, 0)}"
        )
    lines.extend(
        (
            "# HELP openreviewer_model_http_calls Model HTTP ledger rows by status.",
            "# TYPE openreviewer_model_http_calls gauge",
        )
    )
    for call_status in ("reserved", "settled", "uncertain"):
        lines.append(
            f'openreviewer_model_http_calls{{status="{call_status}"}} '
            f"{snapshot.model_http_call_counts.get(call_status, 0)}"
        )
    lines.extend(
        (
            "# HELP openreviewer_workers_fresh Workers with a fresh heartbeat.",
            "# TYPE openreviewer_workers_fresh gauge",
            f"openreviewer_workers_fresh {snapshot.fresh_workers}",
            "# HELP openreviewer_outbox_pending Unpublished Outbox events.",
            "# TYPE openreviewer_outbox_pending gauge",
            f"openreviewer_outbox_pending {snapshot.pending_outbox_events}",
            "# HELP openreviewer_outbox_oldest_age_seconds Age of the oldest unpublished event.",
            "# TYPE openreviewer_outbox_oldest_age_seconds gauge",
            "openreviewer_outbox_oldest_age_seconds "
            f"{snapshot.outbox_oldest_age_seconds:.3f}",
            "# HELP openreviewer_outbox_max_publish_attempts Maximum attempts among unpublished events.",
            "# TYPE openreviewer_outbox_max_publish_attempts gauge",
            "openreviewer_outbox_max_publish_attempts "
            f"{snapshot.outbox_max_publish_attempts}",
            "# HELP openreviewer_tasks_claimable Tasks ready to be claimed now.",
            "# TYPE openreviewer_tasks_claimable gauge",
            f"openreviewer_tasks_claimable {snapshot.claimable_tasks}",
            "# HELP openreviewer_task_queue_oldest_age_seconds Age of the oldest claimable task.",
            "# TYPE openreviewer_task_queue_oldest_age_seconds gauge",
            "openreviewer_task_queue_oldest_age_seconds "
            f"{snapshot.oldest_claimable_task_age_seconds:.3f}",
            "# HELP openreviewer_retrieval_pending Queued or building code indexes.",
            "# TYPE openreviewer_retrieval_pending gauge",
            f"openreviewer_retrieval_pending {snapshot.retrieval_pending}",
            "# HELP openreviewer_retrieval_oldest_age_seconds Age of the oldest unfinished index.",
            "# TYPE openreviewer_retrieval_oldest_age_seconds gauge",
            f"openreviewer_retrieval_oldest_age_seconds {snapshot.retrieval_oldest_age_seconds:.3f}",
        )
    )
    return "\n".join(lines) + "\n"


class OperationsService:
    """协调只读探针和不持有事务的 Outbox 发布。"""

    def __init__(
        self,
        repository: OperationsRepository,
        settings: OperationsSettings | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        publisher: Callable[[OutboxEvent], None] | None = None,
    ) -> None:
        self._repository = repository
        self.settings = settings or OperationsSettings()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._publisher = publisher or self._log_event

    def readiness(self) -> ReadinessSnapshot:
        return self._repository.readiness(
            EXPECTED_DATABASE_REVISION,
            self.settings.worker_max_age,
        )

    def metrics(self) -> str:
        return render_prometheus_metrics(
            self._repository.metrics(self.settings.worker_max_age)
        )

    def publish_pending(self, owner: str) -> int:
        events = self._repository.claim_outbox(
            owner,
            batch_size=self.settings.outbox_batch_size,
            lease_duration=self.settings.outbox_lease_duration,
        )
        if not events:
            return 0

        succeeded: list[str] = []
        failed: list[str] = []
        failure_types: set[str] = set()
        for event in events:
            try:
                self._publisher(event)
            except Exception as exc:  # 发布器是可替换的外部边界。
                failed.append(event.id)
                failure_types.add(type(exc).__name__[:100])
            else:
                succeeded.append(event.id)

        if succeeded:
            self._repository.complete_outbox(owner, tuple(succeeded))
        if failed:
            types = ",".join(sorted(failure_types)) or "unknown"
            self._repository.release_outbox(
                owner,
                tuple(failed),
                retry_delay=self.settings.outbox_retry_delay,
                error=f"publisher failed ({types})"[:500],
            )
        return len(succeeded)

    def cleanup(self) -> CleanupResult:
        return self._repository.cleanup(
            RetentionCutoffs.from_settings(self.settings, self._clock()),
            batch_size=self.settings.cleanup_batch_size,
        )

    @staticmethod
    def _log_event(event: OutboxEvent) -> None:
        safe_payload = redact_sensitive(
            {
                "id": event.id,
                "event_key": event.event_key,
                "aggregate_type": event.aggregate_type,
                "aggregate_id": event.aggregate_id,
                "event_type": event.event_type,
                "payload": dict(event.payload),
                "occurred_at": event.occurred_at.isoformat(),
                "publish_attempts": event.publish_attempts,
            }
        )
        OUTBOX_LOGGER.info(
            "outbox_event=%s",
            json.dumps(
                safe_payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )


class WorkerMaintenance:
    """每轮发布 Outbox，并按固定间隔执行一批保留期清理。"""

    def __init__(
        self,
        operations: OperationsService,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._operations = operations
        self._monotonic = monotonic
        self._next_cleanup_at = 0.0

    def run_once(self, owner: str) -> None:
        published = self._operations.publish_pending(owner)
        if published:
            LOGGER.info("本轮已发布 %s 条 Outbox 事件", published)

        now = self._monotonic()
        if now < self._next_cleanup_at:
            return
        self._next_cleanup_at = (
            now + self._operations.settings.cleanup_interval.total_seconds()
        )
        result = self._operations.cleanup()
        if result.total:
            LOGGER.info(
                "本轮保留期清理共删除 %s 条记录，明细=%s",
                result.total,
                {
                    "published_outbox_events": result.published_outbox_events,
                    "admin_sessions": result.admin_sessions,
                    "worker_heartbeats": result.worker_heartbeats,
                    "webhook_deliveries": result.webhook_deliveries,
                    "review_runs": result.review_runs,
                    "pull_request_versions": result.pull_request_versions,
                    "quota_buckets": result.quota_buckets,
                    "finding_evaluations": result.finding_evaluations,
                },
            )
