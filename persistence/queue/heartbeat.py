"""heartbeat 阶段的有界事务与数据访问。"""

from datetime import timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from domain.enums import WorkerStatus
from persistence.models import WorkerHeartbeatRecord
from persistence.queue.common import _as_utc
from persistence.queue.context import QueueStorage
from services.task_queue import TaskLeaseLostError, TaskQueueError


def _validate_heartbeat_identity(worker_id: str, instance_id: str | None) -> None:
    if not worker_id or len(worker_id) > 200:
        raise ValueError("worker ID must contain 1 to 200 characters")
    if instance_id is not None and (not instance_id or len(instance_id) > 64):
        raise ValueError("worker instance ID must contain 1 to 64 characters")


def start_heartbeat(
    self: QueueStorage,
    worker_id: str,
    instance_id: str,
) -> None:
    """原子接管稳定 Worker ID，并把旧进程的后续 CAS 心跳变为失败。

    每次进程启动都生成新的 ``instance_id``。已有行会在一个 UPDATE 中替换
    token、启动时间和状态；没有行时插入。并发首次启动发生唯一键竞争时，
    失败方回滚后再执行一次接管 UPDATE，因此最终只会有一个 token 生效。
    """

    _validate_heartbeat_identity(worker_id, instance_id)
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
                raise TaskQueueError("worker heartbeat could not be claimed") from exc
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError("worker heartbeat could not be claimed") from exc


def record_heartbeat(
    self: QueueStorage,
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
    _validate_heartbeat_identity(worker_id, instance_id)
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
                    raise TaskLeaseLostError("worker heartbeat ownership was lost")
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
                    raise TaskLeaseLostError("worker heartbeat ownership was lost")
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


def heartbeat_is_fresh(self: QueueStorage, worker_id: str, max_age: timedelta) -> bool:
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
