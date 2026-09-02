"""基于 PostgreSQL 的任务租约、恢复、重试与心跳存储。"""

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, delete, func, insert, literal, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Load, Session, sessionmaker

from domain.enums import (
    ChangedFileStatus,
    CiState,
    CoverageStatus,
    EvidenceVerificationStatus,
    ExecutionStatus,
    FindingAdjudicationStatus,
    FindingLifecycleState,
    FindingOccurrenceStatus,
    ModelBatchStatus,
    PatchState,
    PullRequestState,
    ReviewConclusion,
    ReviewFileDecision,
    WorkerStatus,
)
from domain.github import GitHubReviewContext, PullRequestFile
from domain.identifiers import build_review_version_key
from domain.model_review import (
    MaterializedFinding,
    ModelReviewInput,
    ModelReviewResult,
    materialize_findings,
)
from domain.review_planning import (
    DEFAULT_REVIEW_DOMAINS,
    RepositoryRule,
    RepositoryRulesSnapshot,
    ReviewPlan,
    ReviewUnit,
)
from domain.security import ErrorCode, SafeError, redact_sensitive
from persistence.models import (
    FindingLifecycleRecord,
    GitHubInstallationRecord,
    ModelCallRecord,
    ModelHttpCallRecord,
    ModelReviewBatchRecord,
    OutboxEventRecord,
    PullRequestCiCheckRecord,
    PullRequestFileRecord,
    PullRequestVersionRecord,
    ReviewFilePlanRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewPlanRuleRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
    ReviewUnitRecord,
    WorkerHeartbeatRecord,
)
from services.model_budget import ModelBudgetRequest, ModelBudgetReservation
from services.model_review import MAX_MODEL_REVIEW_BATCHES, ModelReviewBatch
from services.task_queue import (
    ModelBatchBusyError,
    ModelBatchLease,
    ModelBudgetExceededError,
    ModelReviewCheckpointTooLargeError,
    ModelReviewConflictError,
    ModelReviewInputError,
    ReviewPlanConflictError,
    ReviewPlanInputError,
    ReviewPlanningInput,
    ReviewTarget,
    ReviewTaskLease,
    StoredModelBatch,
    StoredModelReview,
    StoredReviewPlan,
    TaskLeaseLostError,
    TaskQueueError,
)

_MAX_TRUNCATION_CHECKPOINT_BYTES = 4 * 1024 * 1024


