"""SQLAlchemy implementation of the review persistence boundary."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import CoverageStatus, ExecutionStatus
from domain.models import ReviewRequest
from persistence.models import OutboxEventRecord, ReviewRunRecord, ReviewTaskRecord
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

    def create_or_get(
        self,
        request: ReviewRequest,
        idempotency_key: str,
        request_fingerprint: str,
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
                            pull_request_number=request.pull_request_number,
                            head_sha=request.head_sha,
                            execution_status=ExecutionStatus.QUEUED.value,
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
            except SQLAlchemyError as exc:
                session.rollback()
                raise ReviewPersistenceError(
                    "review request could not be persisted"
                ) from exc

    @staticmethod
    def _find_existing(
        session: Session,
        idempotency_key: str,
    ) -> tuple[ReviewRunRecord, ReviewTaskRecord] | None:
        """按幂等键联表查找已有运行及其唯一任务。

        参数：
            session: 当前事务使用的 SQLAlchemy 会话；调用方负责提交或回滚。
            idempotency_key: 要查找的唯一幂等键。

        返回：
            找到时返回 ``(ReviewRunRecord, ReviewTaskRecord)``；没有记录时返回
            ``None``。任务表对 ``review_run_id`` 有唯一约束，因此这里预期每个
            运行只有一条任务，``one_or_none`` 也能暴露意外重复数据。

        异常：
            SQLAlchemy 查询异常会原样向上抛出，由外层事务方法统一包装。
        """
        statement = (
            select(ReviewRunRecord, ReviewTaskRecord)
            .join(
                ReviewTaskRecord,
                ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
            )
            .where(ReviewRunRecord.idempotency_key == idempotency_key)
        )
        row = session.execute(statement).one_or_none()
        if row is None:
            return None
        return row[0], row[1]

    @staticmethod
    def _existing_result(
        existing: tuple[ReviewRunRecord, ReviewTaskRecord],
        request_fingerprint: str,
    ) -> ReviewSubmissionResult:
        """把已存在的 ORM 记录转换成幂等接口的返回对象。

        在转换前比较请求指纹，确保同一幂等键不能被不同请求内容复用；数据库
        返回的 naive 时间会补上 UTC 时区，保证 API 输出格式稳定。

        参数：
            existing: ``_find_existing`` 返回的运行/任务 ORM 记录二元组。
            request_fingerprint: 本次请求按规范 JSON 计算的摘要。

        返回：
            可直接交给 API 响应模型的提交结果；状态取运行记录当前值，而不是
            强行重置为 ``queued``。

        异常：
            IdempotencyConflictError: 指纹不同，说明调用方错误复用了幂等键。
            ValueError: 数据库里的执行状态不属于领域枚举，表示数据与代码契约
            已经不一致。
        """
        review_run, review_task = existing
        if review_run.request_fingerprint != request_fingerprint:
            raise IdempotencyConflictError(
                "idempotency key was already used for another request"
            )
        accepted_at = review_run.created_at
        if accepted_at.tzinfo is None:
            accepted_at = accepted_at.replace(tzinfo=UTC)
        return ReviewSubmissionResult(
            review_run_id=review_run.id,
            review_task_id=review_task.id,
            review_version_key=review_run.review_version_key,
            execution_status=ExecutionStatus(review_run.execution_status),
            accepted_at=accepted_at,
            created=False,
        )
