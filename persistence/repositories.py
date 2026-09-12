"""审查持久化边界的 SQLAlchemy 实现。"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import CoverageStatus, ExecutionStatus
from domain.models import ReviewRequest
from persistence.models import (
    OutboxEventRecord,
    ReviewQuotaBucketRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.repository_policy import repository_policy_snapshot
from services.review_quota import (
    ReviewQuotaExceededError,
    ReviewQuotaPolicy,
    quota_windows,
    retry_after_for,
)
from services.reviews import (
    IdempotencyConflictError,
    ReviewPersistenceError,
    ReviewSubmissionResult,
)


class SqlAlchemyReviewRepository:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], UUID] | None = None,
        quota_policy: ReviewQuotaPolicy | None = None,
    ) -> None:
        """初始化审查任务仓储。

        ``clock`` 和 ``uuid_factory`` 是可选的依赖注入点：生产环境使用真实时间
        和随机 UUID，测试可以固定它们来验证事务结果和返回值。

        参数：
            sessions: 已绑定目标数据库的 SQLAlchemy 会话工厂；每次操作都会创建
                独立会话并在方法内完成提交或回滚。
            clock: 可选当前时间函数，用于给三类记录写入相同时间戳。
            uuid_factory: 可选 UUID 生成器；一次新建会调用三次，分别给运行、任务
                和 Outbox 事件分配 ID。

        构造函数不打开会话，也不访问数据库；所有异常在具体仓储操作中处理。
        """
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory or uuid4
        self._quota_policy = quota_policy or ReviewQuotaPolicy.from_environment()

    def create_or_get(
        self,
        request: ReviewRequest,
        idempotency_key: str,
        request_fingerprint: str,
        actor: str = "system",
    ) -> ReviewSubmissionResult:
        """幂等地创建审查运行、任务和 Outbox 事件。

        正常路径先按幂等键查找已有记录；没有记录时，在同一个数据库事务中生成
        三条记录并提交。并发请求可能在查询后同时插入，遇到唯一约束冲突时会回滚
        并重新读取已有记录，因此调用方可以安全重试。请求指纹不同会抛出
        ``IdempotencyConflictError``，其他数据库故障统一转换为
        ``ReviewPersistenceError``。

        参数：
            request: 已通过领域模型校验的审查请求。
            idempotency_key: 已清理且不超过 200 字符的幂等键。
            request_fingerprint: 应用服务根据规范化请求体计算的 SHA-256 摘要。

        返回：
            新建时返回 ``created=True`` 和 ``queued`` 状态；重复请求返回原运行、
            原任务及数据库当前执行状态，并把 ``created`` 设为 ``False``。

        异常：
            IdempotencyConflictError: 已存在记录的指纹与本次指纹不同。
            ReviewPersistenceError: 唯一约束冲突无法重新读取、关联记录缺失，或
                其他 SQLAlchemy 错误导致事务无法可靠完成。

        事务边界：
            新建路径把一条 ``ReviewRunRecord``、一条 ``ReviewTaskRecord`` 和一条
            ``OutboxEventRecord`` 一起提交；任何一条失败都会整体回滚，避免出现
            “任务已创建但没有事件”或“有事件却没有任务”的半成品状态。并发请求
            同时插入时依靠数据库唯一约束仲裁，失败方回滚后再查询胜出的记录。
        """
        with self._sessions() as session:
            try:
                existing = self._find_existing(session, idempotency_key)
                if existing is not None:
                    return self._existing_result(existing, request_fingerprint)

                now = self._clock()
                policy = repository_policy_snapshot(session, request.repository)
                self._reserve_quota(session, actor, request.repository, now)
                review_run_id = str(self._uuid_factory())
                review_task_id = str(self._uuid_factory())
                outbox_event_id = str(self._uuid_factory())

                session.add_all(
                    [
                        ReviewRunRecord(
                            id=review_run_id,
                            review_version_key=request.review_version_key,
                            installation_id=request.installation_id,
                            repository_id=request.repository_id,
                            repository=request.repository,
                            repository_policy=policy,
                            pull_request_number=request.pull_request_number,
                            head_sha=request.head_sha,
                            execution_status=ExecutionStatus.QUEUED.value,
                            workflow_status=ExecutionStatus.QUEUED.value,
                            review_conclusion=None,
                            coverage_status=CoverageStatus.UNKNOWN.value,
                            idempotency_key=idempotency_key,
                            request_fingerprint=request_fingerprint,
                            created_at=now,
                            updated_at=now,
                        ),
                        ReviewTaskRecord(
                            id=review_task_id,
                            review_run_id=review_run_id,
                            execution_status=ExecutionStatus.QUEUED.value,
                            workflow_status=ExecutionStatus.QUEUED.value,
                            priority=100,
                            attempt_count=0,
                            max_attempts=3,
                            available_at=now,
                            created_at=now,
                            updated_at=now,
                        ),
                        OutboxEventRecord(
                            id=outbox_event_id,
                            event_key=f"review.requested:{review_run_id}",
                            aggregate_type="review_run",
                            aggregate_id=review_run_id,
                            event_type="review.requested",
                            payload={
                                "review_run_id": review_run_id,
                                "review_task_id": review_task_id,
                                "review_version_key": request.review_version_key,
                            },
                            occurred_at=now,
                            publish_attempts=0,
                        ),
                    ]
                )
                session.commit()
                return ReviewSubmissionResult(
                    review_run_id=review_run_id,
                    review_task_id=review_task_id,
                    review_version_key=request.review_version_key,
                    execution_status=ExecutionStatus.QUEUED,
                    accepted_at=now,
                    created=True,
                )
            except IntegrityError as exc:
                session.rollback()
                try:
                    existing = self._find_existing(session, idempotency_key)
                except SQLAlchemyError as lookup_exc:
                    raise ReviewPersistenceError(
                        "review request could not be reconciled after a conflict"
                    ) from lookup_exc
                if existing is None:
                    raise ReviewPersistenceError(
                        "review request violated a database constraint"
                    ) from exc
                return self._existing_result(existing, request_fingerprint)
            except IdempotencyConflictError:
                raise
            except ReviewQuotaExceededError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise ReviewPersistenceError(
                    "review request could not be persisted"
                ) from exc

    def _reserve_quota(
        self,
        session: Session,
        actor: str,
        repository: str,
        now: datetime,
    ) -> None:
        """用一条批量 UPSERT 原子增加账号、仓库和全局窗口计数。"""

        normalized_actor = actor.strip().casefold() or "system"
        if len(normalized_actor) > 100:
            normalized_actor = normalized_actor[:100]
        normalized_repository = repository.strip().casefold()
        windows = quota_windows(now)
        scope_limits = {
            "user": (
                self._quota_policy.user_hourly,
                self._quota_policy.user_daily,
            ),
            "repository": (
                self._quota_policy.repository_hourly,
                self._quota_policy.repository_daily,
            ),
            "global": (
                self._quota_policy.global_hourly,
                self._quota_policy.global_daily,
            ),
        }
        scope_keys = (
            ("user", normalized_actor),
            ("repository", normalized_repository),
            ("global", "all"),
        )
        values = [
            {
                "id": str(
                    uuid5(
                        NAMESPACE_URL,
                        f"openreviewer:quota:{scope}:{scope_key}:{window}:{window_start.isoformat()}",
                    )
                ),
                "scope": scope,
                "scope_key": scope_key,
                "window": window,
                "window_start": window_start,
                "request_count": 1,
                "updated_at": now,
            }
            for scope, scope_key in scope_keys
            for window, window_start in windows
        ]
        dialect = session.get_bind().dialect.name
        statement: Any
        if dialect == "postgresql":
            statement = postgresql_insert(ReviewQuotaBucketRecord)
        elif dialect == "sqlite":
            statement = sqlite_insert(ReviewQuotaBucketRecord)
        else:
            raise ReviewPersistenceError(
                "review quota requires PostgreSQL or SQLite"
            )
        statement = statement.values(values).on_conflict_do_update(
            index_elements=[
                ReviewQuotaBucketRecord.scope,
                ReviewQuotaBucketRecord.scope_key,
                ReviewQuotaBucketRecord.window,
                ReviewQuotaBucketRecord.window_start,
            ],
            set_={
                "request_count": ReviewQuotaBucketRecord.request_count + 1,
                "updated_at": now,
            },
        ).returning(
            ReviewQuotaBucketRecord.scope,
            ReviewQuotaBucketRecord.window,
            ReviewQuotaBucketRecord.window_start,
            ReviewQuotaBucketRecord.request_count,
        )
        bucket_rows = session.execute(statement).all()
        limits = {
            (scope, "hour"): hourly
            for scope, (hourly, _daily) in scope_limits.items()
        }
        limits.update(
            {
                (scope, "day"): daily
                for scope, (_hourly, daily) in scope_limits.items()
            }
        )
        for row in bucket_rows:
            scope = str(row.scope)
            window = str(row.window)
            count = int(row.request_count)
            limit = limits[(scope, window)]
            if count > limit:
                raise ReviewQuotaExceededError(
                    scope,
                    limit,
                    retry_after_for(window, row.window_start, now),
                )

    @staticmethod
    def _find_existing(
        session: Session,
        idempotency_key: str,
    ) -> tuple[str, datetime, str, str, str, str] | None:
        """按幂等键联表查找已有运行及其唯一任务。

        参数：
            session: 当前事务使用的 SQLAlchemy 会话；调用方负责提交或回滚。
            idempotency_key: 要查找的唯一幂等键。

        返回：
            找到时只返回组装幂等响应需要的指纹、时间、运行/任务 ID、版本键和
            状态；没有记录时返回 ``None``。任务表对 ``review_run_id`` 有唯一
            约束，因此 ``one_or_none`` 也能暴露意外重复数据。

        异常：
            SQLAlchemy 查询异常会原样向上抛出，由外层事务方法统一包装。
        """
        statement = (
            select(
                ReviewRunRecord.request_fingerprint,
                ReviewRunRecord.created_at,
                ReviewRunRecord.id,
                ReviewTaskRecord.id,
                ReviewRunRecord.review_version_key,
                ReviewRunRecord.execution_status,
            )
            .join(
                ReviewTaskRecord,
                ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
            )
            .where(ReviewRunRecord.idempotency_key == idempotency_key)
        )
        row = session.execute(statement).one_or_none()
        if row is None:
            return None
        return tuple(row)

    @staticmethod
    def _existing_result(
        existing: tuple[str, datetime, str, str, str, str],
        request_fingerprint: str,
    ) -> ReviewSubmissionResult:
        """把已存在的 ORM 记录转换成幂等接口的返回对象。

        在转换前比较请求指纹，确保同一幂等键不能被不同请求内容复用；数据库
        返回的 naive 时间会补上 UTC 时区，保证 API 输出格式稳定。

        参数：
            existing: ``_find_existing`` 返回的六个明确数据库字段。
            request_fingerprint: 本次请求按规范 JSON 计算的摘要。

        返回：
            可直接交给 API 响应模型的提交结果；状态取运行记录当前值，而不是
            强行重置为 ``queued``。

        异常：
            IdempotencyConflictError: 指纹不同，说明调用方错误复用了幂等键。
            ValueError: 数据库里的执行状态不属于领域枚举，表示数据与代码契约
            已经不一致。
        """
        (
            stored_fingerprint,
            accepted_at,
            review_run_id,
            review_task_id,
            review_version_key,
            execution_status,
        ) = existing
        if stored_fingerprint != request_fingerprint:
            raise IdempotencyConflictError(
                "idempotency key was already used for another request"
            )
        if accepted_at.tzinfo is None:
            accepted_at = accepted_at.replace(tzinfo=UTC)
        return ReviewSubmissionResult(
            review_run_id=review_run_id,
            review_task_id=review_task_id,
            review_version_key=review_version_key,
            execution_status=ExecutionStatus(execution_status),
            accepted_at=accepted_at,
            created=False,
        )