def _validated_truncation_checkpoint(
    checkpoint: Mapping[str, object],
) -> dict[str, object]:
    """验证并复制拆分检查点，避免把任意不可序列化对象写入 JSON 列。"""

    if not isinstance(checkpoint, Mapping):
        raise ModelReviewConflictError("截断恢复检查点格式无效")
    copied = dict(checkpoint)
    try:
        encoded = json.dumps(
            copied,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ModelReviewConflictError("截断恢复检查点不可序列化") from exc
    if len(encoded) > _MAX_TRUNCATION_CHECKPOINT_BYTES:
        raise ModelReviewCheckpointTooLargeError()
    return copied


def _as_utc(value: datetime) -> datetime:
    """把数据库时间转换为带 UTC 时区的时间。

    参数：
        value: SQLAlchemy 返回的时间；不同驱动可能返回带时区或不带时区的对象。

    返回：
        带 ``UTC`` 的时间。无时区值按项目约定解释为 UTC；已有其他时区的值会
        换算到同一绝对时刻，而不是简单替换时区标签。

    统一时间后，租约过期和心跳新鲜度比较就不会因为 PostgreSQL/SQLite 驱动差异
    触发 naive/aware ``datetime`` 的运行时异常。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _latest_summary_failed(session: Session, review_run_id: str) -> bool:
    """读取当前运行最近一次汇总终态，判断是否需要强制汇总重试。

    汇总事件数量受详情读取上限约束；这里按时间和自增 ID 取一条，查询始终为
    O(1)。``summary_skipped`` 会覆盖旧的失败事件，避免普通部分重试误触发
    汇总模型。
    """

    row = session.execute(
        select(OutboxEventRecord.event_type, OutboxEventRecord.payload)
        .where(
            OutboxEventRecord.aggregate_type == "review_run",
            OutboxEventRecord.aggregate_id == review_run_id,
            OutboxEventRecord.event_type.in_(
                (
                    "review.model.summary_completed",
                    "review.model.summary_skipped",
                )
            ),
        )
        .order_by(
            OutboxEventRecord.occurred_at.desc(),
            OutboxEventRecord.id.desc(),
        )
        .limit(1)
    ).one_or_none()
    if row is None:
        return False
    event_type, payload = row
    return (
        event_type == "review.model.summary_completed"
        and isinstance(payload, dict)
        and payload.get("agent_status") == "failed"
    )


def _task_run_mutation_load_options() -> tuple[Load, Load]:
    """只加载任务和运行状态转换代码实际读取的列。"""

    return (
        Load(ReviewTaskRecord).load_only(
            ReviewTaskRecord.id,
            ReviewTaskRecord.review_run_id,
            ReviewTaskRecord.execution_status,
            ReviewTaskRecord.workflow_status,
            ReviewTaskRecord.workflow_paused_from,
            ReviewTaskRecord.attempt_count,
            ReviewTaskRecord.model_attempt_count,
            ReviewTaskRecord.max_attempts,
            ReviewTaskRecord.claimed_from_status,
            ReviewTaskRecord.ci_poll_count,
            ReviewTaskRecord.ci_wait_started_at,
            ReviewTaskRecord.ci_deadline_at,
            raiseload=True,
        ),
        Load(ReviewRunRecord).load_only(
            ReviewRunRecord.id,
            ReviewRunRecord.review_version_key,
            ReviewRunRecord.installation_id,
            ReviewRunRecord.repository_id,
            ReviewRunRecord.repository,
            ReviewRunRecord.pull_request_number,
            ReviewRunRecord.head_sha,
            ReviewRunRecord.execution_status,
            ReviewRunRecord.workflow_status,
            ReviewRunRecord.workflow_paused_from,
            ReviewRunRecord.coverage_status,
            ReviewRunRecord.created_at,
            raiseload=True,
        ),
    )


class SqlAlchemyReviewTaskQueue:
    """使用数据库行锁而非内存消息代理的持久化队列。"""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], UUID] | None = None,
        retry_base_seconds: int = 5,
        retry_cap_seconds: int = 300,
        recovery_batch_size: int = 100,
    ) -> None:
        """初始化持久化队列及重试策略。

        队列不依赖内存消息代理，而是直接使用数据库行锁；因此进程重启后任务
        仍然存在。重试延迟默认从 5 秒开始指数增长，最高不超过 300 秒。时钟和
        UUID 工厂可注入，便于测试确定性地模拟过期和检查事件内容。

        参数：
            sessions: 目标数据库的 SQLAlchemy 会话工厂。
            clock: 可选当前时间函数；所有本次操作的时间戳都从它读取。
            uuid_factory: 可选事件 ID 生成器，测试可注入固定 UUID。
            retry_base_seconds: 第一次重试的基础延迟，必须为正数。
            retry_cap_seconds: 指数退避的最大延迟，不能小于基础延迟。

        异常：
            ValueError: 两个退避参数不满足正数/上限关系。

        构造过程不创建数据库事务；会话只在具体队列操作开始时短暂打开。
        """
        if retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be positive")
        if retry_cap_seconds < retry_base_seconds:
            raise ValueError("retry_cap_seconds must not be less than the base")
        if not 1 <= recovery_batch_size <= 1000:
            raise ValueError("recovery_batch_size must be between 1 and 1000")
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory or uuid4
        self._retry_base_seconds = retry_base_seconds
        self._retry_cap_seconds = retry_cap_seconds
        self._recovery_batch_size = recovery_batch_size

    @staticmethod
    def _validate_heartbeat_identity(worker_id: str, instance_id: str | None) -> None:
        if not worker_id or len(worker_id) > 200:
            raise ValueError("worker ID must contain 1 to 200 characters")
        if instance_id is not None and (not instance_id or len(instance_id) > 64):
            raise ValueError("worker instance ID must contain 1 to 64 characters")

    def start_heartbeat(
        self,
        worker_id: str,
        instance_id: str,
    ) -> None:
        """原子接管稳定 Worker ID，并把旧进程的后续 CAS 心跳变为失败。

        每次进程启动都生成新的 ``instance_id``。已有行会在一个 UPDATE 中替换
        token、启动时间和状态；没有行时插入。并发首次启动发生唯一键竞争时，
        失败方回滚后再执行一次接管 UPDATE，因此最终只会有一个 token 生效。
        """

        self._validate_heartbeat_identity(worker_id, instance_id)
        now = self._clock()

        def takeover(session: Session) -> Any:
            return session.execute(
                update(WorkerHeartbeatRecord)
                .where(WorkerHeartbeatRecord.worker_id == worker_id)
                .values(
                    instance_id=instance_id,
                    status=WorkerStatus.STARTING.value,
                    current_task_id=None,
                    started_at=now,
                    last_seen_at=now,
                )
            )

        with self._sessions() as session:
            try:
                result = takeover(session)
                if result.rowcount == 0:
                    session.add(
                        WorkerHeartbeatRecord(
                            worker_id=worker_id,
                            instance_id=instance_id,
                            status=WorkerStatus.STARTING.value,
                            current_task_id=None,
                            started_at=now,
                            last_seen_at=now,
                        )
                    )
                session.commit()
            except IntegrityError:
                session.rollback()
                try:
                    result = takeover(session)
                    if getattr(result, "rowcount", None) != 1:
                        raise TaskQueueError("worker heartbeat could not be claimed")
                    session.commit()
                except (TaskQueueError, SQLAlchemyError) as exc:
                    session.rollback()
                    if isinstance(exc, TaskQueueError):
                        raise
                    raise TaskQueueError(
                        "worker heartbeat could not be claimed"
                    ) from exc
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("worker heartbeat could not be claimed") from exc

    def record_heartbeat(
        self,
        worker_id: str,
        worker_status: WorkerStatus,
        current_task_id: str | None = None,
        *,
        instance_id: str | None = None,
    ) -> None:
        """记录 Worker 心跳。

        第一次看到某个 Worker 时创建记录并保存启动时间；后续调用只更新状态、
        当前任务和最后心跳时间。写入失败会回滚事务并转换成队列层异常，避免
        Dashboard 把不可确认的状态当成健康状态。

        参数：
            worker_id: 稳定 Worker ID；相同 ID 会更新同一行，而不是新建心跳。
            worker_status: 当前生命周期状态。
            current_task_id: ``busy`` 时正在处理的任务 ID；空闲、启动或停止时
                通常传 ``None``。
            instance_id: 进程启动时接管得到的 token。提供时使用条件 UPDATE，旧
                进程 token 不匹配会失败，不能覆盖新进程状态。省略只用于旧调用方。

        异常：
            TaskQueueError: 查询、插入、更新或提交失败。事务会先回滚，调用方
            不应把失败的心跳当成在线信号。

        新 Worker 进程应先调用 :meth:`start_heartbeat`；该操作会重置
        ``started_at``。不带 token 的兼容调用不会覆盖已经被新进程接管的行。
        """
        self._validate_heartbeat_identity(worker_id, instance_id)
        now = self._clock()
        with self._sessions() as session:
            try:
                if instance_id is not None:
                    result = session.execute(
                        update(WorkerHeartbeatRecord)
                        .where(
                            WorkerHeartbeatRecord.worker_id == worker_id,
                            WorkerHeartbeatRecord.instance_id == instance_id,
                        )
                        .values(
                            status=worker_status.value,
                            current_task_id=current_task_id,
                            last_seen_at=now,
                        )
                    )
                    if getattr(result, "rowcount", None) != 1:
                        # 新进程接管同一稳定 Worker ID 后，旧进程的 token
                        # 永远不会恢复；把它标记为租约丢失，让后台心跳线程
                        # 立即退出，避免旧实例持续轮询数据库。
                        raise TaskLeaseLostError(
                            "worker heartbeat ownership was lost"
                        )
                    session.commit()
                    return
                heartbeat = session.get(WorkerHeartbeatRecord, worker_id)
                if heartbeat is None:
                    heartbeat = WorkerHeartbeatRecord(
                        worker_id=worker_id,
                        instance_id=None,
                        status=worker_status.value,
                        current_task_id=current_task_id,
                        started_at=now,
                        last_seen_at=now,
                    )
                    session.add(heartbeat)
                else:
                    if heartbeat.instance_id is not None:
                        raise TaskLeaseLostError(
                            "worker heartbeat ownership was lost"
                        )
                    heartbeat.status = worker_status.value
                    heartbeat.current_task_id = current_task_id
                    heartbeat.last_seen_at = now
                session.commit()
            except TaskQueueError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("worker heartbeat could not be persisted") from exc

    def recover_expired_leases(self) -> int:
        """恢复一批已过期的运行中任务租约。

        查询使用 ``FOR UPDATE SKIP LOCKED``，多个 Worker 并行恢复时不会互相等待
        或重复处理同一任务。每个任务根据剩余尝试次数回到 ``queued`` 并设置退避，
        或进入 ``failed``；状态变化和 Outbox 事件在同一事务中提交。

        返回：
            本次事务成功处理的过期任务数量。

        异常：
            TaskQueueError: 查询、状态转换或事务提交失败；整个批次会回滚，避免
            只恢复一部分任务。

        任务状态筛选只接受 ``running`` 且 ``lease_expires_at <= now`` 的行，
        每批最多处理配置的 ``recovery_batch_size`` 条。任务与关联运行通过同一
        JOIN 读取；如果运行缺失，该任务不会被误当成可恢复记录。
        """
        now = self._clock()
        with self._sessions() as session:
            try:
                statement = (
                    select(ReviewTaskRecord, ReviewRunRecord)
                    .join(
                        ReviewRunRecord,
                        ReviewRunRecord.id == ReviewTaskRecord.review_run_id,
                    )
                    .where(
                        ReviewTaskRecord.execution_status
                        == ExecutionStatus.RUNNING.value,
                        or_(
                            ReviewTaskRecord.workflow_status.is_(None),
                            ReviewTaskRecord.workflow_status
                            != ExecutionStatus.PAUSED.value,
                        ),
                        or_(
                            ReviewRunRecord.workflow_status.is_(None),
                            ReviewRunRecord.workflow_status
                            != ExecutionStatus.PAUSED.value,
                        ),
                        ReviewTaskRecord.lease_expires_at.is_not(None),
                        ReviewTaskRecord.lease_expires_at <= now,
                    )
                    .order_by(ReviewTaskRecord.lease_expires_at.asc())
                    .limit(self._recovery_batch_size)
                    .options(*_task_run_mutation_load_options())
                    .with_for_update(skip_locked=True)
                )
                expired_tasks = list(session.execute(statement))
                lease_error = SafeError(
                    code=ErrorCode.TASK_LEASE_EXPIRED,
                    safe_message="Worker 租约超时，任务已进入恢复流程",
                    retryable=True,
                )
                for task, run in expired_tasks:
                    is_model_stage = (
                        task.claimed_from_status
                        == ExecutionStatus.READY_FOR_REVIEW.value
                        and task.model_attempt_count > 0
                    )
                    self._reschedule_or_fail(
                        session,
                        task,
                        run,
                        now,
                        lease_error,
                        event_suffix=(
                            f"lease-expired-model-{task.model_attempt_count}"
                            if is_model_stage
                            else f"lease-expired-{task.attempt_count}"
                        ),
                    )
                session.commit()
                return len(expired_tasks)
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("expired task leases could not be recovered") from exc

    def claim_next(
        self,
        worker_id: str,
        lease_duration: timedelta,
        *,
        ai_configured: bool = True,
    ) -> ReviewTaskLease | None:
        """原子领取一个当前可执行的任务。

        选择 ``queued``、到期的 ``waiting_for_ci``，或尚未保存计划的
        ``ready_for_review`` 任务，并按优先级、可用时间和创建时间排序。锁定后
        同时更新任务和运行状态、原阶段、尝试次数、租约所有者及过期时间，再写入
        ``review.task.running`` 事件；没有可领取任务时返回 ``None``。

        参数：
            worker_id: 领取者的稳定身份，会写入 ``lease_owner``。
            lease_duration: 从当前时钟到租约到期的时长，必须大于零。

        返回：
            成功时返回包含数据库主键、运行 ID、尝试次数和到期时间的租约快照；
            没有合资格任务时返回 ``None``，此时只提交一个空事务并释放锁。

        异常：
            ValueError: 租约时长不大于零。
            TaskQueueError: 关联运行不存在、数据库锁定/更新/提交失败。

        新任务或错误重试会增加 ``attempt_count``；正常 CI 轮询只增加
        ``ci_poll_count``，不会因为等待外部流水线而耗尽错误重试次数。
        """
        if lease_duration.total_seconds() <= 0:
            raise ValueError("lease_duration must be positive")
        now = self._clock()
        lease_expires_at = now + lease_duration
        with self._sessions() as session:
            try:
                plan_id_query = (
                    select(ReviewPlanRecord.id)
                    .where(ReviewPlanRecord.review_run_id == ReviewRunRecord.id)
                    .scalar_subquery()
                )
                model_completed_query = (
                    select(ReviewPlanRecord.model_review_completed_at)
                    .where(ReviewPlanRecord.review_run_id == ReviewRunRecord.id)
                    .scalar_subquery()
                )
                statement = (
                    select(
                        ReviewTaskRecord,
                        ReviewRunRecord,
                        plan_id_query.label("review_plan_id"),
                    )
                    .join(
                        ReviewRunRecord,
                        ReviewRunRecord.id == ReviewTaskRecord.review_run_id,
                    )
                    .where(
                        ReviewTaskRecord.execution_status.in_(
                            (
                                ExecutionStatus.QUEUED.value,
                                ExecutionStatus.WAITING_FOR_CI.value,
                                ExecutionStatus.READY_FOR_REVIEW.value,
                            )
                        ),
                        or_(
                            ReviewTaskRecord.workflow_status.is_(None),
                            ReviewTaskRecord.workflow_status
                            != ExecutionStatus.PAUSED.value,
                        ),
                        or_(
                            ReviewRunRecord.workflow_status.is_(None),
                            ReviewRunRecord.workflow_status
                            != ExecutionStatus.PAUSED.value,
                        ),
                        ReviewTaskRecord.available_at <= now,
                        or_(
                            literal(ai_configured),
                            ReviewTaskRecord.execution_status
                            != ExecutionStatus.READY_FOR_REVIEW.value,
                        ),
                        or_(
                            ReviewTaskRecord.execution_status
                            != ExecutionStatus.READY_FOR_REVIEW.value,
                            plan_id_query.is_(None),
                            model_completed_query.is_(None),
                        ),
                    )
                    .order_by(
                        ReviewTaskRecord.priority.desc(),
                        ReviewTaskRecord.available_at.asc(),
                        ReviewTaskRecord.created_at.asc(),
                    )
                    .limit(1)
                    .options(*_task_run_mutation_load_options())
                    .with_for_update(skip_locked=True)
                )
                # 行锁保证多个 Worker 同时轮询时，只有一个能拿到这条任务。
                row = session.execute(statement).one_or_none()
                if row is None:
                    session.commit()
                    return None
                task, run, review_plan_id = row

                claimed_from_status = ExecutionStatus(task.execution_status)
                task.execution_status = ExecutionStatus.RUNNING.value
                is_model_stage = (
                    claimed_from_status is ExecutionStatus.READY_FOR_REVIEW
                    and review_plan_id is not None
                )
                force_summary = (
                    _latest_summary_failed(session, task.review_run_id)
                    if is_model_stage
                    else False
                )
                if is_model_stage:
                    task.model_attempt_count += 1
                elif claimed_from_status in {
                    ExecutionStatus.QUEUED,
                    ExecutionStatus.READY_FOR_REVIEW,
                }:
                    task.attempt_count += 1
                else:
                    task.ci_poll_count += 1
                task.claimed_from_status = claimed_from_status.value
                task.lease_owner = worker_id
                task.lease_expires_at = lease_expires_at
                task.last_error = None
                task.last_error_code = None
                task.last_error_retryable = None
                task.last_error_details = None
                task.updated_at = now
                run.execution_status = ExecutionStatus.RUNNING.value
                run.updated_at = now
                self._add_event(
                    session,
                    task,
                    "review.task.running",
                    (
                        f"model-review:{task.model_attempt_count}"
                        if is_model_stage
                        else f"{claimed_from_status.value}:{task.attempt_count}:"
                        f"ci-poll-{task.ci_poll_count}"
                    ),
                    now,
                )
                session.commit()
                return ReviewTaskLease(
                    task_id=task.id,
                    review_run_id=task.review_run_id,
                    worker_id=worker_id,
                    attempt_count=task.attempt_count,
                    model_attempt_count=task.model_attempt_count,
                    lease_expires_at=lease_expires_at,
                    ci_poll_count=task.ci_poll_count,
                    claimed_from_status=claimed_from_status,
                    review_plan_id=review_plan_id,
                    force_summary=force_summary,
                )
            except TaskQueueError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("the next review task could not be claimed") from exc

    def renew_lease(
        self,
        lease: ReviewTaskLease,
        lease_duration: timedelta,
    ) -> ReviewTaskLease:
        """延长当前租约并返回新的租约对象。

        更新前会再次按任务 ID、运行 ID、Worker ID、尝试次数和未过期条件加锁
        校验。任何一项不匹配都意味着旧 Worker 已失去所有权，此时抛出
        ``TaskLeaseLostError``，阻止过期 Worker 覆盖新 Worker 的状态。

        参数：
            lease: 之前领取任务时保存的所有权快照。
            lease_duration: 从本次续租时刻重新计算的有效期，必须大于零。

        返回：
            与旧租约身份相同、``lease_expires_at`` 更新后的新快照。

        异常：
            ValueError: 续租时长不大于零。
            TaskLeaseLostError: 任务已被恢复/重新领取，或租约已过期。
            TaskQueueError: 数据库更新或提交失败。

        更新使用行锁并在事务中提交；失败时回滚，所以不会留下“内存认为续租成功、
        数据库仍是旧到期时间”的半完成状态。
        """
        if lease_duration.total_seconds() <= 0:
            raise ValueError("lease_duration must be positive")
        now = self._clock()
        renewed_until = now + lease_duration
        with self._sessions() as session:
            try:
                task = self._locked_owned_task(session, lease, now)
                task.lease_expires_at = renewed_until
                task.updated_at = now
                session.commit()
                return ReviewTaskLease(
                    task_id=lease.task_id,
                    review_run_id=lease.review_run_id,
                    worker_id=lease.worker_id,
                    attempt_count=lease.attempt_count,
                    model_attempt_count=lease.model_attempt_count,
                    lease_expires_at=renewed_until,
                    ci_poll_count=lease.ci_poll_count,
                    claimed_from_status=lease.claimed_from_status,
                    review_plan_id=lease.review_plan_id,
                    force_summary=lease.force_summary,
                )
            except TaskLeaseLostError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("the review task lease could not be renewed") from exc

    def load_target(self, lease: ReviewTaskLease) -> ReviewTarget:
        """用一次有索引 JOIN 读取任务目标，不在外部请求期间持有事务。"""

        now = self._clock()
        context_fetched_at = (
            select(PullRequestVersionRecord.context_fetched_at)
            .where(
                PullRequestVersionRecord.review_version_key
                == ReviewRunRecord.review_version_key
            )
            .correlate(ReviewRunRecord)
            .scalar_subquery()
            .label("context_fetched_at")
        )
        statement = (
            select(
                ReviewRunRecord.installation_id,
                ReviewRunRecord.repository_id,
                ReviewRunRecord.repository,
                ReviewRunRecord.pull_request_number,
                ReviewRunRecord.head_sha,
                ReviewRunRecord.review_version_key,
                context_fetched_at,
            )
            .join(
                ReviewTaskRecord,
                ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
            )
            .where(
                ReviewTaskRecord.id == lease.task_id,
                ReviewTaskRecord.review_run_id == lease.review_run_id,
                ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
                ReviewTaskRecord.lease_owner == lease.worker_id,
                ReviewTaskRecord.attempt_count == lease.attempt_count,
                ReviewTaskRecord.model_attempt_count == lease.model_attempt_count,
                ReviewTaskRecord.ci_poll_count == lease.ci_poll_count,
                    ReviewTaskRecord.claimed_from_status
                == lease.claimed_from_status.value,
                or_(
                    ReviewTaskRecord.workflow_status.is_(None),
                    ReviewTaskRecord.workflow_status
                    != ExecutionStatus.PAUSED.value,
                ),
                or_(
                    ReviewRunRecord.workflow_status.is_(None),
                    ReviewRunRecord.workflow_status
                    != ExecutionStatus.PAUSED.value,
                ),
                ReviewRunRecord.execution_status == ExecutionStatus.RUNNING.value,
                ReviewTaskRecord.lease_expires_at.is_not(None),
                ReviewTaskRecord.lease_expires_at > now,
            )
        )
        with self._sessions() as session:
            try:
                row = session.execute(statement).one_or_none()
                if row is None:
                    raise TaskLeaseLostError()
                return ReviewTarget(
                    installation_id=row.installation_id,
                    repository_id=row.repository_id,
                    repository=row.repository,
                    pull_request_number=row.pull_request_number,
                    head_sha=row.head_sha,
                    review_version_key=row.review_version_key,
                    context_fetched_at=(
                        _as_utc(row.context_fetched_at)
                        if row.context_fetched_at is not None
                        else None
                    ),
                )
            except TaskLeaseLostError:
                raise
            except SQLAlchemyError as exc:
                raise TaskQueueError("the review target could not be loaded") from exc

    def store_github_context(
        self,
        lease: ReviewTaskLease,
        context: GitHubReviewContext,
        *,
        ci_poll_interval: timedelta,
        ci_wait_timeout: timedelta,
    ) -> ExecutionStatus:
        """保存有界快照并按当前 PR/CI 状态原子推进任务。"""

        if lease.claimed_from_status not in {
            ExecutionStatus.QUEUED,
            ExecutionStatus.WAITING_FOR_CI,
        }:
            raise TaskLeaseLostError("当前租约不属于 GitHub 上下文阶段")
        if ci_poll_interval.total_seconds() <= 0:
            raise ValueError("CI poll interval must be positive")
        if ci_wait_timeout <= ci_poll_interval:
            raise ValueError("CI wait timeout must exceed the poll interval")
        now = self._clock()
        with self._sessions() as session:
            try:
                task, run = self._locked_owned_task_with_run(session, lease, now)
                pull_request = context.pull_request
                if (
                    pull_request.repository_id != run.repository_id
                    or pull_request.repository != run.repository
                    or pull_request.pull_request_number
                    != run.pull_request_number
                ):
                    raise TaskQueueError("GitHub PR identity does not match the review task")
                if pull_request.head_sha != run.head_sha:
                    self._set_owned_status(
                        task,
                        run,
                        ExecutionStatus.SUPERSEDED,
                        now,
                    )
                    self._set_workflow_status(
                        task,
                        run,
                        ExecutionStatus.SUPERSEDED,
                        now,
                    )
                    run.publish_attempt_token = None
                    run.coverage_status = CoverageStatus.STALE.value
                    self._add_event(
                        session,
                        task,
                        "review.superseded",
                        f"head-mismatch:{task.attempt_count}:ci-poll-{task.ci_poll_count}",
                        now,
                    )
                    session.commit()
                    return ExecutionStatus.SUPERSEDED

                version = self._get_or_create_version(session, run, now)
                self._update_pull_request_snapshot(version, context, now)
                if (
                    pull_request.state is PullRequestState.CLOSED
                    or pull_request.draft
                ):
                    self._set_owned_status(
                        task,
                        run,
                        ExecutionStatus.CANCELLED,
                        now,
                    )
                    self._set_workflow_status(
                        task,
                        run,
                        ExecutionStatus.CANCELLED,
                        now,
                    )
                    run.publish_attempt_token = None
                    self._add_event(
                        session,
                        task,
                        "review.cancelled",
                        f"not-reviewable:{task.attempt_count}:ci-poll-{task.ci_poll_count}",
                        now,
                    )
                    session.commit()
                    return ExecutionStatus.CANCELLED

                if context.files is not None:
                    self._replace_files(session, version.id, context, now)
                    version.files_complete = context.files_complete
                    version.diff_complete = context.diff_complete
                    version.context_fetched_at = now
                if context.ci is None or context.ci.head_sha != run.head_sha:
                    raise TaskQueueError("GitHub CI snapshot is missing or stale")
                self._replace_ci_checks(session, version.id, context, now)
                version.ci_state = context.ci.state.value
                version.ci_checks_complete = context.ci.complete
                version.ci_checked_at = context.ci.checked_at

                superseded_count = self._supersede_previous_versions(
                    session,
                    run,
                    now,
                )
                if superseded_count:
                    self._add_event(
                        session,
                        task,
                        "review.previous_versions_superseded",
                        f"{task.attempt_count}:ci-poll-{task.ci_poll_count}",
                        now,
                        extra_payload={"superseded_count": superseded_count},
                    )

                if context.ci.state in {CiState.PENDING, CiState.UNKNOWN}:
                    wait_started_at = task.ci_wait_started_at or now
                    deadline = task.ci_deadline_at or (
                        wait_started_at + ci_wait_timeout
                    )
                    task.ci_wait_started_at = wait_started_at
                    task.ci_deadline_at = deadline
                    if _as_utc(deadline) <= _as_utc(now):
                        timeout_error = SafeError(
                            code=ErrorCode.CI_WAIT_TIMEOUT,
                            safe_message="等待 GitHub CI 完成已超时",
                            retryable=False,
                            details={"ci_state": context.ci.state.value},
                        )
                        task.last_error = timeout_error.safe_message
                        task.last_error_code = timeout_error.code.value
                        task.last_error_retryable = timeout_error.retryable
                        task.last_error_details = dict(timeout_error.details)
                        self._set_owned_status(
                            task,
                            run,
                            ExecutionStatus.TIMED_OUT,
                            now,
                        )
                        self._set_workflow_status(
                            task,
                            run,
                            ExecutionStatus.FAILED,
                            now,
                        )
                        self._add_event(
                            session,
                            task,
                            "review.ci_timed_out",
                            f"{task.attempt_count}:ci-poll-{task.ci_poll_count}",
                            now,
                            error=timeout_error,
                        )
                        next_status = ExecutionStatus.TIMED_OUT
                    else:
                        self._set_owned_status(
                            task,
                            run,
                            ExecutionStatus.WAITING_FOR_CI,
                            now,
                        )
                        self._set_workflow_status(
                            task,
                            run,
                            ExecutionStatus.CI,
                            now,
                        )
                        task.available_at = now + ci_poll_interval
                        self._add_event(
                            session,
                            task,
                            "review.waiting_for_ci",
                            f"{task.attempt_count}:ci-poll-{task.ci_poll_count}",
                            now,
                            extra_payload={"ci_state": context.ci.state.value},
                        )
                        next_status = ExecutionStatus.WAITING_FOR_CI
                else:
                    # 终态（包括明确的“未配置 CI”）不应残留上一轮等待期限，
                    # 否则后续重试可能被旧 deadline 误判为超时。
                    task.ci_wait_started_at = None
                    task.ci_deadline_at = None
                    self._set_owned_status(
                        task,
                        run,
                        ExecutionStatus.READY_FOR_REVIEW,
                        now,
                    )
                    self._set_workflow_status(
                        task,
                        run,
                        ExecutionStatus.PLANNING,
                        now,
                    )
                    self._add_event(
                        session,
                        task,
                        "review.ready_for_review",
                        f"{task.attempt_count}:ci-poll-{task.ci_poll_count}",
                        now,
                        extra_payload={"ci_state": context.ci.state.value},
                    )
                    next_status = ExecutionStatus.READY_FOR_REVIEW
                session.commit()
                return next_status
            except (TaskLeaseLostError, TaskQueueError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("GitHub review context could not be stored") from exc

    def load_planning_input(self, lease: ReviewTaskLease) -> ReviewPlanningInput:
        """一次读取精确版本及其最多 3000 个 changed files。"""

        if lease.claimed_from_status is not ExecutionStatus.READY_FOR_REVIEW:
            raise ReviewPlanInputError("只有可审查阶段的任务才能读取规划输入")
        now = self._clock()
        statement = (
            select(
                ReviewRunRecord.installation_id.label("installation_id"),
                ReviewRunRecord.repository_id.label("repository_id"),
                ReviewRunRecord.repository.label("repository"),
                ReviewRunRecord.pull_request_number.label("pull_request_number"),
                ReviewRunRecord.head_sha.label("run_head_sha"),
                ReviewRunRecord.review_version_key.label("review_version_key"),
                PullRequestVersionRecord.id.label("version_id"),
                PullRequestVersionRecord.head_sha.label("version_head_sha"),
                PullRequestVersionRecord.context_fetched_at.label(
                    "context_fetched_at"
                ),
                PullRequestVersionRecord.files_complete.label("files_complete"),
                PullRequestVersionRecord.changed_files_count.label(
                    "changed_files_count"
                ),
                PullRequestFileRecord.id.label("file_id"),
                PullRequestFileRecord.path.label("file_path"),
                PullRequestFileRecord.previous_path.label("previous_path"),
                PullRequestFileRecord.status.label("file_status"),
                PullRequestFileRecord.blob_sha.label("blob_sha"),
                PullRequestFileRecord.additions.label("additions"),
                PullRequestFileRecord.deletions.label("deletions"),
                PullRequestFileRecord.changes.label("changes"),
                PullRequestFileRecord.patch_state.label("patch_state"),
                PullRequestFileRecord.patch.label("patch"),
            )
            .select_from(ReviewTaskRecord)
            .join(
                ReviewRunRecord,
                ReviewRunRecord.id == ReviewTaskRecord.review_run_id,
            )
            .join(
                PullRequestVersionRecord,
                PullRequestVersionRecord.review_version_key
                == ReviewRunRecord.review_version_key,
            )
            .outerjoin(
                PullRequestFileRecord,
                PullRequestFileRecord.pull_request_version_id
                == PullRequestVersionRecord.id,
            )
            .where(
                ReviewTaskRecord.id == lease.task_id,
                ReviewTaskRecord.review_run_id == lease.review_run_id,
                ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
                ReviewTaskRecord.lease_owner == lease.worker_id,
                ReviewTaskRecord.attempt_count == lease.attempt_count,
                ReviewTaskRecord.model_attempt_count == lease.model_attempt_count,
                ReviewTaskRecord.ci_poll_count == lease.ci_poll_count,
                ReviewTaskRecord.claimed_from_status
                == ExecutionStatus.READY_FOR_REVIEW.value,
                ReviewRunRecord.execution_status == ExecutionStatus.RUNNING.value,
                ReviewTaskRecord.lease_expires_at.is_not(None),
                ReviewTaskRecord.lease_expires_at > now,
            )
            .order_by(PullRequestFileRecord.path.asc())
            .limit(3001)
        )
        with self._sessions() as session:
            try:
                rows = session.execute(statement).all()
            except SQLAlchemyError as exc:
                raise TaskQueueError("review planning input could not be loaded") from exc
        if not rows:
            raise TaskLeaseLostError()

        first = rows[0]
        if (
            first.run_head_sha != first.version_head_sha
            or first.review_version_key
            != build_review_version_key(
                first.repository_id,
                first.pull_request_number,
                first.run_head_sha,
            )
        ):
            raise ReviewPlanConflictError(
                "持久化 PR 版本与当前审查任务身份不一致"
            )
        if first.context_fetched_at is None or first.files_complete is not True:
            raise ReviewPlanInputError()
        if (
            first.changed_files_count is None
            or first.changed_files_count < 0
            or first.changed_files_count > 3000
        ):
            raise ReviewPlanInputError("PR changed files 数量超出规划边界")

        file_rows = [row for row in rows if row.file_id is not None]
        if len(file_rows) > 3000 or len(file_rows) != first.changed_files_count:
            raise ReviewPlanInputError("PR 文件快照数量与 GitHub 元数据不一致")
        try:
            files = tuple(
                PullRequestFile(
                    path=row.file_path,
                    previous_path=row.previous_path,
                    status=ChangedFileStatus(row.file_status),
                    blob_sha=row.blob_sha,
                    additions=row.additions,
                    deletions=row.deletions,
                    changes=row.changes,
                    patch_state=PatchState(row.patch_state),
                    patch=row.patch,
                )
                for row in file_rows
            )
        except (TypeError, ValueError) as exc:
            raise ReviewPlanInputError("PR 文件快照字段不符合规划契约") from exc
        return ReviewPlanningInput(
            target=ReviewTarget(
                installation_id=first.installation_id,
                repository_id=first.repository_id,
                repository=first.repository,
                pull_request_number=first.pull_request_number,
                head_sha=first.run_head_sha,
                review_version_key=first.review_version_key,
                context_fetched_at=_as_utc(first.context_fetched_at),
            ),
            files=files,
        )

    def store_review_plan(
        self,
        lease: ReviewTaskLease,
        rules: RepositoryRulesSnapshot,
        plan: ReviewPlan,
    ) -> StoredReviewPlan:
        """短事务校验版本并批量保存完整 Review Plan。"""

        if lease.claimed_from_status is not ExecutionStatus.READY_FOR_REVIEW:
            raise ReviewPlanConflictError("计划只能由可审查阶段的租约保存")
        if (
            rules.repository_id != plan.repository_id
            or rules.repository != plan.repository
            or rules.head_sha != plan.head_sha
            or tuple(rules.rules) != tuple(plan.rules)
        ):
            raise ReviewPlanConflictError("规则快照与 Review Plan 身份不一致")

        now = self._clock()
        with self._sessions() as session:
            try:
                existing = session.execute(
                    select(
                        ReviewPlanRecord.id,
                        ReviewPlanRecord.plan_fingerprint,
                        ReviewPlanRecord.review_version_key,
                        ReviewPlanRecord.head_sha,
                        ReviewRunRecord.execution_status,
                    )
                    .join(
                        ReviewRunRecord,
                        ReviewRunRecord.id == ReviewPlanRecord.review_run_id,
                    )
                    .where(ReviewPlanRecord.review_run_id == lease.review_run_id)
                ).one_or_none()
                if existing is not None:
                    if (
                        existing.plan_fingerprint != plan.plan_fingerprint
                        or existing.review_version_key != plan.review_version_key
                        or existing.head_sha != plan.head_sha
                    ):
                        raise ReviewPlanConflictError(
                            "同一审查运行已经保存了不同指纹的计划"
                        )
                    return StoredReviewPlan(
                        plan_id=existing.id,
                        created=False,
                        execution_status=ExecutionStatus(
                            existing.execution_status
                        ),
                    )

                # 只有“已经存在且指纹完全一致”的只读幂等重放允许使用已失效
                # 租约；任何新建或冲突路径都必须在下面重新校验当前所有权。
                task, run = self._locked_owned_task_with_run(session, lease, now)
                if (
                    run.review_version_key != plan.review_version_key
                    or run.repository_id != plan.repository_id
                    or run.repository != plan.repository
                    or run.pull_request_number != plan.pull_request_number
                    or run.head_sha != plan.head_sha
                ):
                    raise ReviewPlanConflictError(
                        "Review Plan 与被锁定任务的精确版本不一致"
                    )

                version = session.scalar(
                    select(PullRequestVersionRecord)
                    .where(
                        PullRequestVersionRecord.review_version_key
                        == run.review_version_key
                    )
                    .options(
                        Load(PullRequestVersionRecord).load_only(
                            PullRequestVersionRecord.id,
                            PullRequestVersionRecord.review_version_key,
                            PullRequestVersionRecord.repository_id,
                            PullRequestVersionRecord.repository,
                            PullRequestVersionRecord.pull_request_number,
                            PullRequestVersionRecord.head_sha,
                            PullRequestVersionRecord.files_complete,
                            PullRequestVersionRecord.changed_files_count,
                            raiseload=True,
                        )
                    )
                    .with_for_update()
                )
                if version is None:
                    raise ReviewPlanInputError("当前 SHA 没有持久化 PR 版本")
                if (
                    version.review_version_key != plan.review_version_key
                    or version.repository_id != plan.repository_id
                    or version.repository != plan.repository
                    or version.pull_request_number != plan.pull_request_number
                    or version.head_sha != plan.head_sha
                ):
                    raise ReviewPlanConflictError(
                        "当前 PR 版本已经不再匹配 Review Plan"
                    )
                if version.files_complete is not True:
                    raise ReviewPlanInputError()
                if version.changed_files_count != len(plan.files):
                    raise ReviewPlanInputError(
                        "Review Plan 文件数与当前 SHA 快照不一致"
                    )

                newer_run_id = session.scalar(
                    select(ReviewRunRecord.id)
                    .where(
                        ReviewRunRecord.repository_id == run.repository_id,
                        ReviewRunRecord.pull_request_number
                        == run.pull_request_number,
                        ReviewRunRecord.id != run.id,
                        ReviewRunRecord.head_sha != run.head_sha,
                        ReviewRunRecord.created_at >= run.created_at,
                    )
                    .order_by(ReviewRunRecord.created_at.desc())
                    .limit(1)
                )
                if newer_run_id is not None:
                    self._set_owned_status(
                        task,
                        run,
                        ExecutionStatus.SUPERSEDED,
                        now,
                    )
                    self._set_workflow_status(
                        task,
                        run,
                        ExecutionStatus.SUPERSEDED,
                        now,
                    )
                    run.publish_attempt_token = None
                    run.coverage_status = CoverageStatus.STALE.value
                    self._add_event(
                        session,
                        task,
                        "review.superseded",
                        f"plan-head-stale:{task.attempt_count}",
                        now,
                    )
                    session.commit()
                    return StoredReviewPlan(
                        plan_id=None,
                        created=False,
                        execution_status=ExecutionStatus.SUPERSEDED,
                    )

                plan_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        "openreviewer:plan:"
                        f"{run.id}:{plan.plan_fingerprint}",
                    )
                )
                session.add(
                    ReviewPlanRecord(
                        id=plan_id,
                        review_run_id=run.id,
                        pull_request_version_id=version.id,
                        review_version_key=plan.review_version_key,
                        head_sha=plan.head_sha,
                        plan_fingerprint=plan.plan_fingerprint,
                        planner_version=plan.planner_version,
                        rules_complete=rules.complete,
                        incomplete_files=list(rules.incomplete_files),
                        rule_issues=[
                            issue.model_dump(mode="json") for issue in rules.issues
                        ],
                        candidate_count=rules.candidate_count,
                        requested_candidate_count=rules.requested_candidate_count,
                        rule_count=len(plan.rules),
                        unit_count=len(plan.units),
                        file_count=len(plan.files),
                        total_estimated_input_bytes=(
                            plan.total_estimated_input_bytes
                        ),
                        max_model_http_calls=plan.model_budget.max_http_calls,
                        max_model_input_tokens=plan.model_budget.max_input_tokens,
                        max_model_output_tokens=plan.model_budget.max_output_tokens,
                        max_model_cost_microusd=(
                            plan.model_budget.max_estimated_cost_microusd
                        ),
                        max_model_duration_seconds=(
                            plan.model_budget.max_duration_seconds
                        ),
                        model_budget_mode=plan.model_budget.enforcement,
                        model_http_calls=0,
                        model_input_tokens=0,
                        model_output_tokens=0,
                        model_estimated_cost_microusd=0,
                        created_at=now,
                    )
                )
                session.flush()

                rule_rows = [
                    {
                        "id": str(
                            uuid5(
                                NAMESPACE_URL,
                                f"openreviewer:plan-rule:{plan_id}:{rule.path}",
                            )
                        ),
                        "review_plan_id": plan_id,
                        "ordinal": ordinal,
                        "path": rule.path,
                        "scope": rule.scope,
                        "blob_sha": rule.blob_sha,
                        "content": rule.content,
                        "content_sha256": rule.content_sha256,
                        "byte_size": rule.byte_size,
                    }
                    for ordinal, rule in enumerate(plan.rules)
                ]
                if rule_rows:
                    session.execute(insert(ReviewPlanRuleRecord), rule_rows)

                unit_ids = {
                    unit.unit_key: str(
                        uuid5(
                            NAMESPACE_URL,
                            f"openreviewer:review-unit:{plan_id}:{unit.unit_key}",
                        )
                    )
                    for unit in plan.units
                }
                unit_rows = [
                    {
                        "id": unit_ids[unit.unit_key],
                        "review_plan_id": plan_id,
                        "ordinal": ordinal,
                        "unit_key": unit.unit_key,
                        "group_key": unit.group_key or unit.unit_key,
                        "file": unit.file,
                        "blob_sha": unit.blob_sha,
                        "language": unit.language,
                        "patch": unit.patch,
                        "patch_sha256": unit.patch_sha256,
                        "rule_paths": list(unit.rule_paths),
                        "review_domains": [
                            agent.value for agent in unit.review_domains
                        ],
                        "estimated_input_bytes": unit.estimated_input_bytes,
                        "planner_version": unit.planner_version,
                    }
                    for ordinal, unit in enumerate(plan.units)
                ]
                if unit_rows:
                    session.execute(insert(ReviewUnitRecord), unit_rows)

                file_rows = [
                    {
                        "id": str(
                            uuid5(
                                NAMESPACE_URL,
                                f"openreviewer:file-plan:{plan_id}:{item.file}",
                            )
                        ),
                        "review_plan_id": plan_id,
                        "review_unit_id": (
                            unit_ids[item.unit_key]
                            if item.unit_key is not None
                            else None
                        ),
                        "ordinal": ordinal,
                        "file": item.file,
                        "decision": item.decision.value,
                    }
                    for ordinal, item in enumerate(plan.files)
                ]
                if file_rows:
                    session.execute(insert(ReviewFilePlanRecord), file_rows)

                self._set_owned_status(
                    task,
                    run,
                    ExecutionStatus.READY_FOR_REVIEW,
                    now,
                )
                self._set_workflow_status(
                    task,
                    run,
                    ExecutionStatus.AGENT_BATCHES,
                    now,
                )
                self._add_event(
                    session,
                    task,
                    "review.plan.prepared",
                    plan.plan_fingerprint,
                    now,
                    extra_payload={
                        "review_plan_id": plan_id,
                        "plan_fingerprint": plan.plan_fingerprint,
                        "rule_count": len(plan.rules),
                        "unit_count": len(plan.units),
                        "file_count": len(plan.files),
                        "rules_complete": rules.complete,
                    },
                )
                session.commit()
                return StoredReviewPlan(
                    plan_id=plan_id,
                    created=True,
                    execution_status=ExecutionStatus.READY_FOR_REVIEW,
                )
            except (ReviewPlanConflictError, ReviewPlanInputError, TaskLeaseLostError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("Review Plan could not be persisted") from exc

    def load_model_review_input(self, lease: ReviewTaskLease) -> ModelReviewInput:
        """用三次有界查询读取计划元数据、规则和全部 Review Unit。"""

        if (
            lease.claimed_from_status is not ExecutionStatus.READY_FOR_REVIEW
            or lease.review_plan_id is None
        ):
            raise ModelReviewInputError("当前租约不属于模型审查阶段")
        now = self._clock()
        plan_statement = (
            select(
                ReviewPlanRecord.id.label("plan_id"),
                ReviewPlanRecord.review_run_id,
                ReviewPlanRecord.plan_fingerprint,
                ReviewPlanRecord.planner_version,
                ReviewPlanRecord.review_version_key,
                ReviewPlanRecord.head_sha.label("plan_head_sha"),
                ReviewPlanRecord.rule_count,
                ReviewPlanRecord.unit_count,
                ReviewPlanRecord.total_estimated_input_bytes,
                ReviewPlanRecord.model_review_completed_at,
                ReviewRunRecord.repository_id,
                ReviewRunRecord.repository,
                ReviewRunRecord.pull_request_number,
                ReviewRunRecord.head_sha.label("run_head_sha"),
            )
            .select_from(ReviewTaskRecord)
            .join(
                ReviewRunRecord,
                ReviewRunRecord.id == ReviewTaskRecord.review_run_id,
            )
            .join(
                ReviewPlanRecord,
                ReviewPlanRecord.review_run_id == ReviewRunRecord.id,
            )
            .where(
                ReviewTaskRecord.id == lease.task_id,
                ReviewTaskRecord.review_run_id == lease.review_run_id,
                ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
                ReviewTaskRecord.lease_owner == lease.worker_id,
                ReviewTaskRecord.attempt_count == lease.attempt_count,
                ReviewTaskRecord.model_attempt_count == lease.model_attempt_count,
                ReviewTaskRecord.ci_poll_count == lease.ci_poll_count,
                ReviewTaskRecord.claimed_from_status
                == ExecutionStatus.READY_FOR_REVIEW.value,
                ReviewRunRecord.execution_status == ExecutionStatus.RUNNING.value,
                ReviewPlanRecord.id == lease.review_plan_id,
                ReviewPlanRecord.model_review_completed_at.is_(None),
                ReviewTaskRecord.lease_expires_at.is_not(None),
                ReviewTaskRecord.lease_expires_at > now,
            )
            .limit(1)
        )
        rules_statement = (
            select(
                ReviewPlanRuleRecord.path,
                ReviewPlanRuleRecord.scope,
                ReviewPlanRuleRecord.blob_sha,
                ReviewPlanRuleRecord.content,
                ReviewPlanRuleRecord.content_sha256,
                ReviewPlanRuleRecord.byte_size,
            )
            .where(ReviewPlanRuleRecord.review_plan_id == lease.review_plan_id)
            .order_by(ReviewPlanRuleRecord.ordinal.asc())
            .limit(257)
        )
        units_statement = (
            select(
                ReviewUnitRecord.unit_key,
                ReviewUnitRecord.group_key,
                ReviewUnitRecord.file,
                ReviewUnitRecord.blob_sha,
                ReviewUnitRecord.language,
                ReviewUnitRecord.patch,
                ReviewUnitRecord.patch_sha256,
                ReviewUnitRecord.rule_paths,
                ReviewUnitRecord.review_domains,
                ReviewUnitRecord.estimated_input_bytes,
                ReviewUnitRecord.planner_version,
            )
            .where(ReviewUnitRecord.review_plan_id == lease.review_plan_id)
            .order_by(ReviewUnitRecord.ordinal.asc())
            .limit(3001)
        )
        with self._sessions() as session:
            try:
                plan_row = session.execute(plan_statement).one_or_none()
                if plan_row is None:
                    raise TaskLeaseLostError()
                rule_rows = session.execute(rules_statement).all()
                unit_rows = session.execute(units_statement).all()
            except TaskLeaseLostError:
                raise
            except SQLAlchemyError as exc:
                raise TaskQueueError("model review input could not be loaded") from exc

        if plan_row.plan_head_sha != plan_row.run_head_sha:
            raise ModelReviewConflictError("Review Plan 与运行的 head SHA 不一致")
        if len(rule_rows) > 256 or len(rule_rows) != plan_row.rule_count:
            raise ModelReviewInputError("Review Plan 规则快照数量不一致")
        if len(unit_rows) > 3000 or len(unit_rows) != plan_row.unit_count:
            raise ModelReviewInputError("Review Plan Unit 数量不一致")
        try:
            rules = tuple(
                RepositoryRule(
                    path=row.path,
                    scope=row.scope,
                    blob_sha=row.blob_sha,
                    content=row.content,
                    content_sha256=row.content_sha256,
                    byte_size=row.byte_size,
                )
                for row in rule_rows
            )
            units = tuple(
                ReviewUnit(
                    unit_key=row.unit_key,
                    group_key=row.group_key,
                    review_version_key=plan_row.review_version_key,
                    head_sha=plan_row.plan_head_sha,
                    file=row.file,
                    blob_sha=row.blob_sha,
                    language=row.language,
                    patch=row.patch,
                    patch_sha256=row.patch_sha256,
                    rule_paths=tuple(row.rule_paths),
                    review_domains=tuple(
                        row.review_domains or DEFAULT_REVIEW_DOMAINS
                    ),
                    estimated_input_bytes=row.estimated_input_bytes,
                    planner_version=row.planner_version,
                )
                for row in unit_rows
            )
            return ModelReviewInput(
                review_plan_id=plan_row.plan_id,
                review_run_id=plan_row.review_run_id,
                plan_fingerprint=plan_row.plan_fingerprint,
                planner_version=plan_row.planner_version,
                review_version_key=plan_row.review_version_key,
                repository_id=plan_row.repository_id,
                repository=plan_row.repository,
                pull_request_number=plan_row.pull_request_number,
                head_sha=plan_row.plan_head_sha,
                rules=rules,
                units=units,
                total_estimated_input_bytes=plan_row.total_estimated_input_bytes,
            )
        except (TypeError, ValueError) as exc:
            raise ModelReviewInputError(
                "持久化 Review Plan 不符合模型输入契约"
            ) from exc

    @staticmethod
    def _reconcile_finding_lifecycles(
        session: Session,
        run: ReviewRunRecord,
        findings: tuple[MaterializedFinding, ...],
        now: datetime,
        *,
        coverage_complete: bool,
    ) -> tuple[
        dict[str, tuple[FindingOccurrenceStatus, int, str | None]],
        int,
    ]:
        """批量判定当前 Finding，并在完整覆盖时消解上一轮遗留问题。"""

        fingerprints = tuple(sorted(item.finding.fingerprint for item in findings))
        predicates = [
            FindingLifecycleRecord.state == FindingLifecycleState.PRESENT.value,
            FindingLifecycleRecord.fixed_by_review_run_id == run.id,
        ]
        if fingerprints:
            predicates.append(FindingLifecycleRecord.fingerprint.in_(fingerprints))
        lifecycle_rows = list(
            session.scalars(
                select(FindingLifecycleRecord)
                .where(
                    FindingLifecycleRecord.repository_id == run.repository_id,
                    FindingLifecycleRecord.pull_request_number
                    == run.pull_request_number,
                    or_(*predicates),
                )
                .limit(401)
                .with_for_update()
            )
        )
        # 每轮最多 200 个 Finding；上一轮 present 集合也最多 200 个。超过
        # 400 说明持久化状态已违反边界，不能继续做不完整的生命周期判定。
        if len(lifecycle_rows) > 400:
            raise ModelReviewConflictError("Finding 生命周期集合超过安全上限")
        lifecycles = {row.fingerprint: row for row in lifecycle_rows}
        occurrence_by_fingerprint: dict[
            str, tuple[FindingOccurrenceStatus, int, str | None]
        ] = {}

        for fingerprint in fingerprints:
            lifecycle = lifecycles.get(fingerprint)
            if lifecycle is None:
                lifecycle = FindingLifecycleRecord(
                    repository_id=run.repository_id,
                    pull_request_number=run.pull_request_number,
                    fingerprint=fingerprint,
                    state=FindingLifecycleState.PRESENT.value,
                    first_seen_review_run_id=run.id,
                    last_seen_review_run_id=run.id,
                    previous_seen_review_run_id=None,
                    fixed_by_review_run_id=None,
                    first_seen_head_sha=run.head_sha,
                    last_seen_head_sha=run.head_sha,
                    last_occurrence_status=FindingOccurrenceStatus.NEW.value,
                    occurrence_count=1,
                    first_seen_at=now,
                    last_seen_at=now,
                    fixed_at=None,
                    historical_backfilled_at=now,
                    updated_at=now,
                )
                session.add(lifecycle)
                lifecycles[fingerprint] = lifecycle
                status = FindingOccurrenceStatus.NEW
            elif lifecycle.last_seen_review_run_id == run.id:
                # 阶段级重审会删除并重建本轮 Finding。复用已有判定，避免同一
                # review_run 被重复计数或错误标成再次出现。
                status = FindingOccurrenceStatus(lifecycle.last_occurrence_status)
                lifecycle.state = FindingLifecycleState.PRESENT.value
                lifecycle.fixed_by_review_run_id = None
                lifecycle.fixed_at = None
                lifecycle.updated_at = now
            else:
                status = (
                    FindingOccurrenceStatus.REINTRODUCED
                    if lifecycle.state == FindingLifecycleState.FIXED.value
                    else FindingOccurrenceStatus.STILL_PRESENT
                )
                lifecycle.previous_seen_review_run_id = (
                    lifecycle.last_seen_review_run_id
                )
                lifecycle.last_seen_review_run_id = run.id
                lifecycle.last_seen_head_sha = run.head_sha
                lifecycle.last_occurrence_status = status.value
                lifecycle.occurrence_count += 1
                lifecycle.last_seen_at = now
                lifecycle.state = FindingLifecycleState.PRESENT.value
                lifecycle.fixed_by_review_run_id = None
                lifecycle.fixed_at = None
                lifecycle.updated_at = now
            occurrence_by_fingerprint[fingerprint] = (
                status,
                lifecycle.occurrence_count,
                lifecycle.previous_seen_review_run_id,
            )

        if coverage_complete:
            current = set(fingerprints)
            for lifecycle in lifecycle_rows:
                if (
                    lifecycle.fingerprint not in current
                    and lifecycle.state == FindingLifecycleState.PRESENT.value
                ):
                    lifecycle.state = FindingLifecycleState.FIXED.value
                    lifecycle.fixed_by_review_run_id = run.id
                    lifecycle.fixed_at = now
                    lifecycle.updated_at = now

        fixed_count = sum(
            lifecycle.state == FindingLifecycleState.FIXED.value
            and lifecycle.fixed_by_review_run_id == run.id
            for lifecycle in lifecycles.values()
        )
        return occurrence_by_fingerprint, fixed_count

    def store_model_review(
        self,
        lease: ReviewTaskLease,
        review_input: ModelReviewInput,
        result: ModelReviewResult,
        findings: tuple[MaterializedFinding, ...],
        *,
        configuration_revision: int | None = None,
        partial: bool = False,
    ) -> StoredModelReview:
        """短事务保存模型结果。

        ``partial`` 用于固定 Agent DAG：成功节点的 Finding 先落库，但不把
        Review Plan 标记为完成。后续完整重试会复用同一 ``model_call`` 并只
        插入尚未存在的指纹，避免重复结果和重复计费。
        """

        if (
            lease.claimed_from_status is not ExecutionStatus.READY_FOR_REVIEW
            or lease.review_plan_id is None
            or lease.review_plan_id != review_input.review_plan_id
            or lease.review_run_id != review_input.review_run_id
        ):
            raise ModelReviewConflictError("模型结果不属于当前租约的 Review Plan")
        try:
            expected_findings = materialize_findings(review_input, result.output)
        except ValueError as exc:
            raise ModelReviewConflictError(
                "模型 Finding 引用了计划外的 Unit、文件或规则"
            ) from exc
        if len(findings) != len(expected_findings):
            raise ModelReviewConflictError("模型 Finding 没有按平台契约完成身份补齐")
        for actual, expected in zip(findings, expected_findings, strict=True):
            if actual.source_unit_key != expected.source_unit_key:
                raise ModelReviewConflictError(
                    "模型 Finding 没有按平台契约完成身份补齐"
                )
            actual_finding = actual.finding.model_copy(
                update={
                    "evidence_verification_status": (
                        expected.finding.evidence_verification_status
                    ),
                    "evidence_verification_reason": (
                        expected.finding.evidence_verification_reason
                    ),
                }
            )
            if actual_finding != expected.finding:
                raise ModelReviewConflictError(
                    "模型 Finding 没有按平台契约完成身份补齐"
                )
        if not review_input.units and result.status.value != "skipped":
            raise ModelReviewConflictError("空 Review Plan 不应调用模型")
        if review_input.units and result.status.value != "succeeded":
            raise ModelReviewConflictError("非空 Review Plan 缺少成功模型调用")
        if partial and not review_input.units:
            raise ModelReviewConflictError("空 Review Plan 不应保存部分结果")

        now = self._clock()
        with self._sessions() as session:
            try:
                existing = session.execute(
                    select(
                        ModelCallRecord.id,
                        ModelCallRecord.provider,
                        ModelCallRecord.model,
                        ModelCallRecord.request_fingerprint,
                        ModelCallRecord.finding_count,
                        ReviewPlanRecord.model_review_completed_at,
                        ReviewRunRecord.execution_status,
                    )
                    .join(
                        ReviewPlanRecord,
                        ReviewPlanRecord.id == ModelCallRecord.review_plan_id,
                    )
                    .join(
                        ReviewRunRecord,
                        ReviewRunRecord.id == ReviewPlanRecord.review_run_id,
                    )
                    .where(ModelCallRecord.review_plan_id == review_input.review_plan_id)
                    # 该表对 review_plan_id 有唯一约束；LIMIT 仍作为数据损坏
                    # 或旧迁移不完整时的有界保护，避免详情请求无界读取。
                    .limit(1)
                ).one_or_none()
                if existing is not None and existing.model_review_completed_at is not None:
                    if (
                        existing.provider != result.provider.value
                        or existing.model != result.model
                        or existing.request_fingerprint != result.request_fingerprint
                    ):
                        raise ModelReviewConflictError(
                            "同一 Review Plan 已保存不同模型请求"
                        )
                    return StoredModelReview(
                        model_call_id=existing.id,
                        created=False,
                        finding_count=existing.finding_count,
                        execution_status=ExecutionStatus(existing.execution_status),
                        coverage_status=CoverageStatus.COMPLETE.value,
                        partial=False,
                    )

                # 未完成的 partial 快照也必须经过当前租约所有权检查。旧实现
                # 在这里直接返回，导致调用方的任务仍停留在 RUNNING，租约无法
                # 释放，后续 Worker 只能等待超时恢复。
                task, run = self._locked_owned_task_with_run(session, lease, now)
                if existing is not None:
                    same_partial = (
                        partial
                        and existing.model_review_completed_at is None
                        and existing.request_fingerprint == result.request_fingerprint
                    )
                    if same_partial:
                        if (
                            existing.provider != result.provider.value
                            or existing.model != result.model
                        ):
                            raise ModelReviewConflictError(
                                "同一 Review Plan 已保存不同模型请求"
                            )
                        # 结果快照已经写入；本次重入不再插入 Finding，但要把
                        # 当前任务收口为可重试的 partial 状态并清除租约。
                        self._set_owned_status(task, run, ExecutionStatus.FAILED, now)
                        self._set_workflow_status(
                            task,
                            run,
                            ExecutionStatus.AGENT_BATCHES,
                            now,
                        )
                        run.coverage_status = CoverageStatus.PARTIAL.value
                        run.review_conclusion = (
                            ReviewConclusion.FINDINGS_PRESENT.value
                            if existing.finding_count
                            else ReviewConclusion.NO_CONFIRMED_FINDINGS.value
                        )
                        session.commit()
                        return StoredModelReview(
                            model_call_id=existing.id,
                            created=False,
                            finding_count=existing.finding_count,
                            execution_status=ExecutionStatus.FAILED,
                            coverage_status=CoverageStatus.PARTIAL.value,
                            partial=True,
                        )

                plan = session.scalar(
                    select(ReviewPlanRecord)
                    .where(
                        ReviewPlanRecord.id == review_input.review_plan_id,
                        ReviewPlanRecord.review_run_id == run.id,
                    )
                    .options(
                        Load(ReviewPlanRecord).load_only(
                            ReviewPlanRecord.id,
                            ReviewPlanRecord.review_run_id,
                            ReviewPlanRecord.review_version_key,
                            ReviewPlanRecord.head_sha,
                            ReviewPlanRecord.plan_fingerprint,
                            ReviewPlanRecord.rules_complete,
                            ReviewPlanRecord.model_review_completed_at,
                            raiseload=True,
                        )
                    )
                    .with_for_update()
                )
                if plan is None:
                    raise ModelReviewInputError("当前任务没有对应 Review Plan")
                if plan.model_review_completed_at is not None:
                    raise ModelReviewConflictError("Review Plan 模型阶段已经完成")
                if (
                    run.review_version_key != review_input.review_version_key
                    or run.repository_id != review_input.repository_id
                    or run.repository != review_input.repository
                    or run.pull_request_number != review_input.pull_request_number
                    or run.head_sha != review_input.head_sha
                    or plan.review_version_key != review_input.review_version_key
                    or plan.head_sha != review_input.head_sha
                    or plan.plan_fingerprint != review_input.plan_fingerprint
                ):
                    raise ModelReviewConflictError(
                        "模型结果与被锁定任务的计划身份不一致"
                    )

                newer_run_id = session.scalar(
                    select(ReviewRunRecord.id)
                    .where(
                        ReviewRunRecord.repository_id == run.repository_id,
                        ReviewRunRecord.pull_request_number
                        == run.pull_request_number,
                        ReviewRunRecord.id != run.id,
                        ReviewRunRecord.head_sha != run.head_sha,
                        ReviewRunRecord.created_at >= run.created_at,
                    )
                    .order_by(ReviewRunRecord.created_at.desc())
                    .limit(1)
                )
                if newer_run_id is not None:
                    self._set_owned_status(
                        task,
                        run,
                        ExecutionStatus.SUPERSEDED,
                        now,
                    )
                    self._set_workflow_status(
                        task,
                        run,
                        ExecutionStatus.SUPERSEDED,
                        now,
                    )
                    run.publish_attempt_token = None
                    run.coverage_status = CoverageStatus.STALE.value
                    self._add_event(
                        session,
                        task,
                        "review.superseded",
                        f"model-head-stale:{task.model_attempt_count}",
                        now,
                    )
                    session.commit()
                    return StoredModelReview(
                        model_call_id=None,
                        created=False,
                        finding_count=0,
                        execution_status=ExecutionStatus.SUPERSEDED,
                    )

                incomplete_file_count = session.scalar(
                    select(func.count())
                    .select_from(ReviewFilePlanRecord)
                    .where(
                        ReviewFilePlanRecord.review_plan_id == plan.id,
                        ReviewFilePlanRecord.decision
                        != ReviewFileDecision.PLANNED.value,
                    )
                )
                coverage_complete = bool(
                    plan.rules_complete
                    and not incomplete_file_count
                    and not partial
                )
                lifecycle_occurrences, fixed_finding_count = (
                    self._reconcile_finding_lifecycles(
                        session,
                        run,
                        findings,
                        now,
                        coverage_complete=coverage_complete,
                    )
                )
                model_call_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        "openreviewer:model-call:"
                        f"{plan.id}:{result.request_fingerprint}",
                    )
                )
                # 计划只有一个兼容的 ModelCall 行。部分结果已经存在时复用
                # 该行并更新为本次聚合快照，避免唯一键冲突；完整结果随后
                # 可以在同一行上完成收口。
                model_call = (
                    session.scalar(
                        select(ModelCallRecord)
                        .where(ModelCallRecord.id == existing.id)
                        .with_for_update()
                    )
                    if existing is not None
                    else None
                )
                if model_call is None:
                    model_call = ModelCallRecord(
                        id=model_call_id,
                        review_plan_id=plan.id,
                        configuration_revision=configuration_revision,
                        provider=result.provider.value,
                        api_protocol=result.api_protocol.value,
                        model=result.model,
                        status=result.status.value,
                        prompt_version=result.prompt_version,
                        request_fingerprint=result.request_fingerprint,
                        provider_response_id=result.provider_response_id,
                        provider_request_id=result.provider_request_id,
                        response_status=result.response_status,
                        duration_ms=result.duration_ms,
                        input_tokens=result.usage.input_tokens,
                        output_tokens=result.usage.output_tokens,
                        cache_read_input_tokens=result.usage.cache_read_input_tokens,
                        cache_write_input_tokens=result.usage.cache_write_input_tokens,
                        reasoning_output_tokens=result.usage.reasoning_output_tokens,
                        estimated_cost_microusd=result.estimated_cost_microusd,
                        finding_count=0,
                        created_at=now,
                    )
                    session.add(model_call)
                else:
                    model_call_id = model_call.id
                    model_call.configuration_revision = configuration_revision
                    model_call.provider = result.provider.value
                    model_call.api_protocol = result.api_protocol.value
                    model_call.model = result.model
                    model_call.status = result.status.value
                    model_call.prompt_version = result.prompt_version
                    model_call.request_fingerprint = result.request_fingerprint
                    model_call.provider_response_id = result.provider_response_id
                    model_call.provider_request_id = result.provider_request_id
                    model_call.response_status = result.response_status
                    model_call.duration_ms = result.duration_ms
                    model_call.input_tokens = result.usage.input_tokens
                    model_call.output_tokens = result.usage.output_tokens
                    model_call.cache_read_input_tokens = result.usage.cache_read_input_tokens
                    model_call.cache_write_input_tokens = result.usage.cache_write_input_tokens
                    model_call.reasoning_output_tokens = result.usage.reasoning_output_tokens
                    model_call.estimated_cost_microusd = result.estimated_cost_microusd
                session.flush()
                existing_fingerprints = set(
                    session.scalars(
                        select(ReviewFindingRecord.fingerprint).where(
                            ReviewFindingRecord.review_run_id == run.id
                        )
                    )
                )
                new_findings = tuple(
                    item
                    for item in findings
                    if item.finding.fingerprint not in existing_fingerprints
                )
                finding_rows: list[dict[str, object]] = []
                for item in new_findings:
                    finding = item.finding
                    evidence_status = (
                        finding.evidence_verification_status
                        or EvidenceVerificationStatus.UNVERIFIED
                    )
                    location = finding.location
                    lifecycle_status, occurrence_count, previous_run_id = (
                        lifecycle_occurrences[finding.fingerprint]
                    )
                    if (
                        finding.head_sha != review_input.head_sha
                        or (
                            finding.verification_status.value == "verified"
                            and (location is None or not location.in_diff)
                        )
                    ):
                        raise ModelReviewConflictError(
                            "模型 Finding 的 SHA 或机器定位状态无效"
                        )
                    finding_rows.append(
                        {
                            "id": str(
                                uuid5(
                                    NAMESPACE_URL,
                                    "openreviewer:finding:"
                                    f"{run.id}:{finding.fingerprint}",
                                )
                            ),
                            "review_run_id": run.id,
                            "review_plan_id": plan.id,
                            "model_call_id": model_call_id,
                            "source_unit_key": item.source_unit_key,
                            "fingerprint": finding.fingerprint,
                            "head_sha": finding.head_sha,
                            "severity": finding.severity.value,
                            "category": finding.category.value,
                            "location_file": location.file if location else None,
                            "location_blob_sha": (
                                location.blob_sha if location else None
                            ),
                            "location_start_line": (
                                location.start_line if location else None
                            ),
                            "location_end_line": (
                                location.end_line if location else None
                            ),
                            "location_side": (
                                location.side.value if location else None
                            ),
                            "location_in_diff": (
                                location.in_diff if location else False
                            ),
                            "location_symbol": (
                                location.symbol if location else None
                            ),
                            "title": finding.title,
                            "evidence": finding.evidence,
                            "impact": finding.impact,
                            "suggestion": finding.suggestion,
                            "required_test": finding.required_test,
                            "confidence": finding.confidence,
                            "verification_status": (
                                finding.verification_status.value
                            ),
                            "evidence_verification_status": (
                                evidence_status.value
                            ),
                            "evidence_verification_reason": (
                                finding.evidence_verification_reason
                            ),
                            "evidence_verified_at": (
                                now
                                if evidence_status
                                is EvidenceVerificationStatus.VERIFIED
                                else None
                            ),
                            "adjudication_status": (
                                FindingAdjudicationStatus.UNREVIEWED.value
                            ),
                            "lifecycle_status": lifecycle_status.value,
                            "occurrence_count": occurrence_count,
                            "previous_review_run_id": previous_run_id,
                            "lifecycle_backfilled_at": now,
                            "rule_reference": finding.rule_reference,
                            "created_at": now,
                        }
                    )
                if finding_rows:
                    session.execute(insert(ReviewFindingRecord), finding_rows)

                total_finding_count = int(
                    session.scalar(
                        select(func.count())
                        .select_from(ReviewFindingRecord)
                        .where(ReviewFindingRecord.review_run_id == run.id)
                    )
                    or 0
                )
                model_call.finding_count = total_finding_count

                run.review_conclusion = (
                    ReviewConclusion.FINDINGS_PRESENT.value
                    if total_finding_count
                    else ReviewConclusion.NO_CONFIRMED_FINDINGS.value
                )
                run.coverage_status = (
                    CoverageStatus.PARTIAL.value
                    if partial or not coverage_complete
                    else CoverageStatus.COMPLETE.value
                )
                if partial:
                    # 兼容旧队列的 failed 状态，同时把真实 DAG 停在可重试的
                    # Agent 节点。成功批次/Finding 已保存，租约被安全释放。
                    self._set_owned_status(task, run, ExecutionStatus.FAILED, now)
                    self._set_workflow_status(
                        task,
                        run,
                        ExecutionStatus.AGENT_BATCHES,
                        now,
                    )
                    event_type = "review.model.partial"
                else:
                    plan.model_review_completed_at = now
                    self._set_owned_status(
                        task,
                        run,
                        ExecutionStatus.COMPLETED,
                        now,
                    )
                    # 旧 execution_status 保持 completed 以兼容现有队列；新的
                    # 工作流必须停在人工批准门，不得把结果误显示为已发布。
                    self._set_workflow_status(
                        task,
                        run,
                        ExecutionStatus.AWAITING_APPROVAL,
                        now,
                    )
                    event_type = "review.model.completed"
                self._add_event(
                    session,
                    task,
                    event_type,
                    result.request_fingerprint,
                    now,
                    extra_payload={
                        "review_plan_id": plan.id,
                        "model_call_id": model_call_id,
                        "provider": result.provider.value,
                        "model": result.model,
                        "model_call_status": result.status.value,
                        "finding_count": total_finding_count,
                        "new_finding_count": sum(
                            status is FindingOccurrenceStatus.NEW
                            for status, _count, _previous in lifecycle_occurrences.values()
                        ),
                        "partial": partial,
                        "coverage_status": run.coverage_status,
                        "still_present_finding_count": sum(
                            status is FindingOccurrenceStatus.STILL_PRESENT
                            for status, _count, _previous in lifecycle_occurrences.values()
                        ),
                        "reintroduced_finding_count": sum(
                            status is FindingOccurrenceStatus.REINTRODUCED
                            for status, _count, _previous in lifecycle_occurrences.values()
                        ),
                        "fixed_finding_count": fixed_finding_count,
                        "input_tokens": result.usage.input_tokens,
                        "output_tokens": result.usage.output_tokens,
                        "cache_read_input_tokens": (
                            result.usage.cache_read_input_tokens
                        ),
                        "cache_write_input_tokens": (
                            result.usage.cache_write_input_tokens
                        ),
                        "estimated_cost_microusd": (
                            result.estimated_cost_microusd
                        ),
                    },
                )
                session.commit()
                return StoredModelReview(
                    model_call_id=model_call_id,
                    created=existing is None,
                    finding_count=total_finding_count,
                    execution_status=(
                        ExecutionStatus.FAILED if partial else ExecutionStatus.COMPLETED
                    ),
                    coverage_status=run.coverage_status,
                    partial=partial,
                )
            except (
                ModelReviewConflictError,
                ModelReviewInputError,
                TaskLeaseLostError,
            ):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("model review could not be persisted") from exc

    def record_model_progress(
        self,
        lease: ReviewTaskLease,
        phase: str,
        payload: Mapping[str, object],
        *,
        agent: str = "default",
    ) -> None:
        """用短事务写入模型批次进度，不保存或伪造 Chain-of-Thought。"""

        if not agent or len(agent) > 32:
            raise ValueError("model progress agent name is invalid")
        allowed_phases = {
            "batches_planned",
            "batch_started",
            "request_started",
            "request_completed",
            "batch_completed",
            "batch_failed",
            "agent_completed",
            "agent_failed",
            "agent_not_applicable",
            "summary_completed",
            "summary_skipped",
            "aggregation_completed",
            "workflow_partial",
            "retry_requested",
            "retry_started",
        }
        if phase not in allowed_phases:
            raise ValueError("unsupported model progress phase")
        if lease.claimed_from_status is not ExecutionStatus.READY_FOR_REVIEW:
            raise ModelReviewConflictError("当前租约不属于模型审查阶段")
        now = self._clock()
        with self._sessions() as session:
            try:
                task, run = self._locked_owned_task_with_run(session, lease, now)
                task.updated_at = now
                run.updated_at = now
                self._add_event(
                    session,
                    task,
                    f"review.model.{phase}",
                    f"{agent}:model-attempt-{task.model_attempt_count}",
                    now,
                    extra_payload={"agent": agent, **dict(payload)},
                )
                session.commit()
            except (ModelReviewConflictError, TaskLeaseLostError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("model progress could not be persisted") from exc

    def mark_model_aggregating(self, lease: ReviewTaskLease) -> None:
        """在三路 Agent 完成后，用短事务暴露固定 DAG 的汇总节点。"""

        if lease.claimed_from_status is not ExecutionStatus.READY_FOR_REVIEW:
            raise ModelReviewConflictError("当前租约不属于模型审查阶段")
        now = self._clock()
        with self._sessions() as session:
            try:
                task, run = self._locked_owned_task_with_run(session, lease, now)
                current = ExecutionStatus(task.workflow_status)
                if current not in {
                    ExecutionStatus.AGENT_BATCHES,
                    ExecutionStatus.AGGREGATING,
                }:
                    raise ModelReviewConflictError("当前工作流不能进入结果汇总阶段")
                if current is not ExecutionStatus.AGGREGATING:
                    self._set_workflow_status(
                        task,
                        run,
                        ExecutionStatus.AGGREGATING,
                        now,
                    )
                    self._add_event(
                        session,
                        task,
                        "review.model.aggregating_started",
                        f"model-attempt-{task.model_attempt_count}",
                        now,
                        extra_payload={
                            "review_plan_id": lease.review_plan_id,
                            "agent_count": 3,
                        },
                    )
                session.commit()
            except (ModelReviewConflictError, TaskLeaseLostError):
                session.rollback()
                raise
            except (SQLAlchemyError, ValueError) as exc:
                session.rollback()
                raise TaskQueueError("model aggregation state could not be persisted") from exc

    def ensure_model_batches(
        self,
        lease: ReviewTaskLease,
        batches: tuple[ModelReviewBatch, ...],
        *,
        agent: str = "default",
    ) -> tuple[StoredModelBatch, ...]:
        """幂等保存模型批次定义；恢复时不会覆盖已完成结果。

        如果同一 Review Plan 的自动重试使用了新的单批配置，且旧批次尚未
        成功、也没有有效运行租约，则原子删除旧定义并重建；有成功结果或仍在
        执行的批次时保守报告冲突，避免丢失可复用结果或制造重复请求。
        """

        if not agent or len(agent) > 32:
            raise ValueError("model batch agent name is invalid")
        if lease.claimed_from_status is not ExecutionStatus.READY_FOR_REVIEW:
            raise ModelReviewConflictError("当前租约不属于模型审查阶段")
        if lease.review_plan_id is None:
            raise ModelReviewConflictError("模型批次缺少 Review Plan")
        if len(batches) > MAX_MODEL_REVIEW_BATCHES:
            raise ModelReviewConflictError("模型批次数量超过持久化上限")
        now = self._clock()
        numbers = tuple(batch.number for batch in batches)
        if len(numbers) != len(set(numbers)):
            raise ModelReviewConflictError("模型批次号必须唯一")
        with self._sessions() as session:
            try:
                self._locked_owned_task_with_run(session, lease, now)
                plan = session.scalar(
                    select(ReviewPlanRecord)
                    .where(ReviewPlanRecord.id == lease.review_plan_id)
                    .with_for_update()
                )
                if plan is None:
                    raise ModelReviewConflictError("模型批次关联的计划不存在")
                existing_rows = list(
                    session.scalars(
                        select(ModelReviewBatchRecord)
                        .where(
                            ModelReviewBatchRecord.review_plan_id
                            == lease.review_plan_id,
                            ModelReviewBatchRecord.agent == agent,
                        )
                        .order_by(ModelReviewBatchRecord.batch_number.asc())
                        .with_for_update()
                        .limit(MAX_MODEL_REVIEW_BATCHES + 1)
                    )
                )
                if len(existing_rows) > MAX_MODEL_REVIEW_BATCHES:
                    raise ModelReviewConflictError("已保存的模型批次数量超过上限")
                incoming = {
                    batch.number: (
                        batch.total,
                        tuple(unit.unit_key for unit in batch.review_input.units),
                        batch.estimated_input_tokens,
                    )
                    for batch in batches
                }
                existing = {row.batch_number: row for row in existing_rows}
                definitions_match = set(existing) == set(incoming) and all(
                    (
                        row.batch_count,
                        tuple(row.unit_keys),
                        row.estimated_input_tokens,
                    )
                    == incoming[number]
                    for number, row in existing.items()
                )
                if not definitions_match and existing_rows:
                    # Agent 配置（尤其是 64K -> 32K 的单批上限）可以在一次
                    # 失败后被管理员调整，导致同一计划重新规划出不同切片。旧
                    # 的 FAILED/PENDING 定义已经没有可复用结果；只有在没有成功
                    # 结果且没有有效运行租约时才允许整体重建，避免删除另一
                    # Worker 正在执行或已经成功的批次。
                    has_succeeded = any(
                        row.status == ModelBatchStatus.SUCCEEDED.value
                        for row in existing_rows
                    )
                    has_live_running = any(
                        row.status == ModelBatchStatus.RUNNING.value
                        and row.lease_expires_at is not None
                        and _as_utc(row.lease_expires_at) > _as_utc(now)
                        for row in existing_rows
                    )
                    if has_succeeded or has_live_running:
                        raise ModelReviewConflictError(
                            "模型批次定义与已保存结果不一致"
                        )
                    session.execute(
                        delete(ModelReviewBatchRecord)
                        .where(
                            ModelReviewBatchRecord.review_plan_id
                            == lease.review_plan_id,
                            ModelReviewBatchRecord.agent == agent,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    session.flush()
                    existing = {}

                for batch in batches:
                    unit_keys = [unit.unit_key for unit in batch.review_input.units]
                    row = existing.get(batch.number)
                    if row is None:
                        row = ModelReviewBatchRecord(
                            id=str(self._uuid_factory()),
                            review_plan_id=lease.review_plan_id,
                            agent=agent,
                            batch_number=batch.number,
                            batch_count=batch.total,
                            unit_keys=unit_keys,
                            estimated_input_tokens=batch.estimated_input_tokens,
                            status=ModelBatchStatus.PENDING.value,
                            attempt_count=0,
                            available_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                        session.add(row)
                        existing[batch.number] = row
                self._add_event(
                    session,
                    None,
                    "review.model.batches_persisted",
                    f"{agent}:{lease.review_plan_id}:{len(batches)}",
                    now,
                    aggregate_id=lease.review_run_id,
                    extra_payload={
                        "review_plan_id": lease.review_plan_id,
                        "agent": agent,
                        "batch_count": len(batches),
                    },
                )
                session.commit()
                rows = sorted(existing.values(), key=lambda item: item.batch_number)
                return tuple(self._stored_model_batch(row) for row in rows)
            except (TaskLeaseLostError, ModelReviewConflictError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("model batch definitions could not be persisted") from exc

    def claim_model_batch(
        self,
        lease: ReviewTaskLease,
        batch_number: int,
        *,
        agent: str = "default",
        lease_duration: timedelta,
    ) -> StoredModelBatch:
        """原子领取一个批次；成功批次只读返回。"""

        if lease_duration.total_seconds() <= 0:
            raise ValueError("model batch lease duration must be positive")
        if lease.review_plan_id is None:
            raise ModelReviewConflictError("模型批次缺少 Review Plan")
        now = self._clock()
        with self._sessions() as session:
            try:
                self._locked_owned_task_with_run(session, lease, now)
                row = session.scalar(
                    select(ModelReviewBatchRecord)
                    .where(
                        ModelReviewBatchRecord.review_plan_id == lease.review_plan_id,
                        ModelReviewBatchRecord.agent == agent,
                        ModelReviewBatchRecord.batch_number == batch_number,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise ModelReviewConflictError("模型批次不存在")
                status = ModelBatchStatus(row.status)
                if status is ModelBatchStatus.SUCCEEDED:
                    session.commit()
                    return self._stored_model_batch(row)
                if (
                    status is ModelBatchStatus.RUNNING
                    and row.lease_expires_at is not None
                    and _as_utc(row.lease_expires_at) > _as_utc(now)
                ):
                    # 即使 Worker ID 相同，也可能是同名副本或一次重入。调用方
                    # 无法区分“自己已领取”和“另一个请求正在执行”，因此必须等待
                    # 租约过期或结果落库，绝不能再次调用外部模型。
                    expiry = _as_utc(row.lease_expires_at)
                    raise ModelBatchBusyError(
                        retry_at=expiry,
                        lease_expires_at=expiry,
                    )
                if row.available_at is not None and _as_utc(row.available_at) > _as_utc(now):
                    available_at = _as_utc(row.available_at)
                    raise ModelBatchBusyError(
                        "模型批次尚未到重试时间",
                        retry_at=available_at,
                        available_at=available_at,
                    )
                row.status = ModelBatchStatus.RUNNING.value
                row.attempt_count += 1
                row.lease_owner = lease.worker_id
                row.lease_expires_at = now + lease_duration
                row.updated_at = now
                self._add_event(
                    session,
                    None,
                    "review.model.batch_claimed",
                    f"{agent}:{lease.review_plan_id}:{batch_number}:{row.attempt_count}",
                    now,
                    aggregate_id=lease.review_run_id,
                    extra_payload={
                        "review_plan_id": lease.review_plan_id,
                        "agent": agent,
                        "batch_number": batch_number,
                        "batch_count": row.batch_count,
                        "attempt_count": row.attempt_count,
                    },
                )
                session.commit()
                return self._stored_model_batch(row)
            except (
                TaskLeaseLostError,
                ModelReviewConflictError,
                ModelBatchBusyError,
            ):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("model batch could not be claimed") from exc

    def renew_model_batch(
        self,
        lease: ReviewTaskLease,
        batch_number: int,
        *,
        agent: str = "default",
        lease_duration: timedelta,
    ) -> StoredModelBatch:
        """延长当前 Worker 持有的模型批次租约。

        批次租约与任务租约分开存储；模型请求可能长于一次心跳周期，因此必须
        在同一任务所有权检查下单独续期。方法只更新批次行，不写进度事件，避免
        长请求产生无界的审计记录。
        """

        if lease_duration.total_seconds() <= 0:
            raise ValueError("model batch lease duration must be positive")
        if lease.review_plan_id is None:
            raise ModelReviewConflictError("模型批次缺少 Review Plan")
        now = self._clock()
        renewed_until = now + lease_duration
        with self._sessions() as session:
            try:
                self._locked_owned_task_with_run(session, lease, now)
                row = session.scalar(
                    select(ModelReviewBatchRecord)
                    .where(
                        ModelReviewBatchRecord.review_plan_id == lease.review_plan_id,
                        ModelReviewBatchRecord.agent == agent,
                        ModelReviewBatchRecord.batch_number == batch_number,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise ModelReviewConflictError("模型批次不存在")
                if row.status == ModelBatchStatus.SUCCEEDED.value:
                    session.commit()
                    return self._stored_model_batch(row)
                if (
                    row.status != ModelBatchStatus.RUNNING.value
                    or row.lease_owner != lease.worker_id
                    or row.lease_expires_at is None
                    or _as_utc(row.lease_expires_at) <= _as_utc(now)
                ):
                    raise TaskLeaseLostError("模型批次租约已失效")
                row.lease_expires_at = max(
                    _as_utc(row.lease_expires_at),
                    _as_utc(renewed_until),
                )
                row.updated_at = now
                session.commit()
                return self._stored_model_batch(row)
            except (TaskLeaseLostError, ModelReviewConflictError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("model batch lease could not be renewed") from exc

    def renew_model_batches(
        self,
        lease: ReviewTaskLease,
        batches: tuple[ModelBatchLease, ...],
    ) -> tuple[StoredModelBatch, ...]:
        """在一个事务中延长当前 Worker 持有的多个模型批次租约。

        固定工作流最多同时运行三路 Agent；批次心跳必须用一次有界查询和一次
        提交完成续租，不能在 Python 循环中逐批访问数据库形成 N+1。
        """

        if not batches:
            return ()
        if lease.review_plan_id is None:
            raise ModelReviewConflictError("模型批次缺少 Review Plan")
        for item in batches:
            if not item.agent or len(item.agent) > 32:
                raise ValueError("model batch agent name is invalid")
            if item.batch_number <= 0:
                raise ValueError("model batch number must be positive")
            if item.lease_duration.total_seconds() <= 0:
                raise ValueError("model batch lease duration must be positive")
        keys = tuple((item.agent, item.batch_number) for item in batches)
        if len(keys) != len(set(keys)):
            raise ValueError("model batch identities must be unique")
        now = self._clock()
        renewed_until = {
            (item.agent, item.batch_number): now + item.lease_duration
            for item in batches
        }
        predicates = tuple(
            and_(
                ModelReviewBatchRecord.agent == agent,
                ModelReviewBatchRecord.batch_number == batch_number,
            )
            for agent, batch_number in keys
        )
        with self._sessions() as session:
            try:
                self._locked_owned_task_with_run(session, lease, now)
                rows = list(
                    session.scalars(
                        select(ModelReviewBatchRecord)
                        .where(
                            ModelReviewBatchRecord.review_plan_id
                            == lease.review_plan_id,
                            or_(*predicates),
                        )
                        .with_for_update()
                    )
                )
                rows_by_key = {
                    (row.agent, row.batch_number): row for row in rows
                }
                for key in keys:
                    row = rows_by_key.get(key)
                    if row is None:
                        raise ModelReviewConflictError("模型批次不存在")
                    if row.status == ModelBatchStatus.SUCCEEDED.value:
                        continue
                    if (
                        row.status != ModelBatchStatus.RUNNING.value
                        or row.lease_owner != lease.worker_id
                        or row.lease_expires_at is None
                        or _as_utc(row.lease_expires_at) <= _as_utc(now)
                    ):
                        raise TaskLeaseLostError("模型批次租约已失效")
                    row.lease_expires_at = max(
                        _as_utc(row.lease_expires_at),
                        _as_utc(renewed_until[key]),
                    )
                    row.updated_at = now
                session.commit()
                return tuple(
                    self._stored_model_batch(rows_by_key[key]) for key in keys
                )
            except (TaskLeaseLostError, ModelReviewConflictError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("model batch leases could not be renewed") from exc

    def complete_model_batch(
        self,
        lease: ReviewTaskLease,
        batch_number: int,
        result: ModelReviewResult,
        *,
        agent: str = "default",
        expected_attempt_count: int | None = None,
    ) -> StoredModelBatch:
        """保存一次成功结果，并清除批次租约。

        ``expected_attempt_count`` 是领取批次时返回的代次。旧 Worker 即使和
        新 Worker 使用同一个稳定 ID，也不能在租约过期并重新领取后覆盖新结果。
        """

        if lease.review_plan_id is None:
            raise ModelReviewConflictError("模型批次缺少 Review Plan")
        now = self._clock()
        with self._sessions() as session:
            try:
                self._locked_owned_task_with_run(session, lease, now)
                row = session.scalar(
                    select(ModelReviewBatchRecord)
                    .where(
                        ModelReviewBatchRecord.review_plan_id == lease.review_plan_id,
                        ModelReviewBatchRecord.agent == agent,
                        ModelReviewBatchRecord.batch_number == batch_number,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise ModelReviewConflictError("模型批次不存在")
                if row.status == ModelBatchStatus.SUCCEEDED.value:
                    session.commit()
                    return self._stored_model_batch(row)
                if (
                    row.status != ModelBatchStatus.RUNNING.value
                    or row.lease_owner != lease.worker_id
                    or row.lease_expires_at is None
                    or _as_utc(row.lease_expires_at) <= _as_utc(now)
                    or (
                        expected_attempt_count is not None
                        and row.attempt_count != expected_attempt_count
                    )
                ):
                    raise TaskLeaseLostError("模型批次租约已失效")
                row.status = ModelBatchStatus.SUCCEEDED.value
                row.lease_owner = None
                row.lease_expires_at = None
                row.request_fingerprint = result.request_fingerprint
                row.provider_request_id = result.provider_request_id
                row.response_status = result.response_status
                row.duration_ms = result.duration_ms
                row.result = result.model_dump(mode="json")
                row.error_code = None
                row.error_message = None
                row.error_details = None
                row.updated_at = now
                self._add_event(
                    session,
                    None,
                    "review.model.batch_persisted",
                    f"{agent}:{lease.review_plan_id}:{batch_number}:{result.request_fingerprint}",
                    now,
                    aggregate_id=lease.review_run_id,
                    extra_payload={
                        "review_plan_id": lease.review_plan_id,
                        "agent": agent,
                        "batch_number": batch_number,
                        "response_status": result.response_status,
                        "duration_ms": result.duration_ms,
                        "input_tokens": result.usage.input_tokens,
                        "output_tokens": result.usage.output_tokens,
                        "provider_request_id": result.provider_request_id,
                    },
                )
                session.commit()
                return self._stored_model_batch(row)
            except (TaskLeaseLostError, ModelReviewConflictError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("model batch result could not be persisted") from exc

    def checkpoint_model_batch(
        self,
        lease: ReviewTaskLease,
        batch_number: int,
        checkpoint: Mapping[str, object],
        *,
        agent: str = "default",
        expected_attempt_count: int | None = None,
    ) -> StoredModelBatch:
        """保存截断拆分的结构化子结果检查点。

        检查点写在批次已有的 ``error_details`` JSON 列中，避免为一次恢复性
        优化引入迁移；它只在批次仍由当前 Worker 持有时更新，过期 Worker
        不能覆盖新代次的检查点。
        """

        if lease.review_plan_id is None:
            raise ModelReviewConflictError("模型批次缺少 Review Plan")
        validated = _validated_truncation_checkpoint(checkpoint)
        now = self._clock()
        with self._sessions() as session:
            try:
                self._locked_owned_task_with_run(session, lease, now)
                row = session.scalar(
                    select(ModelReviewBatchRecord)
                    .where(
                        ModelReviewBatchRecord.review_plan_id == lease.review_plan_id,
                        ModelReviewBatchRecord.agent == agent,
                        ModelReviewBatchRecord.batch_number == batch_number,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise ModelReviewConflictError("模型批次不存在")
                if row.status == ModelBatchStatus.SUCCEEDED.value:
                    session.commit()
                    return self._stored_model_batch(row)
                if (
                    row.status != ModelBatchStatus.RUNNING.value
                    or row.lease_owner != lease.worker_id
                    or row.lease_expires_at is None
                    or _as_utc(row.lease_expires_at) <= _as_utc(now)
                    or (
                        expected_attempt_count is not None
                        and row.attempt_count != expected_attempt_count
                    )
                ):
                    raise TaskLeaseLostError("模型批次租约已失效")
                details = (
                    dict(row.error_details)
                    if isinstance(row.error_details, dict)
                    else {}
                )
                details["truncation_checkpoint"] = validated
                row.error_details = details
                row.updated_at = now
                session.commit()
                return self._stored_model_batch(row)
            except (TaskLeaseLostError, ModelReviewConflictError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("model batch checkpoint could not be persisted") from exc

    def fail_model_batch(
        self,
        lease: ReviewTaskLease,
        batch_number: int,
        error: SafeError,
        *,
        agent: str = "default",
        retry_delay: timedelta | None = None,
        expected_attempt_count: int | None = None,
    ) -> StoredModelBatch:
        """保存单批安全错误，供阶段级重试恢复。"""

        if lease.review_plan_id is None:
            raise ModelReviewConflictError("模型批次缺少 Review Plan")
        now = self._clock()
        # 零秒是合法的立即重试退避；不能用 ``or`` 把 ``timedelta(0)``
        # 误当成“未传参数”并替换成默认退避。
        delay = (
            retry_delay
            if retry_delay is not None
            else timedelta(seconds=self._retry_base_seconds)
        )
        if delay.total_seconds() < 0:
            raise ValueError("model batch retry delay cannot be negative")
        with self._sessions() as session:
            try:
                self._locked_owned_task_with_run(session, lease, now)
                row = session.scalar(
                    select(ModelReviewBatchRecord)
                    .where(
                        ModelReviewBatchRecord.review_plan_id == lease.review_plan_id,
                        ModelReviewBatchRecord.agent == agent,
                        ModelReviewBatchRecord.batch_number == batch_number,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise ModelReviewConflictError("模型批次不存在")
                if row.status == ModelBatchStatus.SUCCEEDED.value:
                    session.commit()
                    return self._stored_model_batch(row)
                if row.status == ModelBatchStatus.FAILED.value:
                    # 失败上报可能因调用方重入而重复到达；保留第一次的退避和
                    # 错误快照，避免重复写事件或缩短退避窗口。
                    session.commit()
                    return self._stored_model_batch(row)
                if (
                    row.lease_owner != lease.worker_id
                    or row.lease_expires_at is None
                    or _as_utc(row.lease_expires_at) <= _as_utc(now)
                    or (
                        expected_attempt_count is not None
                        and row.attempt_count != expected_attempt_count
                    )
                ):
                    raise TaskLeaseLostError("模型批次由其他 Worker 持有")
                row.status = ModelBatchStatus.FAILED.value
                row.lease_owner = None
                row.lease_expires_at = None
                row.available_at = now + delay
                row.error_code = error.code.value
                row.error_message = error.safe_message[:1000]
                details = dict(error.details)
                # 如果截断拆分在本次调用中已经完成了部分子批次，保留检查点，
                # 让下一次领取直接复用这些成功结果，而不是重复请求。
                previous_details = row.error_details
                if isinstance(previous_details, dict):
                    checkpoint = previous_details.get("truncation_checkpoint")
                    if isinstance(checkpoint, dict):
                        details["truncation_checkpoint"] = checkpoint
                row.error_details = details
                row.updated_at = now
                self._add_event(
                    session,
                    None,
                    "review.model.batch_retry_waiting",
                    f"{agent}:{lease.review_plan_id}:{batch_number}:{row.attempt_count}:{error.code.value}",
                    now,
                    aggregate_id=lease.review_run_id,
                    error=error,
                    extra_payload={
                        "review_plan_id": lease.review_plan_id,
                        "agent": agent,
                        "batch_number": batch_number,
                        "attempt_count": row.attempt_count,
                        "retry_at": (now + delay).isoformat(),
                    },
                )
                session.commit()
                return self._stored_model_batch(row)
            except (TaskLeaseLostError, ModelReviewConflictError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("model batch failure could not be persisted") from exc

    def load_model_batches(
        self,
        lease: ReviewTaskLease,
        *,
        agent: str = "default",
    ) -> tuple[StoredModelBatch, ...]:
        """一次有界查询读取一个 Agent 的所有批次。"""

        if lease.review_plan_id is None:
            raise ModelReviewConflictError("模型批次缺少 Review Plan")
        now = self._clock()
        with self._sessions() as session:
            try:
                self._locked_owned_task_with_run(session, lease, now)
                rows = list(
                    session.scalars(
                        select(ModelReviewBatchRecord)
                        .where(
                            ModelReviewBatchRecord.review_plan_id
                            == lease.review_plan_id,
                            ModelReviewBatchRecord.agent == agent,
                        )
                        .order_by(ModelReviewBatchRecord.batch_number.asc())
                        .limit(MAX_MODEL_REVIEW_BATCHES + 1)
                    )
                )
                if len(rows) > MAX_MODEL_REVIEW_BATCHES:
                    raise ModelReviewConflictError("已保存的模型批次数量超过上限")
                return tuple(self._stored_model_batch(row) for row in rows)
            except (TaskLeaseLostError, ModelReviewConflictError):
                raise
            except SQLAlchemyError as exc:
                raise TaskQueueError("model batches could not be loaded") from exc

    @staticmethod
    def _stored_model_batch(row: ModelReviewBatchRecord) -> StoredModelBatch:
        result = None
        if row.result is not None:
            try:
                result = ModelReviewResult.model_validate(row.result)
            except (TypeError, ValueError) as exc:
                raise TaskQueueError("已保存的模型批次结果无效") from exc
        checkpoint = None
        if isinstance(row.error_details, dict):
            raw_checkpoint = row.error_details.get("truncation_checkpoint")
            if isinstance(raw_checkpoint, dict):
                checkpoint = dict(raw_checkpoint)
        return StoredModelBatch(
            id=row.id,
            review_plan_id=row.review_plan_id,
            agent=row.agent,
            batch_number=row.batch_number,
            batch_count=row.batch_count,
            unit_keys=tuple(str(item) for item in row.unit_keys),
            estimated_input_tokens=row.estimated_input_tokens,
            status=ModelBatchStatus(row.status),
            attempt_count=row.attempt_count,
            request_fingerprint=row.request_fingerprint,
            result=result,
            error_code=row.error_code,
            error_message=row.error_message,
            checkpoint=checkpoint,
        )

    def reserve_model_budget(
        self,
        lease: ReviewTaskLease,
        request: ModelBudgetRequest,
        *,
        agent: str = "default",
    ) -> ModelBudgetReservation:
        """在发送 HTTP 前锁定计划并预留最坏情况用量。

        新计划和升级后的历史计划都使用 ``observe`` 模式；显式保存
        ``enforce`` 的计划仍保留旧版硬预算行为。对极少数尚未完成迁移、
        模式仍为 NULL 的历史行，运行时也按 observe 兜底，避免预算再次阻断。
        """

        if lease.review_plan_id is None:
            raise ModelReviewConflictError("模型预算缺少 Review Plan")
        if not agent or len(agent) > 32:
            raise ValueError("model budget agent name is invalid")
        numeric_values = (
            request.request_bytes,
            request.input_token_upper_bound,
            request.output_token_upper_bound,
        )
        if any(value < 0 for value in numeric_values):
            raise ValueError("model budget reservation values cannot be negative")
        if (
            request.cost_upper_bound_microusd is not None
            and request.cost_upper_bound_microusd < 0
        ):
            raise ValueError("model budget reservation values cannot be negative")
        if request.input_token_upper_bound == 0 and request.request_bytes > 0:
            raise ValueError("non-empty model request requires an input reservation")
        now = self._clock()
        with self._sessions() as session:
            try:
                task, _run = self._locked_owned_task_with_run(session, lease, now)
                plan = session.scalar(
                    select(ReviewPlanRecord)
                    .where(ReviewPlanRecord.id == lease.review_plan_id)
                    .with_for_update()
                )
                if plan is None:
                    raise ModelReviewConflictError("模型预算关联的计划不存在")
                # 0040 迁移会把历史 NULL 回填为 observe。运行时仍以 observe
                # 兜底，防止迁移尚未完成或人工导入的旧行重新启用硬阻断；只有
                # 明确保存 enforce 的计划才执行旧版硬预算。
                budget_mode = plan.model_budget_mode or "observe"
                if budget_mode not in {"observe", "enforce"}:
                    raise TaskQueueError("模型预算处理模式无效")
                enforce_budget = budget_mode == "enforce"
                # 资源累计指标在 observe 模式只记录；墙上时钟仍是任务级
                # 运行保护，避免异常中转站让一个任务无限占用 Worker。
                started_at = plan.model_budget_started_at or now
                elapsed_ms = max(
                    0,
                    int((_as_utc(now) - _as_utc(started_at)).total_seconds() * 1000),
                )
                max_duration_ms = plan.max_model_duration_seconds * 1000
                budget_multiplier = plan.model_budget_resume_count + 1
                max_http_calls = plan.max_model_http_calls * budget_multiplier
                max_input_tokens = plan.max_model_input_tokens * budget_multiplier
                max_output_tokens = plan.max_model_output_tokens * budget_multiplier
                max_estimated_cost_microusd = (
                    plan.max_model_cost_microusd * budget_multiplier
                    if plan.max_model_cost_microusd is not None
                    else None
                )
                reserved_cost_microusd = request.cost_upper_bound_microusd or 0
                projections = {
                    "http_calls": plan.model_http_calls + 1,
                    "input_tokens": (
                        plan.model_input_tokens + request.input_token_upper_bound
                    ),
                    "output_tokens": (
                        plan.model_output_tokens + request.output_token_upper_bound
                    ),
                    "estimated_cost_microusd": (
                        plan.model_estimated_cost_microusd
                        + reserved_cost_microusd
                    ),
                }
                reason = plan.model_budget_exhausted_reason if enforce_budget else None
                if reason is None and elapsed_ms >= max_duration_ms:
                    reason = "duration"
                if (
                    reason is None
                    and max_estimated_cost_microusd is not None
                    and request.cost_upper_bound_microusd is None
                ):
                    reason = "pricing_unknown"
                if reason is None and projections["http_calls"] > max_http_calls:
                    reason = "http_calls"
                if (
                    reason is None
                    and projections["input_tokens"] > max_input_tokens
                ):
                    reason = "input_tokens"
                if (
                    reason is None
                    and projections["output_tokens"] > max_output_tokens
                ):
                    reason = "output_tokens"
                if (
                    reason is None
                    and max_estimated_cost_microusd is not None
                    and projections["estimated_cost_microusd"]
                    > max_estimated_cost_microusd
                ):
                    reason = "estimated_cost"
                if reason is not None and (enforce_budget or reason == "duration"):
                    plan.model_budget_started_at = started_at
                    plan.model_budget_exhausted_at = (
                        plan.model_budget_exhausted_at or now
                    )
                    plan.model_budget_exhausted_reason = reason
                    error = ModelBudgetExceededError(
                        reason,
                        details={
                            "review_plan_id": plan.id,
                            "http_calls": plan.model_http_calls,
                            "max_http_calls": max_http_calls,
                            "input_tokens": plan.model_input_tokens,
                            "max_input_tokens": max_input_tokens,
                            "output_tokens": plan.model_output_tokens,
                            "max_output_tokens": max_output_tokens,
                            "estimated_cost_microusd": (
                                plan.model_estimated_cost_microusd
                            ),
                            "max_estimated_cost_microusd": (
                                max_estimated_cost_microusd
                            ),
                            "pricing_configured": (
                                request.cost_upper_bound_microusd is not None
                            ),
                            "budget_resume_count": plan.model_budget_resume_count,
                            "elapsed_ms": elapsed_ms,
                            "max_duration_ms": max_duration_ms,
                        },
                    )
                    self._add_event(
                        session,
                        task,
                        "review.model.budget_exhausted",
                        f"{plan.id}:{reason}",
                        now,
                        error=error.error,
                        extra_payload={
                            "review_plan_id": plan.id,
                            "budget_reason": reason,
                        },
                    )
                    session.commit()
                    raise error

                if reason is not None:
                    # observe 模式只留下可审计事件，不设置 exhausted_reason，
                    # 避免管理界面把观测到的超限误判成需要人工恢复的暂停。
                    self._add_event(
                        session,
                        task,
                        "review.model.budget_observed",
                        f"{plan.id}:{reason}:{projections['http_calls']}",
                        now,
                        extra_payload={
                            "review_plan_id": plan.id,
                            "model_budget_mode": budget_mode,
                            "budget_reason": reason,
                            "http_calls": projections["http_calls"],
                            "max_http_calls": max_http_calls,
                            "input_tokens": projections["input_tokens"],
                            "max_input_tokens": max_input_tokens,
                            "output_tokens": projections["output_tokens"],
                            "max_output_tokens": max_output_tokens,
                            "estimated_cost_microusd": projections[
                                "estimated_cost_microusd"
                            ],
                            "max_estimated_cost_microusd": (
                                max_estimated_cost_microusd
                            ),
                            "pricing_configured": (
                                request.cost_upper_bound_microusd is not None
                            ),
                            "budget_resume_count": plan.model_budget_resume_count,
                            "elapsed_ms": elapsed_ms,
                            "max_duration_ms": max_duration_ms,
                        },
                    )

                call_id = str(self._uuid_factory())
                sequence = projections["http_calls"]
                plan.model_budget_started_at = started_at
                plan.model_http_calls = projections["http_calls"]
                plan.model_input_tokens = projections["input_tokens"]
                plan.model_output_tokens = projections["output_tokens"]
                plan.model_estimated_cost_microusd = projections[
                    "estimated_cost_microusd"
                ]
                session.add(
                    ModelHttpCallRecord(
                        id=call_id,
                        review_plan_id=plan.id,
                        sequence=sequence,
                        agent=agent,
                        provider=request.provider,
                        api_protocol=request.api_protocol,
                        model=request.model,
                        request_bytes=request.request_bytes,
                        reserved_input_tokens=request.input_token_upper_bound,
                        reserved_output_tokens=request.output_token_upper_bound,
                        reserved_cost_microusd=reserved_cost_microusd,
                        status="reserved",
                        started_at=now,
                    )
                )
                session.commit()
                return ModelBudgetReservation(
                    id=call_id,
                    review_plan_id=plan.id,
                    sequence=sequence,
                    reserved_input_tokens=request.input_token_upper_bound,
                    reserved_output_tokens=request.output_token_upper_bound,
                    reserved_cost_microusd=reserved_cost_microusd,
                    remaining_duration_ms=max(1, max_duration_ms - elapsed_ms),
                )
            except (
                ModelBudgetExceededError,
                ModelReviewConflictError,
                TaskLeaseLostError,
            ):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("model budget could not be reserved") from exc

    def settle_model_budget(
        self,
        reservation: ModelBudgetReservation,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        estimated_cost_microusd: int | None,
        response_status: int | None,
        duration_ms: int,
        uncertain: bool = False,
    ) -> None:
        """结算真实用量；无法确认是否计费时保留全部预留。"""

        if duration_ms < 0:
            raise ValueError("model budget duration cannot be negative")
        if response_status is not None and not 100 <= response_status <= 599:
            raise ValueError("model budget response status is invalid")
        actual_values = (input_tokens, output_tokens, estimated_cost_microusd)
        if any(value is not None and value < 0 for value in actual_values):
            raise ValueError("model budget actual values cannot be negative")
        if not uncertain and (input_tokens is None or output_tokens is None):
            raise ValueError("settled model budget requires token usage")
        now = self._clock()
        budget_error: ModelBudgetExceededError | None = None
        with self._sessions() as session:
            try:
                row = session.scalar(
                    select(ModelHttpCallRecord)
                    .where(ModelHttpCallRecord.id == reservation.id)
                    .with_for_update()
                )
                if row is None or row.review_plan_id != reservation.review_plan_id:
                    raise ModelReviewConflictError("模型预算预留不存在")
                if row.status != "reserved":
                    return
                plan = session.scalar(
                    select(ReviewPlanRecord)
                    .where(ReviewPlanRecord.id == reservation.review_plan_id)
                    .with_for_update()
                )
                if plan is None:
                    raise ModelReviewConflictError("模型预算关联的计划不存在")
                # 与 reserve_model_budget 保持一致：NULL 只按 observe 兜底，
                # 显式 enforce 才会让结算超限中断模型结果。
                budget_mode = plan.model_budget_mode or "observe"
                if budget_mode not in {"observe", "enforce"}:
                    raise TaskQueueError("模型预算处理模式无效")
                enforce_budget = budget_mode == "enforce"
                started_at = plan.model_budget_started_at or row.started_at or now
                elapsed_ms = max(
                    0,
                    int((_as_utc(now) - _as_utc(started_at)).total_seconds() * 1000),
                )
                max_duration_ms = plan.max_model_duration_seconds * 1000
                row.response_status = response_status
                row.duration_ms = duration_ms
                row.completed_at = now
                overrun_reason = (
                    plan.model_budget_exhausted_reason if enforce_budget else None
                )
                if uncertain:
                    row.status = "uncertain"
                else:
                    actual_input = int(input_tokens or 0)
                    actual_output = int(output_tokens or 0)
                    actual_cost = int(estimated_cost_microusd or 0)
                    plan.model_input_tokens = max(
                        0,
                        plan.model_input_tokens
                        - row.reserved_input_tokens
                        + actual_input,
                    )
                    plan.model_output_tokens = max(
                        0,
                        plan.model_output_tokens
                        - row.reserved_output_tokens
                        + actual_output,
                    )
                    plan.model_estimated_cost_microusd = max(
                        0,
                        plan.model_estimated_cost_microusd
                        - row.reserved_cost_microusd
                        + actual_cost,
                    )
                    row.actual_input_tokens = actual_input
                    row.actual_output_tokens = actual_output
                    row.actual_cost_microusd = (
                        actual_cost if estimated_cost_microusd is not None else None
                    )
                    row.status = "settled"
                    budget_multiplier = plan.model_budget_resume_count + 1
                    if overrun_reason is None and (
                        plan.model_input_tokens
                        > plan.max_model_input_tokens * budget_multiplier
                    ):
                        overrun_reason = "input_tokens"
                    elif overrun_reason is None and (
                        plan.model_output_tokens
                        > plan.max_model_output_tokens * budget_multiplier
                    ):
                        overrun_reason = "output_tokens"
                    elif overrun_reason is None and (
                        plan.max_model_cost_microusd is not None
                        and plan.model_estimated_cost_microusd
                        > plan.max_model_cost_microusd * budget_multiplier
                    ):
                        overrun_reason = "estimated_cost"
                # 结算时再次检查墙上时钟。硬预算模式下越过截止线会暂停任务；
                # observe 模式仅记录这次观测，不影响已经收到的模型结果。
                if overrun_reason is None and elapsed_ms >= max_duration_ms:
                    overrun_reason = "duration"
                if overrun_reason is not None:
                    if enforce_budget or overrun_reason == "duration":
                        newly_exhausted = plan.model_budget_exhausted_reason is None
                        plan.model_budget_exhausted_at = (
                            plan.model_budget_exhausted_at or now
                        )
                        plan.model_budget_exhausted_reason = overrun_reason
                        if newly_exhausted:
                            error = ModelBudgetExceededError(
                                overrun_reason,
                                details={
                                    "review_plan_id": plan.id,
                                    "model_http_calls": plan.model_http_calls,
                                    "model_input_tokens": plan.model_input_tokens,
                                    "model_output_tokens": plan.model_output_tokens,
                                    "model_estimated_cost_microusd": (
                                        plan.model_estimated_cost_microusd
                                    ),
                                    "budget_resume_count": plan.model_budget_resume_count,
                                    "elapsed_ms": elapsed_ms,
                                    "max_duration_ms": max_duration_ms,
                                    "settled_after_deadline": (
                                        overrun_reason == "duration"
                                    ),
                                },
                            )
                            self._add_event(
                                session,
                                None,
                                "review.model.budget_exhausted",
                                f"{plan.id}:{overrun_reason}:{reservation.sequence}",
                                now,
                                error=error.error,
                                aggregate_id=plan.review_run_id,
                                extra_payload={
                                    "review_plan_id": plan.id,
                                    "budget_reason": overrun_reason,
                                    "settled_after_deadline": (
                                        overrun_reason == "duration"
                                    ),
                                },
                            )
                            budget_error = error
                    else:
                        self._add_event(
                            session,
                            None,
                            "review.model.budget_observed",
                            f"{plan.id}:{overrun_reason}:{reservation.sequence}",
                            now,
                            aggregate_id=plan.review_run_id,
                            extra_payload={
                                "review_plan_id": plan.id,
                                "model_budget_mode": budget_mode,
                                "budget_reason": overrun_reason,
                                "model_http_calls": plan.model_http_calls,
                                "model_input_tokens": plan.model_input_tokens,
                                "model_output_tokens": plan.model_output_tokens,
                                "model_estimated_cost_microusd": (
                                    plan.model_estimated_cost_microusd
                                ),
                                "budget_resume_count": plan.model_budget_resume_count,
                                "elapsed_ms": elapsed_ms,
                                "max_duration_ms": max_duration_ms,
                                "settled_after_deadline": (
                                    overrun_reason == "duration"
                                ),
                            },
                        )
                session.commit()
            except ModelReviewConflictError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("model budget could not be settled") from exc
        if budget_error is not None:
            raise budget_error

    def pause_for_model_budget(
        self,
        lease: ReviewTaskLease,
        error: SafeError,
    ) -> None:
        """把旧版硬预算超限任务转为人工暂停，并保留原工作流节点。"""

        if error.code is not ErrorCode.MODEL_BUDGET_EXCEEDED:
            raise ValueError("only model budget errors can pause this workflow")
        now = self._clock()
        with self._sessions() as session:
            try:
                task, run = self._locked_owned_task_with_run(session, lease, now)
                current = ExecutionStatus(task.workflow_status)
                paused_from = (
                    current
                    if current
                    in {ExecutionStatus.AGENT_BATCHES, ExecutionStatus.AGGREGATING}
                    else ExecutionStatus.AGENT_BATCHES
                )
                task.last_error = error.safe_message[:4000]
                task.last_error_code = error.code.value
                task.last_error_retryable = False
                task.last_error_details = dict(error.details)
                task.workflow_paused_from = paused_from.value
                run.workflow_paused_from = paused_from.value
                task.workflow_status = ExecutionStatus.PAUSED.value
                run.workflow_status = ExecutionStatus.PAUSED.value
                task.execution_status = ExecutionStatus.READY_FOR_REVIEW.value
                run.execution_status = ExecutionStatus.READY_FOR_REVIEW.value
                task.available_at = now
                task.lease_owner = None
                task.lease_expires_at = None
                task.claimed_from_status = None
                task.updated_at = now
                run.updated_at = now
                self._add_event(
                    session,
                    task,
                    "review.workflow.pause",
                    f"model-budget:{lease.review_plan_id}",
                    now,
                    error=error,
                    extra_payload={
                        "action": "pause",
                        "actor": "model_budget",
                        "previous_status": paused_from.value,
                        "new_status": ExecutionStatus.PAUSED.value,
                        "paused_from": paused_from.value,
                        "review_plan_id": lease.review_plan_id,
                    },
                )
                session.commit()
            except TaskLeaseLostError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("model budget pause could not be persisted") from exc

    def mark_waiting_for_ci(self, lease: ReviewTaskLease) -> None:
        """供兼容测试路径把任务直接推进到 ``waiting_for_ci``。

        生产 Worker 使用 ``store_github_context`` 保存真实 CI；本方法只保留给不注入
        GitHub 读取器的测试和兼容调用。它清除租约并写事件，但绝不会伪造 ``completed``。

        参数：
            lease: 当前 Worker 领取任务时获得的、尚未过期的租约。

        异常：
            TaskLeaseLostError: 任务不再属于该 Worker。
            TaskQueueError: 关联运行不存在，或状态/事件事务无法提交。

        成功后任务不再有租约；本方法不会调用 GitHub、模型或外部消息系统。
        """
        now = self._clock()
        with self._sessions() as session:
            try:
                task, run = self._locked_owned_task_with_run(session, lease, now)

                task.execution_status = ExecutionStatus.WAITING_FOR_CI.value
                task.lease_owner = None
                task.lease_expires_at = None
                task.claimed_from_status = None
                task.updated_at = now
                run.execution_status = ExecutionStatus.WAITING_FOR_CI.value
                run.updated_at = now
                self._set_workflow_status(
                    task,
                    run,
                    ExecutionStatus.CI,
                    now,
                )
                self._add_event(
                    session,
                    task,
                    "review.waiting_for_ci",
                    f"waiting-for-ci:{task.attempt_count}",
                    now,
                )
                session.commit()
            except (TaskLeaseLostError, TaskQueueError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError(
                    "the review task could not enter the CI waiting state"
                ) from exc

    def retry_or_fail(self, lease: ReviewTaskLease, error: SafeError) -> None:
        """记录本次处理失败，并根据尝试次数安排重试或最终失败。

        错误对象已经包含稳定代码、重试属性、脱敏说明和安全详情；持久化层再次
        限制说明长度。只有仍持有有效租约的 Worker 才能执行此更新；状态、错误
        信息和事件一次性提交，失败时整体回滚。

        参数：
            lease: 发生异常的那次领取操作对应的租约。
            error: 已完成分类和统一脱敏的安全错误对象。

        异常：
            TaskLeaseLostError: 上报时租约已经失效，旧 Worker 不得覆盖新状态。
            TaskQueueError: 任务/运行读取或事务提交失败。

        当 ``attempt_count < max_attempts`` 时按指数退避重新排队；达到上限时把
        任务和运行都改为 ``failed``。两种结果都会写唯一 Outbox 事件。
        """
        now = self._clock()
        with self._sessions() as session:
            try:
                task, run = self._locked_owned_task_with_run(session, lease, now)
                self._reschedule_or_fail(
                    session,
                    task,
                    run,
                    now,
                    error,
                    event_suffix=(
                        f"model-attempt-{task.model_attempt_count}"
                        if lease.review_plan_id is not None
                        else f"attempt-{task.attempt_count}"
                    ),
                )
                session.commit()
            except (TaskLeaseLostError, TaskQueueError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("the failed review task could not be recorded") from exc

    def heartbeat_is_fresh(self, worker_id: str, max_age: timedelta) -> bool:
        """判断 Worker 最近一次心跳是否仍在新鲜度窗口内。

        参数：
            worker_id: 要检查的 Worker 稳定 ID。
            max_age: 允许的最大心跳年龄；调用方应传正数时间窗口。

        返回：
            找到记录且 ``now - last_seen_at <= max_age`` 时返回 ``True``；没有记录
            或心跳过旧返回 ``False``。

        异常：
            TaskQueueError: 查询数据库失败。方法不会把数据库错误误报为离线，
            而是让健康检查进程以失败退出。
        """
        now = self._clock()
        with self._sessions() as session:
            try:
                heartbeat = session.get(WorkerHeartbeatRecord, worker_id)
                if heartbeat is None:
                    return False
                return now - _as_utc(heartbeat.last_seen_at) <= max_age
            except SQLAlchemyError as exc:
                raise TaskQueueError("worker heartbeat could not be checked") from exc

    @staticmethod
    def _locked_owned_task(
        session: Session,
        lease: ReviewTaskLease,
        now: datetime,
    ) -> ReviewTaskRecord:
        """锁定并验证租约所属的任务。

        这是所有“修改运行中任务”操作共用的所有权检查。除了 ID 关联外，还会
        校验状态、Worker、失败尝试次数、CI 轮询代次和租约过期时间；任一条件
        失败都视为租约丢失。

        参数：
            session: 已在事务中的 SQLAlchemy 会话。
            lease: 调用方持有的租约快照。
            now: 本次操作统一使用的当前 UTC 时间。

        返回：
            被 ``FOR UPDATE`` 锁定且通过所有权检查的任务 ORM 对象；调用方可以在
            同一事务中安全修改它。

        异常：
            TaskLeaseLostError: 任务不存在、状态不是 ``running``、Worker/运行、失败
            尝试次数或 CI 轮询代次不匹配，或租约为空/已过期。

        这个私有方法故意集中所有权条件，避免续租、成功推进和失败上报各自漏掉
        某个检查而产生旧 Worker 覆盖新 Worker 的竞态。
        """
        statement = (
            select(ReviewTaskRecord)
            .join(
                ReviewRunRecord,
                ReviewRunRecord.id == ReviewTaskRecord.review_run_id,
            )
            .where(
                ReviewTaskRecord.id == lease.task_id,
                ReviewTaskRecord.review_run_id == lease.review_run_id,
                ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
                ReviewTaskRecord.lease_owner == lease.worker_id,
                ReviewTaskRecord.attempt_count == lease.attempt_count,
                ReviewTaskRecord.model_attempt_count == lease.model_attempt_count,
                ReviewTaskRecord.ci_poll_count == lease.ci_poll_count,
                ReviewTaskRecord.claimed_from_status
                == lease.claimed_from_status.value,
                or_(
                    ReviewTaskRecord.workflow_status.is_(None),
                    ReviewTaskRecord.workflow_status
                    != ExecutionStatus.PAUSED.value,
                ),
                or_(
                    ReviewRunRecord.workflow_status.is_(None),
                    ReviewRunRecord.workflow_status
                    != ExecutionStatus.PAUSED.value,
                ),
                ReviewRunRecord.execution_status == ExecutionStatus.RUNNING.value,
                ReviewTaskRecord.lease_expires_at.is_not(None),
                ReviewTaskRecord.lease_expires_at > now,
            )
            .options(
                Load(ReviewTaskRecord).load_only(
                    ReviewTaskRecord.id,
                    raiseload=True,
                )
            )
            .with_for_update()
        )
        task = session.scalar(statement)
        if task is None:
            raise TaskLeaseLostError("the worker no longer owns this review task")
        return task

    @staticmethod
    def _locked_owned_task_with_run(
        session: Session,
        lease: ReviewTaskLease,
        now: datetime,
    ) -> tuple[ReviewTaskRecord, ReviewRunRecord]:
        """通过一次命中索引的 JOIN 查询锁定所属任务及其运行记录。"""

        statement = (
            select(ReviewTaskRecord, ReviewRunRecord)
            .join(
                ReviewRunRecord,
                ReviewRunRecord.id == ReviewTaskRecord.review_run_id,
            )
            .where(
                ReviewTaskRecord.id == lease.task_id,
                ReviewTaskRecord.review_run_id == lease.review_run_id,
                ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
                ReviewTaskRecord.lease_owner == lease.worker_id,
                ReviewTaskRecord.attempt_count == lease.attempt_count,
                ReviewTaskRecord.model_attempt_count == lease.model_attempt_count,
                ReviewTaskRecord.ci_poll_count == lease.ci_poll_count,
                ReviewTaskRecord.claimed_from_status
                == lease.claimed_from_status.value,
                # 暂停是独立的人工门。即使旧数据在暂停时错误地保留了
                # ``execution_status=running``，也不能让旧 Worker 继续写入
                # 批次、预算或进度；与 _locked_owned_task 保持同一所有权条件。
                or_(
                    ReviewTaskRecord.workflow_status.is_(None),
                    ReviewTaskRecord.workflow_status
                    != ExecutionStatus.PAUSED.value,
                ),
                or_(
                    ReviewRunRecord.workflow_status.is_(None),
                    ReviewRunRecord.workflow_status
                    != ExecutionStatus.PAUSED.value,
                ),
                ReviewRunRecord.execution_status == ExecutionStatus.RUNNING.value,
                ReviewTaskRecord.lease_expires_at.is_not(None),
                ReviewTaskRecord.lease_expires_at > now,
            )
            .options(*_task_run_mutation_load_options())
            .with_for_update()
        )
        row = session.execute(statement).one_or_none()
        if row is None:
            raise TaskLeaseLostError("the worker no longer owns this review task")
        return row[0], row[1]

    def _get_or_create_version(
        self,
        session: Session,
        run: ReviewRunRecord,
        now: datetime,
    ) -> PullRequestVersionRecord:
        """锁定版本行；兼容历史手工任务缺少版本记录的情况。"""

        version = session.scalar(
            select(PullRequestVersionRecord)
            .where(
                PullRequestVersionRecord.review_version_key
                == run.review_version_key
            )
            .with_for_update()
        )
        if version is None:
            installation = session.get(GitHubInstallationRecord, run.installation_id)
            if installation is None:
                installation = GitHubInstallationRecord(
                    id=run.installation_id,
                    created_at=now,
                    last_seen_at=now,
                )
                session.add(installation)
                session.flush()
            else:
                installation.last_seen_at = now
            version = PullRequestVersionRecord(
                id=str(
                    uuid5(
                        NAMESPACE_URL,
                        f"openreviewer:{run.review_version_key}",
                    )
                ),
                review_version_key=run.review_version_key,
                installation_id=run.installation_id,
                repository_id=run.repository_id,
                repository=run.repository,
                pull_request_number=run.pull_request_number,
                head_sha=run.head_sha,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(version)
            session.flush()
        if (
            version.installation_id != run.installation_id
            or version.repository_id != run.repository_id
            or version.repository != run.repository
            or version.pull_request_number != run.pull_request_number
            or version.head_sha != run.head_sha
        ):
            raise TaskQueueError("stored PR version identity does not match the review run")
        return version

    @staticmethod
    def _update_pull_request_snapshot(
        version: PullRequestVersionRecord,
        context: GitHubReviewContext,
        now: datetime,
    ) -> None:
        pull_request = context.pull_request
        version.base_sha = pull_request.base_sha
        version.author_login = pull_request.author_login
        version.html_url = pull_request.html_url
        version.head_repository = pull_request.head_repository
        version.head_ref = pull_request.head_ref
        version.base_repository = pull_request.base_repository
        version.base_ref = pull_request.base_ref
        version.identity_fetched_at = now
        version.pr_state = pull_request.state.value
        version.is_draft = pull_request.draft
        version.title = pull_request.title
        version.changed_files_count = pull_request.changed_files
        version.pr_updated_at = pull_request.updated_at
        version.last_seen_at = now

    @staticmethod
    def _replace_files(
        session: Session,
        version_id: str,
        context: GitHubReviewContext,
        now: datetime,
    ) -> None:
        files = context.files
        if files is None:
            return
        session.execute(
            delete(PullRequestFileRecord).where(
                PullRequestFileRecord.pull_request_version_id == version_id
            )
        )
        if not files:
            return
        rows = [
            {
                "id": str(
                    uuid5(
                        NAMESPACE_URL,
                        f"openreviewer:file:{version_id}:{item.path}",
                    )
                ),
                "pull_request_version_id": version_id,
                "path": item.path,
                "previous_path": item.previous_path,
                "status": item.status.value,
                "blob_sha": item.blob_sha,
                "additions": item.additions,
                "deletions": item.deletions,
                "changes": item.changes,
                "patch_state": item.patch_state.value,
                "patch": item.patch,
                "observed_at": now,
            }
            for item in files
        ]
        session.execute(insert(PullRequestFileRecord), rows)

    @staticmethod
    def _replace_ci_checks(
        session: Session,
        version_id: str,
        context: GitHubReviewContext,
        now: datetime,
    ) -> None:
        ci = context.ci
        if ci is None:
            return
        session.execute(
            delete(PullRequestCiCheckRecord).where(
                PullRequestCiCheckRecord.pull_request_version_id == version_id
            )
        )
        if not ci.checks:
            return
        rows = [
            {
                "id": str(
                    uuid5(
                        NAMESPACE_URL,
                        "openreviewer:ci:"
                        f"{version_id}:{check.kind.value}:{check.external_key}",
                    )
                ),
                "pull_request_version_id": version_id,
                "kind": check.kind.value,
                "external_key": check.external_key,
                "name": check.name,
                "status": check.status,
                "conclusion": check.conclusion,
                "app_id": check.app_id,
                "observed_at": now,
            }
            for check in ci.checks
        ]
        session.execute(insert(PullRequestCiCheckRecord), rows)

    @staticmethod
    def _supersede_previous_versions(
        session: Session,
        current_run: ReviewRunRecord,
        now: datetime,
    ) -> int:
        """用两条批量 UPDATE 淘汰同一 PR 的其他 head SHA，查询次数为常数。"""

        replaceable_statuses = (
            ExecutionStatus.QUEUED.value,
            ExecutionStatus.WAITING_FOR_CI.value,
            ExecutionStatus.RUNNING.value,
            ExecutionStatus.READY_FOR_REVIEW.value,
            ExecutionStatus.COMPLETED.value,
        )
        previous_run_ids = select(ReviewRunRecord.id).where(
            ReviewRunRecord.repository_id == current_run.repository_id,
            ReviewRunRecord.pull_request_number
            == current_run.pull_request_number,
            ReviewRunRecord.id != current_run.id,
            ReviewRunRecord.head_sha != current_run.head_sha,
            ReviewRunRecord.execution_status.in_(replaceable_statuses),
        )
        session.execute(
            update(ReviewTaskRecord)
            .where(
                ReviewTaskRecord.review_run_id.in_(previous_run_ids),
                ReviewTaskRecord.execution_status.in_(replaceable_statuses),
            )
            .values(
                execution_status=ExecutionStatus.SUPERSEDED.value,
                workflow_status=ExecutionStatus.SUPERSEDED.value,
                workflow_paused_from=None,
                lease_owner=None,
                lease_expires_at=None,
                claimed_from_status=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        result = session.execute(
            update(ReviewRunRecord)
            .where(
                ReviewRunRecord.id.in_(previous_run_ids),
                ReviewRunRecord.execution_status.in_(replaceable_statuses),
            )
            .values(
                execution_status=ExecutionStatus.SUPERSEDED.value,
                workflow_status=ExecutionStatus.SUPERSEDED.value,
                workflow_paused_from=None,
                publish_attempt_token=None,
                coverage_status=CoverageStatus.STALE.value,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if not isinstance(result, CursorResult):
            raise TaskQueueError("superseded review update returned no row count")
        return max(0, int(result.rowcount or 0))

    @staticmethod
    def _set_owned_status(
        task: ReviewTaskRecord,
        run: ReviewRunRecord,
        status: ExecutionStatus,
        now: datetime,
    ) -> None:
        task.execution_status = status.value
        task.workflow_paused_from = None
        task.lease_owner = None
        task.lease_expires_at = None
        task.claimed_from_status = None
        task.updated_at = now
        run.execution_status = status.value
        run.workflow_paused_from = None
        run.updated_at = now

    @staticmethod
    def _set_workflow_status(
        task: ReviewTaskRecord,
        run: ReviewRunRecord,
        status: ExecutionStatus,
        now: datetime,
    ) -> None:
        """更新新 DAG 状态，不改变兼容队列使用的 execution_status。"""

        task.workflow_status = status.value
        run.workflow_status = status.value
        if status is not ExecutionStatus.PAUSED:
            task.workflow_paused_from = None
            run.workflow_paused_from = None
        task.updated_at = now
        run.updated_at = now

    def _reschedule_or_fail(
        self,
        session: Session,
        task: ReviewTaskRecord,
        run: ReviewRunRecord,
        now: datetime,
        error: SafeError,
        *,
        event_suffix: str,
    ) -> None:
        """在当前事务内选择重试或最终失败，并追加对应 Outbox 事件。

        ``attempt_count`` 已在领取时递增，因此达到 ``max_attempts`` 就直接失败；
        否则把任务放回队列，并按指数退避计算下一次可用时间。调用者负责在外层
        事务中提交或回滚。

        参数：
            session: 当前外层事务会话。
            task: 已锁定且确认属于当前操作的运行中任务。
            now: 本次状态变化统一使用的时间。
            error: 要拆分写入结构化错误字段的安全错误对象。
            event_suffix: 附加到事件唯一键的尝试/恢复标识，防止同一状态事件重复。

        副作用：
            清除租约并更新任务和关联运行；未耗尽尝试时设置下一次
            ``available_at``，耗尽时设置 ``failed``；最后把事件对象加入当前会话。

        异常：
            TaskQueueError: 找不到关联运行。此方法不提交事务，异常由调用方负责回滚。
        """
        claimed_from = ExecutionStatus(
            task.claimed_from_status or ExecutionStatus.QUEUED.value
        )
        is_model_stage = (
            claimed_from is ExecutionStatus.READY_FOR_REVIEW
            and task.model_attempt_count > 0
        )
        batch_retry_managed = (
            is_model_stage
            and error.code is ErrorCode.MODEL_BATCH_BUSY
            and error.details.get("batch_retry_managed") is True
        )
        if batch_retry_managed:
            # 只有批次忙碌才表示本次领取没有发出模型请求。其他批次错误
            # （超时、解析失败等）确实已经完成了一次模型尝试，不能回退
            # model_attempt_count，否则会绕过任务级重试上限或改变后续阶段判断。
            task.model_attempt_count = max(0, task.model_attempt_count - 1)
        active_attempt_count = (
            task.model_attempt_count if is_model_stage else task.attempt_count
        )
        task.last_error = error.safe_message[:4000]
        task.last_error_code = error.code.value
        task.last_error_retryable = error.retryable
        task.last_error_details = dict(error.details)
        task.lease_owner = None
        task.lease_expires_at = None
        task.claimed_from_status = None
        task.updated_at = now
        if not error.retryable or (
            active_attempt_count >= task.max_attempts
            and not batch_retry_managed
        ):
            task.execution_status = ExecutionStatus.FAILED.value
            run.execution_status = ExecutionStatus.FAILED.value
            self._set_workflow_status(task, run, ExecutionStatus.FAILED, now)
            event_type = "review.task.failed"
            event_key = f"failed:{event_suffix}"
            event_payload: dict[str, object] | None = None
        else:
            retry_status = (
                claimed_from
                if claimed_from
                in {
                    ExecutionStatus.QUEUED,
                    ExecutionStatus.WAITING_FOR_CI,
                    ExecutionStatus.READY_FOR_REVIEW,
                }
                else ExecutionStatus.QUEUED
            )
            task.execution_status = retry_status.value
            run.execution_status = retry_status.value
            retry_at = self._batch_retry_at(error, now)
            default_retry_at = now + self._retry_delay(active_attempt_count)
            # 批次忙碌/退避时间来自数据库中的持久化状态，不能被普通任务的
            # 5/10/20 秒退避提前覆盖；同时保留默认退避作为最小间隔，避免
            # 已经过期或时钟轻微回拨时立即忙轮询。
            task.available_at = max(
                default_retry_at,
                retry_at if retry_at is not None else default_retry_at,
            )
            event_type = "review.task.retry_scheduled"
            event_key = f"retry:{event_suffix}"
            event_payload = {
                "retry_at": task.available_at.isoformat(),
                "retry_delay_seconds": max(
                    0,
                    int((task.available_at - now).total_seconds()),
                ),
            }
        run.updated_at = now
        self._add_event(
            session,
            task,
            event_type,
            event_key,
            now,
            error=error,
            extra_payload=event_payload,
        )

    def _retry_delay(self, attempt_count: int) -> timedelta:
        """根据已消耗的尝试次数计算指数退避时长。

        参数：
            attempt_count: 领取时已经递增后的尝试次数；第一次失败传 1。

        返回：
            ``base * 2 ** (attempt_count - 1)`` 秒，但不会超过配置的 cap。即默认
            产生 5、10、20 秒等延迟，直到上限 300 秒。

        负数或零不会产生负延迟：指数使用 ``max(0, attempt_count - 1)``，便于
        数据修复或测试传入边界值时保持安全。
        """
        # 先限制指数再做幂运算，避免损坏数据中的超大计数造成巨大整数。
        exponent = min(8, max(0, attempt_count - 1))
        seconds = min(
            self._retry_cap_seconds,
            self._retry_base_seconds * (2**exponent),
        )
        return timedelta(seconds=seconds)

    @staticmethod
    def _batch_retry_at(
        error: SafeError,
        now: datetime,
    ) -> datetime | None:
        """读取批次级错误携带的下一次可尝试时间。

        只有 ``MODEL_BATCH_BUSY`` 且明确标记为批次管理的错误才允许影响任务
        调度；其他错误详情即使包含同名字段也不会把任务任意推迟。
        """

        if (
            error.code is not ErrorCode.MODEL_BATCH_BUSY
            or error.details.get("batch_retry_managed") is not True
        ):
            return None
        raw_retry_at = error.details.get("retry_at")
        if not isinstance(raw_retry_at, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw_retry_at)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        else:
            parsed = parsed.astimezone(UTC)
        if parsed <= _as_utc(now):
            return None
        return parsed

    def _add_event(
        self,
        session: Session,
        task: ReviewTaskRecord | None,
        event_type: str,
        key_suffix: str,
        occurred_at: datetime,
        *,
        error: SafeError | None = None,
        extra_payload: Mapping[str, object] | None = None,
        aggregate_id: str | None = None,
    ) -> None:
        """在当前事务中追加一条不可重复的任务状态 Outbox 事件。

        事件只携带运行 ID、任务 ID 和尝试次数等非敏感元数据；具体发布器可以在
        后续阶段读取 ``outbox_events``，而不会影响任务状态事务的原子性。

        参数：
            session: 当前状态事务使用的会话；事件只加入会话，不在此处单独提交。
            task: 事件关联的任务记录。
            event_type: 稳定的事件类型，例如 ``review.task.running``。
            key_suffix: 与任务 ID 拼接成唯一 ``event_key`` 的后缀。
            occurred_at: 事件发生时间，由调用方统一提供。

        副作用：
            向会话加入一条尚未发布的 ``OutboxEventRecord``，发布尝试次数初始化为
            0。外部发布器稍后可以读取并投影到 SSE、通知或其他系统。

        该方法不会访问网络，也不会把 ``last_error`` 放入事件 payload，避免事件
        总线携带可能敏感的异常文本。
        """
        payload: dict[str, object] = {}
        if task is not None:
            payload.update(
                {
                    "review_run_id": task.review_run_id,
                    "review_task_id": task.id,
                    "attempt_count": task.attempt_count,
                    "model_attempt_count": task.model_attempt_count,
                    "ci_poll_count": task.ci_poll_count,
                }
            )
        if aggregate_id is None:
            aggregate_id = task.review_run_id if task is not None else "unknown"
        if error is not None:
            payload.update(
                {
                    "error_code": error.code.value,
                    "error_message": redact_sensitive(error.safe_message),
                    "error_retryable": error.retryable,
                }
            )
        if extra_payload is not None:
            payload.update(extra_payload)
        event_id = str(self._uuid_factory())
        event_identity = (
            f"{event_type}:{task.id if task is not None else aggregate_id}:"
            f"{key_suffix}:{event_id}"
        ).encode()
        session.add(
            OutboxEventRecord(
                id=event_id,
                # 状态事务本身保证同一次转换只提交一次；事件 ID 区分人工重试后
                # 计数器重新从 1 开始的全新转换，避免旧事件键阻断整个 Worker。
                event_key=f"review.task.event:{sha256(event_identity).hexdigest()}",
                aggregate_type="review_run",
            aggregate_id=aggregate_id,
                event_type=event_type,
                payload=payload,
                occurred_at=occurred_at,
                publish_attempts=0,
            )
        )
