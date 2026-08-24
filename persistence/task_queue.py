"""基于 PostgreSQL 的任务租约、恢复、重试与心跳存储。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Load, Session, sessionmaker

from domain.enums import ExecutionStatus, WorkerStatus
from domain.security import ErrorCode, SafeError
from persistence.models import (
    OutboxEventRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
    WorkerHeartbeatRecord,
)
from services.task_queue import (
    ReviewTaskLease,
    TaskLeaseLostError,
    TaskQueueError,
)


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


def _task_run_mutation_load_options() -> tuple[Load, Load]:
    """只加载任务和运行状态转换代码实际读取的列。"""

    return (
        Load(ReviewTaskRecord).load_only(
            ReviewTaskRecord.id,
            ReviewTaskRecord.review_run_id,
            ReviewTaskRecord.attempt_count,
            ReviewTaskRecord.max_attempts,
            raiseload=True,
        ),
        Load(ReviewRunRecord).load_only(
            ReviewRunRecord.id,
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

    def record_heartbeat(
        self,
        worker_id: str,
        worker_status: WorkerStatus,
        current_task_id: str | None = None,
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

        异常：
            TaskQueueError: 查询、插入、更新或提交失败。事务会先回滚，调用方
            不应把失败的心跳当成在线信号。

        ``started_at`` 只在首次插入时设置；固定 Worker ID 重启后不会自动重置，
        因而它表示数据库第一次见到该 ID 的时间，而非进程最近一次启动时间。
        """
        now = self._clock()
        with self._sessions() as session:
            try:
                heartbeat = session.get(WorkerHeartbeatRecord, worker_id)
                if heartbeat is None:
                    heartbeat = WorkerHeartbeatRecord(
                        worker_id=worker_id,
                        status=worker_status.value,
                        current_task_id=current_task_id,
                        started_at=now,
                        last_seen_at=now,
                    )
                    session.add(heartbeat)
                else:
                    heartbeat.status = worker_status.value
                    heartbeat.current_task_id = current_task_id
                    heartbeat.last_seen_at = now
                session.commit()
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
                    self._reschedule_or_fail(
                        session,
                        task,
                        run,
                        now,
                        lease_error,
                        event_suffix=f"lease-expired-{task.attempt_count}",
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
    ) -> ReviewTaskLease | None:
        """原子领取一个当前可执行的任务。

        只选择状态为 ``queued`` 且 ``available_at`` 已到期的任务，并按优先级、
        可用时间和创建时间排序。锁定后同时更新任务和运行状态、尝试次数、租约
        所有者及过期时间，再写入 ``review.task.running`` 事件；没有可领取任务时
        返回 ``None``。

        参数：
            worker_id: 领取者的稳定身份，会写入 ``lease_owner``。
            lease_duration: 从当前时钟到租约到期的时长，必须大于零。

        返回：
            成功时返回包含数据库主键、运行 ID、尝试次数和到期时间的租约快照；
            没有合资格任务时返回 ``None``，此时只提交一个空事务并释放锁。

        异常：
            ValueError: 租约时长不大于零。
            TaskQueueError: 关联运行不存在、数据库锁定/更新/提交失败。

        领取时会把 ``attempt_count`` 先加一，再把运行和任务同时改为 ``running``；
        因此重试判断可以用“本次已经是第几次尝试”而不用额外计数器。
        """
        if lease_duration.total_seconds() <= 0:
            raise ValueError("lease_duration must be positive")
        now = self._clock()
        lease_expires_at = now + lease_duration
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
                        == ExecutionStatus.QUEUED.value,
                        ReviewTaskRecord.available_at <= now,
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
                task, run = row

                task.execution_status = ExecutionStatus.RUNNING.value
                task.attempt_count += 1
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
                    f"running:{task.attempt_count}",
                    now,
                )
                session.commit()
                return ReviewTaskLease(
                    task_id=task.id,
                    review_run_id=task.review_run_id,
                    worker_id=worker_id,
                    attempt_count=task.attempt_count,
                    lease_expires_at=lease_expires_at,
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
                    lease_expires_at=renewed_until,
                )
            except TaskLeaseLostError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise TaskQueueError("the review task lease could not be renewed") from exc

    def mark_waiting_for_ci(self, lease: ReviewTaskLease) -> None:
        """完成当前 M2 准备步骤并把任务推进到 ``waiting_for_ci``。

        这个状态是有意保留的能力边界：当前项目还没有 GitHub CI 回调和模型审查，
        因此这里清除租约、同步更新运行状态并写事件，但绝不会伪造 ``completed``。

        参数：
            lease: 当前 Worker 领取任务时获得的、尚未过期的租约。

        异常：
            TaskLeaseLostError: 任务不再属于该 Worker。
            TaskQueueError: 关联运行不存在，或状态/事件事务无法提交。

        成功后任务不再有租约，后续需要真实 CI/模型事件才能继续推进；本方法不会
        调用 GitHub、模型或外部消息系统。
        """
        now = self._clock()
        with self._sessions() as session:
            try:
                task, run = self._locked_owned_task_with_run(session, lease, now)

                task.execution_status = ExecutionStatus.WAITING_FOR_CI.value
                task.lease_owner = None
                task.lease_expires_at = None
                task.updated_at = now
                run.execution_status = ExecutionStatus.WAITING_FOR_CI.value
                run.updated_at = now
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
                    event_suffix=f"attempt-{task.attempt_count}",
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
        校验状态、Worker、尝试次数和租约过期时间；任一条件失败都视为租约丢失。

        参数：
            session: 已在事务中的 SQLAlchemy 会话。
            lease: 调用方持有的租约快照。
            now: 本次操作统一使用的当前 UTC 时间。

        返回：
            被 ``FOR UPDATE`` 锁定且通过所有权检查的任务 ORM 对象；调用方可以在
            同一事务中安全修改它。

        异常：
            TaskLeaseLostError: 任务不存在、状态不是 ``running``、Worker/运行/尝试
            次数不匹配，或租约为空/已过期。

        这个私有方法故意集中所有权条件，避免续租、成功推进和失败上报各自漏掉
        某个检查而产生旧 Worker 覆盖新 Worker 的竞态。
        """
        statement = (
            select(ReviewTaskRecord)
            .where(
                ReviewTaskRecord.id == lease.task_id,
                ReviewTaskRecord.review_run_id == lease.review_run_id,
                ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
                ReviewTaskRecord.lease_owner == lease.worker_id,
                ReviewTaskRecord.attempt_count == lease.attempt_count,
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
        task.last_error = error.safe_message[:4000]
        task.last_error_code = error.code.value
        task.last_error_retryable = error.retryable
        task.last_error_details = dict(error.details)
        task.lease_owner = None
        task.lease_expires_at = None
        task.updated_at = now
        if not error.retryable or task.attempt_count >= task.max_attempts:
            task.execution_status = ExecutionStatus.FAILED.value
            run.execution_status = ExecutionStatus.FAILED.value
            event_type = "review.task.failed"
            event_key = f"failed:{event_suffix}"
        else:
            task.execution_status = ExecutionStatus.QUEUED.value
            run.execution_status = ExecutionStatus.QUEUED.value
            task.available_at = now + self._retry_delay(task.attempt_count)
            event_type = "review.task.retry_scheduled"
            event_key = f"retry:{event_suffix}"
        run.updated_at = now
        self._add_event(
            session,
            task,
            event_type,
            event_key,
            now,
            error=error,
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
        exponent = max(0, attempt_count - 1)
        seconds = min(
            self._retry_cap_seconds,
            self._retry_base_seconds * (2**exponent),
        )
        return timedelta(seconds=seconds)

    def _add_event(
        self,
        session: Session,
        task: ReviewTaskRecord,
        event_type: str,
        key_suffix: str,
        occurred_at: datetime,
        *,
        error: SafeError | None = None,
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
        payload: dict[str, object] = {
            "review_run_id": task.review_run_id,
            "review_task_id": task.id,
            "attempt_count": task.attempt_count,
        }
        if error is not None:
            payload.update(
                {
                    "error_code": error.code.value,
                    "error_retryable": error.retryable,
                }
            )
        session.add(
            OutboxEventRecord(
                id=str(self._uuid_factory()),
                event_key=f"{event_type}:{task.id}:{key_suffix}",
                aggregate_type="review_run",
                aggregate_id=task.review_run_id,
                event_type=event_type,
                payload=payload,
                occurred_at=occurred_at,
                publish_attempts=0,
            )
        )
