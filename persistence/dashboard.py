"""运维 Dashboard 只读模型使用的 SQLAlchemy 查询。"""

import time
from collections import OrderedDict
from datetime import UTC, datetime
from threading import RLock

from sqlalchemy import and_, case, func, or_, select, union
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from domain.enums import ExecutionStatus
from domain.security import redact_sensitive
from domain.workers import WORKER_ONLINE_WINDOW
from persistence.models import (
    OutboxEventRecord,
    PullRequestVersionRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.resource_scope import resource_predicate
from persistence.workers import WorkerOverview, load_worker_overview
from services.dashboard import (
    DashboardChangeState,
    DashboardData,
    DashboardPersistenceError,
    ReviewCursor,
    ReviewListItem,
    as_utc,
    required_utc,
)
from services.rbac import ResourceScope


class SqlAlchemyDashboardRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """保存 Dashboard 只读查询所需的 SQLAlchemy 会话工厂。

        参数：
            sessions: 已绑定数据库引擎的 ``sessionmaker``。仓储不会在构造时打开
                连接，实际会话由 :meth:`load` 创建并在退出上下文后释放。
        """
        self._sessions = sessions
        self._count_cache: OrderedDict[
            tuple[ResourceScope | None, ExecutionStatus | None, str], tuple[float, int]
        ] = OrderedDict()
        self._count_lock = RLock()

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
            Worker 返回准确在线统计与最多三条活跃预览，避免历史实例 ID
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
                if include_overview:
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
                else:
                    total_reviews = self._review_count(
                        session, scope, execution_status, query, reuse=cursor is not None,
                    )
                    status_counts = {}

                reviews, has_more = self._load_reviews(
                    session, limit, cursor, scope, execution_status, query
                )

                overview = (
                    load_worker_overview(session, worker_cutoff or datetime.now(UTC) - WORKER_ONLINE_WINDOW, scope)
                    if include_overview else WorkerOverview((), 0, 0)
                )
                return DashboardData(
                    total_reviews=total_reviews,
                    status_counts=status_counts,
                    recent_reviews=reviews,
                    workers=overview.workers,
                    worker_online_count=overview.online_count,
                    worker_busy_count=overview.busy_count,
                    has_more=has_more,
                )
            except (SQLAlchemyError, ValueError) as exc:
                raise DashboardPersistenceError(
                    "dashboard data could not be loaded"
                ) from exc

    def _review_count(self, session: Session, scope: ResourceScope | None,
                      execution_status: ExecutionStatus | None, query: str,
                      *, reuse: bool) -> int:
        # 首页总是刷新；后续页在五秒内复用同一权限范围和筛选条件的计数。
        # 只缓存整数，不缓存记录；上限128项，查询仍使用原有过滤索引。
        key = (scope, execution_status, query)
        with self._count_lock:
            cached = self._count_cache.get(key)
            if reuse and cached is not None and cached[0] > time.monotonic():
                self._count_cache.move_to_end(key)
                return cached[1]
            count = int(session.scalar(select(func.count(ReviewRunRecord.id)).where(
                *self._review_filters(scope, execution_status, query)
            )) or 0)
            self._count_cache[key] = (time.monotonic() + 5, count)
            self._count_cache.move_to_end(key)
            while len(self._count_cache) > 128:
                self._count_cache.popitem(last=False)
            return count

    def change_state(
        self,
        *,
        scope: ResourceScope | None = None,
        worker_cutoff: datetime | None = None,
    ) -> DashboardChangeState:
        """读取事件与在线节点变化；历史心跳不进入高频变化列表。"""

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
                overview = load_worker_overview(
                    session, worker_cutoff or datetime.now(UTC) - WORKER_ONLINE_WINDOW, scope,
                )
                return DashboardChangeState(
                    latest_event_id=latest_event.id if latest_event else None,
                    latest_event_at=as_utc(latest_event.occurred_at) if latest_event else None,
                    workers=overview.workers,
                    worker_online_count=overview.online_count,
                    worker_busy_count=overview.busy_count,
                )
            except SQLAlchemyError as exc:
                raise DashboardPersistenceError(
                    "dashboard change state could not be loaded"
                ) from exc

    def _load_reviews(self, session: Session, limit: int, cursor: ReviewCursor | None,
                      scope: ResourceScope | None, execution_status: ExecutionStatus | None,
                      query: str) -> tuple[tuple[ReviewListItem, ...], bool]:
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
                ReviewRunRecord.snapshot_review,
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
        reviews_query = reviews_query.where(
            *self._review_filters(scope, execution_status, query)
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
                    snapshot_review=row.snapshot_review,
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
        return reviews, has_more

    @staticmethod
    def _review_filters(scope, execution_status=None, query=""):
        filters = [resource_predicate(
            scope, installation_column=ReviewRunRecord.installation_id,
            repository_column=ReviewRunRecord.repository,
            repository_key_column=ReviewRunRecord.repository_key,
        )]
        if execution_status is not None:
            state_column = ReviewRunRecord.workflow_status if execution_status in {
                ExecutionStatus.PAUSED, ExecutionStatus.AWAITING_APPROVAL, ExecutionStatus.AWAITING_PUBLISH,
                ExecutionStatus.PUBLISHING, ExecutionStatus.APPROVED, ExecutionStatus.REJECTED,
            } else ReviewRunRecord.execution_status
            filters.append(state_column == execution_status.value)
        if query and len(query) < 3 and not query.isdecimal():
            raise ValueError("搜索请输入至少3个字符，PR编号可直接输入数字")
        if query:
            # 各表独立检索再 UNION 主键，避免跨表 OR 迫使整个 JOIN 扫描。
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            run_matches: list[ColumnElement[bool]] = [column.ilike(pattern, escape="\\") for column in (
                ReviewRunRecord.repository, ReviewRunRecord.head_sha,
            )]
            if query.isdecimal() and len(query) <= 18:
                run_matches.append(ReviewRunRecord.pull_request_number == int(query))
                if len(query) < 3:
                    filters.append(ReviewRunRecord.pull_request_number == int(query))
                    return filters
            pr_matches = [column.ilike(pattern, escape="\\") for column in (
                PullRequestVersionRecord.title, PullRequestVersionRecord.author_login,
                PullRequestVersionRecord.head_ref, PullRequestVersionRecord.base_ref,
                PullRequestVersionRecord.head_repository, PullRequestVersionRecord.base_repository,
            )]
            matching_ids = union(
                select(ReviewRunRecord.id).where(or_(*run_matches)),
                select(ReviewRunRecord.id).join(PullRequestVersionRecord,
                    PullRequestVersionRecord.review_version_key == ReviewRunRecord.review_version_key,
                ).where(or_(*pr_matches)),
            )
            filters.append(ReviewRunRecord.id.in_(matching_ids))
        return filters
