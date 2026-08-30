"""运维 Dashboard 只读模型使用的 SQLAlchemy 查询。"""

from datetime import datetime

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ExecutionStatus, WorkerStatus
from domain.security import redact_sensitive
from persistence.models import (
    OutboxEventRecord,
    PullRequestVersionRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
    WorkerHeartbeatRecord,
)
from persistence.resource_scope import resource_predicate
from services.dashboard import (
    DashboardChangeState,
    DashboardData,
    DashboardPersistenceError,
    ReviewCursor,
    ReviewListItem,
    StoredWorkerHeartbeat,
    as_utc,
    required_utc,
)
from services.rbac import ResourceScope

_MAX_DASHBOARD_WORKERS = 100


class SqlAlchemyDashboardRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """保存 Dashboard 只读查询所需的 SQLAlchemy 会话工厂。

        参数：
            sessions: 已绑定数据库引擎的 ``sessionmaker``。仓储不会在构造时打开
                连接，实际会话由 :meth:`load` 创建并在退出上下文后释放。
        """
        self._sessions = sessions

    def load(
        self,
        limit: int,
        cursor: ReviewCursor | None = None,
        scope: ResourceScope | None = None,
    ) -> DashboardData:
        """从数据库读取 Dashboard 所需的聚合数据。

        查询一次任务总数和按状态分组的数量，再读取最近任务及最近一次心跳。
        这里只负责把 ORM 记录转换成服务层读模型，不做在线判定；在线窗口和
        缺省状态由 ``DashboardService`` 统一处理。任何 SQLAlchemy 或枚举转换
        错误都会包装成 ``DashboardPersistenceError``。

        参数：
            limit: 最近任务查询的最大行数。调用方通常已校验 1 到 100，但 SQL
                仓储仍把它原样传给数据库的 ``LIMIT``。

        返回：
            按创建时间倒序排列的最近任务、所有状态计数、总运行数和最后心跳。
            Worker 心跳按最近更新时间倒序返回，并硬限制为 100 条，避免异常实例 ID
            持续增长时形成无界查询。

        异常：
            DashboardPersistenceError: SQL 查询失败、记录里的状态字符串无法映射
            到领域枚举，或时间/记录转换失败。底层异常作为原因保留，但对 API 只
            暴露稳定的服务不可用信息。

        该方法执行多个只读查询，调用方看到的是同一会话内的近似快照，不提供跨
        查询的强一致锁；实时一致性由 SSE 下一次轮询补齐。
        """
        with self._sessions() as session:
            try:
                # 总数和状态分组原本是两次扫描；条件聚合在同一次索引/表扫描中
                # 返回两者。使用 CASE 而不是方言专属的 FILTER，兼容 SQLite 和
                # PostgreSQL 的测试/生产数据库。
                stats_columns = tuple(
                    func.sum(
                        case(
                            (
                                ReviewRunRecord.execution_status == status.value,
                                1,
                            ),
                            else_=0,
                        )
                    ).label(f"status_{status.value}")
                    for status in ExecutionStatus
                )
                stats_row = session.execute(
                    select(
                        func.count(ReviewRunRecord.id).label("total_reviews"),
                        *stats_columns,
                    )
                    .select_from(ReviewRunRecord)
                    .where(
                        resource_predicate(
                            scope,
                            installation_column=ReviewRunRecord.installation_id,
                            repository_column=ReviewRunRecord.repository,
                            repository_key_column=ReviewRunRecord.repository_key,
                        )
                    )
                ).one()
                total_reviews = int(stats_row.total_reviews or 0)
                status_counts = {
                    status: int(getattr(stats_row, f"status_{status.value}") or 0)
                    for status in ExecutionStatus
                }

                reviews_query = (
                    select(
                        ReviewRunRecord.id.label("review_run_id"),
                        ReviewTaskRecord.id.label("review_task_id"),
                        ReviewRunRecord.repository,
                        ReviewRunRecord.pull_request_number,
                        ReviewRunRecord.head_sha,
                        PullRequestVersionRecord.title.label("pr_title"),
                        PullRequestVersionRecord.author_login.label(
                            "pr_author_login"
                        ),
                        PullRequestVersionRecord.html_url.label("pr_html_url"),
                        PullRequestVersionRecord.head_repository,
                        PullRequestVersionRecord.head_ref,
                        PullRequestVersionRecord.base_repository,
                        PullRequestVersionRecord.base_ref,
                        ReviewRunRecord.execution_status,
                        ReviewRunRecord.workflow_status,
                        ReviewTaskRecord.attempt_count,
                        ReviewTaskRecord.max_attempts,
                        ReviewTaskRecord.last_error,
                        ReviewTaskRecord.last_error_code,
                        ReviewTaskRecord.last_error_retryable,
                        ReviewTaskRecord.last_error_details,
                        ReviewRunRecord.review_conclusion,
                        ReviewRunRecord.coverage_status,
                        ReviewTaskRecord.model_attempt_count,
                        ReviewRunRecord.created_at,
                        ReviewRunRecord.updated_at,
                    )
                    .join(
                        ReviewTaskRecord,
                        ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
                    )
                    .outerjoin(
                        PullRequestVersionRecord,
                        PullRequestVersionRecord.review_version_key
                        == ReviewRunRecord.review_version_key,
                    )
                    .order_by(
                        ReviewRunRecord.created_at.desc(),
                        ReviewRunRecord.id.desc(),
                    )
                    .where(
                        resource_predicate(
                            scope,
                            installation_column=ReviewRunRecord.installation_id,
                            repository_column=ReviewRunRecord.repository,
                            repository_key_column=ReviewRunRecord.repository_key,
                        )
                    )
                )
                if cursor is not None:
                    reviews_query = reviews_query.where(
                        or_(
                            ReviewRunRecord.created_at < cursor.created_at,
                            and_(
                                ReviewRunRecord.created_at == cursor.created_at,
                                ReviewRunRecord.id < cursor.review_run_id,
                            ),
                        )
                    )
                raw_rows = session.execute(
                    reviews_query.limit(limit + 1)
                ).all()
                has_more = len(raw_rows) > limit
                rows = raw_rows[:limit]

                # 只对当前页的运行批量读取模型完成时间和 Finding 计数。这样既
                # 避免每行两个相关子查询，也不会为了第一页而聚合整张 findings
                # 表；本页最多 100 个 ID，查询次数始终为 O(1)。
                review_metrics: dict[str, tuple[datetime | None, int, int]] = {}
                review_ids = tuple(row.review_run_id for row in rows)
                if review_ids:
                    metrics_rows = session.execute(
                        select(
                            ReviewRunRecord.id.label("review_run_id"),
                            func.max(
                                ReviewPlanRecord.model_review_completed_at
                            ).label("model_review_completed_at"),
                            func.count(ReviewFindingRecord.id).label("finding_count"),
                            func.coalesce(
                                func.sum(
                                    case(
                                        (
                                            ReviewFindingRecord.adjudication_status
                                            == "unreviewed",
                                            1,
                                        ),
                                        else_=0,
                                    )
                                ),
                                0,
                            ).label("unreviewed_finding_count"),
                        )
                        .select_from(ReviewRunRecord)
                        .outerjoin(
                            ReviewPlanRecord,
                            ReviewPlanRecord.review_run_id == ReviewRunRecord.id,
                        )
                        .outerjoin(
                            ReviewFindingRecord,
                            ReviewFindingRecord.review_run_id == ReviewRunRecord.id,
                        )
                        .where(ReviewRunRecord.id.in_(review_ids))
                        .group_by(ReviewRunRecord.id)
                    ).all()
                    review_metrics = {
                        metric.review_run_id: (
                            metric.model_review_completed_at,
                            int(metric.finding_count or 0),
                            int(metric.unreviewed_finding_count or 0),
                        )
                        for metric in metrics_rows
                    }

                review_items: list[ReviewListItem] = []
                for row in rows:
                    (
                        model_review_completed_at,
                        finding_count,
                        unreviewed_finding_count,
                    ) = review_metrics.get(row.review_run_id, (None, 0, 0))
                    safe_last_error = (
                        redact_sensitive(row.last_error)
                        if row.last_error is not None
                        else None
                    )
                    safe_error_code = (
                        redact_sensitive(row.last_error_code)
                        if row.last_error_code is not None
                        else None
                    )
                    safe_error_details = (
                        redact_sensitive(row.last_error_details)
                        if row.last_error_details is not None
                        else None
                    )
                    review_items.append(
                        ReviewListItem(
                            review_run_id=row.review_run_id,
                            review_task_id=row.review_task_id,
                            repository=row.repository,
                            pull_request_number=row.pull_request_number,
                            head_sha=row.head_sha,
                            pr_title=row.pr_title,
                            pr_author_login=row.pr_author_login,
                            pr_html_url=row.pr_html_url,
                            head_repository=row.head_repository,
                            head_ref=row.head_ref,
                            base_repository=row.base_repository,
                            base_ref=row.base_ref,
                            execution_status=ExecutionStatus(
                                row.execution_status
                            ),
                            workflow_status=ExecutionStatus(
                                row.workflow_status or row.execution_status
                            ),
                            attempt_count=row.attempt_count,
                            max_attempts=row.max_attempts,
                            last_error=(
                                safe_last_error
                                if isinstance(safe_last_error, str)
                                else None
                            ),
                            last_error_code=(
                                safe_error_code
                                if isinstance(safe_error_code, str)
                                else None
                            ),
                            last_error_retryable=row.last_error_retryable,
                            last_error_details=(
                                safe_error_details
                                if isinstance(safe_error_details, dict)
                                else None
                            ),
                            review_conclusion=row.review_conclusion,
                            coverage_status=row.coverage_status,
                            model_review_completed_at=as_utc(
                                model_review_completed_at
                            ),
                            finding_count=finding_count,
                            unreviewed_finding_count=unreviewed_finding_count,
                            model_attempt_count=row.model_attempt_count,
                            created_at=required_utc(row.created_at, "review.created_at"),
                            updated_at=required_utc(row.updated_at, "review.updated_at"),
                        )
                    )
                reviews = tuple(review_items)

                heartbeat_rows = session.execute(
                    select(
                        WorkerHeartbeatRecord.worker_id,
                        WorkerHeartbeatRecord.status,
                        WorkerHeartbeatRecord.current_task_id,
                        WorkerHeartbeatRecord.started_at,
                        WorkerHeartbeatRecord.last_seen_at,
                        ReviewRunRecord.installation_id.label("task_installation_id"),
                        ReviewRunRecord.repository.label("task_repository"),
                    )
                    .outerjoin(
                        ReviewTaskRecord,
                        WorkerHeartbeatRecord.current_task_id == ReviewTaskRecord.id,
                    )
                    .outerjoin(
                        ReviewRunRecord,
                        ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
                    )
                    .order_by(
                        WorkerHeartbeatRecord.last_seen_at.desc(),
                        WorkerHeartbeatRecord.worker_id.asc(),
                    )
                    .limit(_MAX_DASHBOARD_WORKERS)
                ).all()
                workers = tuple(
                    StoredWorkerHeartbeat(
                        worker_id=heartbeat.worker_id,
                        status=WorkerStatus(heartbeat.status),
                        current_task_id=(
                            heartbeat.current_task_id
                            if (
                                scope is None
                                or scope.unrestricted
                                or (
                                    heartbeat.task_installation_id is not None
                                    and heartbeat.task_repository is not None
                                    and scope.allows(
                                        heartbeat.task_installation_id,
                                        heartbeat.task_repository,
                                    )
                                )
                            )
                            else None
                        ),
                        started_at=required_utc(
                            heartbeat.started_at,
                            "worker.started_at",
                        ),
                        last_seen_at=required_utc(
                            heartbeat.last_seen_at,
                            "worker.last_seen_at",
                        ),
                    )
                    for heartbeat in heartbeat_rows
                )
                return DashboardData(
                    total_reviews=total_reviews,
                    status_counts=status_counts,
                    recent_reviews=reviews,
                    workers=workers,
                    has_more=has_more,
                )
            except (SQLAlchemyError, ValueError) as exc:
                raise DashboardPersistenceError(
                    "dashboard data could not be loaded"
                ) from exc

    def change_state(
        self,
        *,
        scope: ResourceScope | None = None,
    ) -> DashboardChangeState:
        """用两个索引首行查询读取 SSE 变化状态。"""

        with self._sessions() as session:
            try:
                latest_event_query = select(
                    OutboxEventRecord.id,
                    OutboxEventRecord.occurred_at,
                ).order_by(
                    OutboxEventRecord.occurred_at.desc(),
                    OutboxEventRecord.id.desc(),
                ).limit(1)
                if scope is not None and not scope.unrestricted:
                    latest_event_query = (
                        latest_event_query.join(
                            ReviewRunRecord,
                            and_(
                                OutboxEventRecord.aggregate_type == "review_run",
                                OutboxEventRecord.aggregate_id == ReviewRunRecord.id,
                            ),
                        )
                        .where(
                            resource_predicate(
                                scope,
                                installation_column=ReviewRunRecord.installation_id,
                                repository_column=ReviewRunRecord.repository,
                                repository_key_column=ReviewRunRecord.repository_key,
                            )
                        )
                    )
                latest_event = session.execute(latest_event_query).one_or_none()
                worker_rows = session.execute(
                    select(
                        WorkerHeartbeatRecord.worker_id,
                        WorkerHeartbeatRecord.status,
                        WorkerHeartbeatRecord.current_task_id,
                        WorkerHeartbeatRecord.started_at,
                        WorkerHeartbeatRecord.last_seen_at,
                        ReviewRunRecord.installation_id.label("task_installation_id"),
                        ReviewRunRecord.repository.label("task_repository"),
                    )
                    .outerjoin(
                        ReviewTaskRecord,
                        WorkerHeartbeatRecord.current_task_id == ReviewTaskRecord.id,
                    )
                    .outerjoin(
                        ReviewRunRecord,
                        ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
                    )
                    .order_by(
                        WorkerHeartbeatRecord.last_seen_at.desc(),
                        WorkerHeartbeatRecord.worker_id.desc(),
                    )
                    .limit(_MAX_DASHBOARD_WORKERS)
                ).all()
                return DashboardChangeState(
                    latest_event_id=latest_event.id if latest_event else None,
                    latest_event_at=(
                        as_utc(latest_event.occurred_at) if latest_event else None
                    ),
                    workers=tuple(
                        StoredWorkerHeartbeat(
                            worker_id=worker.worker_id,
                            status=WorkerStatus(worker.status),
                            current_task_id=(
                                worker.current_task_id
                                if (
                                    scope is None
                                    or scope.unrestricted
                                    or (
                                        worker.task_installation_id is not None
                                        and worker.task_repository is not None
                                        and scope.allows(
                                            worker.task_installation_id,
                                            worker.task_repository,
                                        )
                                    )
                                )
                                else None
                            ),
                            started_at=required_utc(
                                worker.started_at,
                                "worker.started_at",
                            ),
                            last_seen_at=required_utc(
                                worker.last_seen_at,
                                "worker.last_seen_at",
                            ),
                        )
                        for worker in worker_rows
                    ),
                )
            except SQLAlchemyError as exc:
                raise DashboardPersistenceError(
                    "dashboard change state could not be loaded"
                ) from exc
