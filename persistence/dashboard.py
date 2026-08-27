"""运维 Dashboard 只读模型使用的 SQLAlchemy 查询。"""

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ExecutionStatus, WorkerStatus
from domain.security import redact_sensitive
from persistence.models import (
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
    PullRequestVersionRecord,
    WorkerHeartbeatRecord,
)
from services.dashboard import (
    DashboardData,
    DashboardPersistenceError,
    ReviewListItem,
    StoredWorkerHeartbeat,
    as_utc,
)


class SqlAlchemyDashboardRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """保存 Dashboard 只读查询所需的 SQLAlchemy 会话工厂。

        参数：
            sessions: 已绑定数据库引擎的 ``sessionmaker``。仓储不会在构造时打开
                连接，实际会话由 :meth:`load` 创建并在退出上下文后释放。
        """
        self._sessions = sessions

    def load(self, limit: int) -> DashboardData:
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
            没有心跳时 ``latest_worker`` 为 ``None``。

        异常：
            DashboardPersistenceError: SQL 查询失败、记录里的状态字符串无法映射
            到领域枚举，或时间/记录转换失败。底层异常作为原因保留，但对 API 只
            暴露稳定的服务不可用信息。

        该方法执行多个只读查询，调用方看到的是同一会话内的近似快照，不提供跨
        查询的强一致锁；实时一致性由 SSE 下一次轮询补齐。
        """
        with self._sessions() as session:
            try:
                total_reviews = int(
                    session.scalar(
                        select(func.count()).select_from(ReviewRunRecord)
                    )
                    or 0
                )
                grouped_counts = session.execute(
                    select(
                        ReviewRunRecord.execution_status,
                        func.count(ReviewRunRecord.id),
                    ).group_by(ReviewRunRecord.execution_status)
                )
                status_counts = {
                    ExecutionStatus(status): int(count)
                    for status, count in grouped_counts
                }

                model_completed_query = (
                    select(ReviewPlanRecord.model_review_completed_at)
                    .where(ReviewPlanRecord.review_run_id == ReviewRunRecord.id)
                    .scalar_subquery()
                )
                finding_count_query = (
                    select(func.count(ReviewFindingRecord.id))
                    .where(ReviewFindingRecord.review_run_id == ReviewRunRecord.id)
                    .scalar_subquery()
                )
                unverified_finding_count_query = (
                    select(func.count(ReviewFindingRecord.id))
                    .where(
                        ReviewFindingRecord.review_run_id == ReviewRunRecord.id,
                        ReviewFindingRecord.verification_status == "unverified",
                    )
                    .scalar_subquery()
                )

                rows = session.execute(
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
                        model_completed_query.label("model_review_completed_at"),
                        finding_count_query.label("finding_count"),
                        unverified_finding_count_query.label(
                            "unverified_finding_count"
                        ),
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
                    .order_by(ReviewRunRecord.created_at.desc())
                    .limit(limit)
                )
                review_items: list[ReviewListItem] = []
                for row in rows:
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
                                row.model_review_completed_at
                            ),
                            finding_count=int(row.finding_count or 0),
                            unverified_finding_count=int(
                                row.unverified_finding_count or 0
                            ),
                            model_attempt_count=row.model_attempt_count,
                            created_at=as_utc(row.created_at),
                            updated_at=as_utc(row.updated_at),
                        )
                    )
                reviews = tuple(review_items)

                heartbeat = session.scalar(
                    select(WorkerHeartbeatRecord)
                    .order_by(WorkerHeartbeatRecord.last_seen_at.desc())
                    .limit(1)
                )
                stored_worker = (
                    StoredWorkerHeartbeat(
                        worker_id=heartbeat.worker_id,
                        status=WorkerStatus(heartbeat.status),
                        current_task_id=heartbeat.current_task_id,
                        started_at=as_utc(heartbeat.started_at),
                        last_seen_at=as_utc(heartbeat.last_seen_at),
                    )
                    if heartbeat is not None
                    else None
                )
                return DashboardData(
                    total_reviews=total_reviews,
                    status_counts=status_counts,
                    recent_reviews=reviews,
                    latest_worker=stored_worker,
                )
            except (SQLAlchemyError, ValueError) as exc:
                raise DashboardPersistenceError(
                    "dashboard data could not be loaded"
                ) from exc
