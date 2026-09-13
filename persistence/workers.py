"""在线摘要走心跳时间索引；历史按启动时间和标识游标分页。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, case, func, not_, select
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import WorkerStatus
from domain.pagination import encode_cursor
from domain.workers import (
    DASHBOARD_WORKER_PREVIEW_LIMIT,
    WORKER_ONLINE_WINDOW,
    WorkerListState,
    WorkerNode,
    WorkerNodePage,
)
from persistence.models import ReviewRunRecord, ReviewTaskRecord, WorkerHeartbeatRecord
from persistence.pagination import apply_cursor
from persistence.resource_scope import resource_predicate
from services.dashboard import StoredWorkerHeartbeat, required_utc
from services.rbac import ResourceScope


def online_condition(cutoff: datetime):
    return and_(WorkerHeartbeatRecord.last_seen_at >= cutoff,
                WorkerHeartbeatRecord.status != WorkerStatus.STOPPING.value)


def worker_query(scope: ResourceScope | None):
    worker = WorkerHeartbeatRecord
    visible = resource_predicate(scope, installation_column=ReviewRunRecord.installation_id,
        repository_column=ReviewRunRecord.repository, repository_key_column=ReviewRunRecord.repository_key)
    return select(worker.worker_id, worker.status, worker.started_at, worker.last_seen_at,
        case((visible, worker.current_task_id), else_=None).label("current_task_id"),
        case((visible, ReviewRunRecord.id), else_=None).label("current_review_run_id"),
    ).select_from(worker).outerjoin(ReviewTaskRecord, worker.current_task_id == ReviewTaskRecord.id
    ).outerjoin(ReviewRunRecord, ReviewTaskRecord.review_run_id == ReviewRunRecord.id)


@dataclass(frozen=True, slots=True)
class WorkerOverview:
    workers: tuple[StoredWorkerHeartbeat, ...]
    online_count: int
    busy_count: int


def load_worker_overview(session: Session, cutoff: datetime, scope: ResourceScope | None) -> WorkerOverview:
    # 窗口统计在 LIMIT 前计算，在线数量不受预览条数影响；同一 SQL 快照内一致。
    rows = session.execute(worker_query(scope).add_columns(
        func.count().over().label("online_count"),
        func.sum(case((WorkerHeartbeatRecord.status == WorkerStatus.BUSY.value, 1), else_=0)).over().label("busy_count"),
    ).where(online_condition(cutoff)).order_by(WorkerHeartbeatRecord.worker_id.asc())
      .limit(DASHBOARD_WORKER_PREVIEW_LIMIT)).all()
    online_count = int(rows[0].online_count) if rows else 0
    busy_count = int(rows[0].busy_count) if rows else 0
    if not rows:
        # 保留单条最近心跳用于区分“离线”与“从未接入”，不展开历史记录。
        rows = session.execute(worker_query(scope).order_by(
            WorkerHeartbeatRecord.last_seen_at.desc(), WorkerHeartbeatRecord.worker_id.desc(),
        ).limit(1)).all()
    workers = tuple(StoredWorkerHeartbeat(worker_id=row.worker_id, status=WorkerStatus(row.status),
        current_task_id=row.current_task_id, started_at=required_utc(row.started_at, "worker.started_at"),
        last_seen_at=required_utc(row.last_seen_at, "worker.last_seen_at")) for row in rows)
    return WorkerOverview(workers, online_count, busy_count)


class WorkerRepository:
    def __init__(self, sessions: sessionmaker[Session], *, clock: Callable[[], datetime] | None = None):
        self.sessions = sessions
        self.clock = clock or (lambda: datetime.now(UTC))

    def page(self, scope: ResourceScope, *, state: WorkerListState = "online", limit: int = 10,
             cursor: str | None = None, retention_days: int = 7) -> WorkerNodePage:
        if state not in {"online", "offline", "all"} or not 1 <= limit <= 100:
            raise ValueError("节点筛选或分页大小无效")
        now = self.clock().astimezone(UTC)
        active = online_condition(now - WORKER_ONLINE_WINDOW)
        query = worker_query(scope).add_columns(active.label("online"))
        if state != "all":
            query = query.where(active if state == "online" else not_(active))
        query = apply_cursor(query, WorkerHeartbeatRecord.started_at, WorkerHeartbeatRecord.worker_id,
                             cursor, identifier_limit=200, cursor_limit=1536).limit(limit + 1)
        with self.sessions() as session:
            rows = session.execute(query).mappings().all()
        items = tuple(WorkerNode.model_validate({**dict(row),
            "started_at": required_utc(row["started_at"], "worker.started_at"),
            "last_seen_at": required_utc(row["last_seen_at"], "worker.last_seen_at"),
        }) for row in rows[:limit])
        return WorkerNodePage(items=items, generated_at=now, retention_days=retention_days,
            next_cursor=encode_cursor(items[-1].started_at, items[-1].worker_id) if len(rows) > limit else None)
