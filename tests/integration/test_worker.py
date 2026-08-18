from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from apps.worker.main import WorkerRuntime, WorkerSettings
from domain.enums import ExecutionStatus, WorkerStatus
from domain.models import ReviewRequest
from persistence.database import Database
from persistence.models import (
    Base,
    OutboxEventRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
    WorkerHeartbeatRecord,
)
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from services.reviews import ReviewService
from services.task_queue import TaskLeaseLostError


class MutableClock:
    def __init__(self, value: datetime) -> None:
        """创建队列测试使用的可推进时钟。

        参数：
            value: 初始当前时间。

        测试通过直接修改 ``value`` 模拟退避等待和租约过期，不需要真实 sleep，
        因而用例运行快速且结果不受 CI 机器调度影响。
        """
        self.value = value

    def __call__(self) -> datetime:
        """返回当前固定测试时间，匹配队列的无参数时钟协议。

        返回：
            当前 ``value``；调用本身不会自动推进时间。

        该方法没有副作用，可在同一状态转换中重复调用并得到相同结果。
        """
        return self.value


TEST_TASK_AVAILABLE_AT = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path):
    """创建包含所有 Worker 相关表的隔离临时数据库。

    参数：
        tmp_path: pytest 为当前用例提供的临时目录。

    产生：
        已创建运行、任务、Outbox 和心跳表的 ``Database``；用例结束后释放连接池。
    """
    path = (tmp_path / "worker.sqlite3").as_posix()
    configured = Database.connect(f"sqlite:///{path}")
    Base.metadata.create_all(configured.engine)
    try:
        yield configured
    finally:
        configured.dispose()


def submit_review(database: Database, key: str = "worker-test") -> str:
    """创建一条 queued 任务并保证任意测试时钟都能立即领取。

    参数：
        database: 当前用例数据库。
        key: 唯一幂等键，允许同一用例准备多条不同任务。

    返回：
        新建任务 ID。

    服务层先按真实事务创建运行、任务和事件；随后辅助函数把 ``available_at``
    固定到 2000 年，避免本机真实时间和注入测试时间的先后关系影响领取结果。
    """
    result = ReviewService(
        SqlAlchemyReviewRepository(database.sessions)
    ).submit(
        ReviewRequest(
            installation_id=10,
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=128,
            head_sha="a" * 40,
        ),
        key,
    )
    # Keep task availability independent of the wall clock used by CI.
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, result.review_task_id)
        assert task is not None
        task.available_at = TEST_TASK_AVAILABLE_AT
        session.commit()
    return result.review_task_id


def test_worker_claims_one_task_and_stops_at_waiting_for_ci(
    database: Database,
) -> None:
    """验证 Worker 一轮执行的完整成功状态流。

    参数：
        database: 当前用例数据库。

    动作：准备一条 queued 任务，用固定 Worker 配置运行一次 ``run_once``。
    预期：任务和运行都到 ``waiting_for_ci``，尝试次数为 1，租约已清除，Worker
    心跳恢复 idle 且无当前任务；Outbox 共包含请求、运行中和等待 CI 三条事件。
    """
    task_id = submit_review(database)
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    queue = SqlAlchemyReviewTaskQueue(database.sessions, clock=MutableClock(now))
    runtime = WorkerRuntime(
        queue,
        WorkerSettings(
            worker_id="worker-1",
            poll_interval=timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        ),
    )

    assert runtime.run_once() is True

    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, task.review_run_id)
        heartbeat = session.get(WorkerHeartbeatRecord, "worker-1")
        assert task.execution_status == ExecutionStatus.WAITING_FOR_CI.value
        assert run.execution_status == ExecutionStatus.WAITING_FOR_CI.value
        assert task.attempt_count == 1
        assert task.lease_owner is None
        assert task.lease_expires_at is None
        assert heartbeat.status == WorkerStatus.IDLE.value
        assert heartbeat.current_task_id is None
        assert (
            session.scalar(select(func.count()).select_from(OutboxEventRecord))
            == 3
        )


def test_failed_attempt_is_retried_with_backoff_and_old_lease_is_rejected(
    database: Database,
) -> None:
    """验证失败重试的退避时间和租约隔离。

    参数：
        database: 当前用例数据库。

    动作：第一次领取后上报临时失败，立即重领应为空；推进 5 秒后再次领取，再用
    第一次的旧租约尝试完成。
    预期：任务先回 queued 并保存错误，第二次尝试计数为 2，旧租约操作抛出
    ``TaskLeaseLostError``，证明旧 Worker 不能覆盖新尝试。
    """
    task_id = submit_review(database, "retry-test")
    clock = MutableClock(datetime(2026, 8, 18, 12, 0, tzinfo=UTC))
    queue = SqlAlchemyReviewTaskQueue(
        database.sessions,
        clock=clock,
        retry_base_seconds=5,
    )
    first = queue.claim_next("worker-1", timedelta(seconds=30))
    assert first is not None

    queue.retry_or_fail(first, "temporary failure")
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        assert task.execution_status == ExecutionStatus.QUEUED.value
        assert task.last_error == "temporary failure"

    assert queue.claim_next("worker-1", timedelta(seconds=30)) is None
    clock.value += timedelta(seconds=5)
    second = queue.claim_next("worker-1", timedelta(seconds=30))
    assert second is not None
    assert second.attempt_count == 2

    with pytest.raises(TaskLeaseLostError):
        queue.mark_waiting_for_ci(first)


def test_expired_final_lease_marks_task_and_run_failed(database: Database) -> None:
    """验证最后一次可用尝试租约过期后进入最终失败。

    参数：
        database: 当前用例数据库。

    前提：把任务最大尝试次数改为 1，并领取 30 秒租约。
    动作：推进 31 秒，先确认旧租约不能完成，再运行过期租约恢复。
    预期：恰好恢复一条任务，任务和运行都为 failed，最后错误明确包含租约超时。
    """
    task_id = submit_review(database, "expired-test")
    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        task.max_attempts = 1
        session.commit()

    clock = MutableClock(datetime(2026, 8, 18, 12, 0, tzinfo=UTC))
    queue = SqlAlchemyReviewTaskQueue(database.sessions, clock=clock)
    lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert lease is not None
    clock.value += timedelta(seconds=31)

    with pytest.raises(TaskLeaseLostError):
        queue.mark_waiting_for_ci(lease)

    assert queue.recover_expired_leases() == 1

    with database.sessions() as session:
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, task.review_run_id)
        assert task.execution_status == ExecutionStatus.FAILED.value
        assert run.execution_status == ExecutionStatus.FAILED.value
        assert "租约超时" in task.last_error


def test_worker_heartbeat_freshness_is_observable(database: Database) -> None:
    """验证心跳新鲜度随时间窗口变化而可观察。

    参数：
        database: 当前用例数据库。

    动作：在固定时刻记录 idle 心跳，用 15 秒窗口立即检查，再推进 16 秒检查。
    预期：结果从 True 变为 False，证明 Dashboard/容器健康检查读取的是持久化
    ``last_seen_at``，而不是只判断是否存在 Worker 行。
    """
    clock = MutableClock(datetime(2026, 8, 18, 12, 0, tzinfo=UTC))
    queue = SqlAlchemyReviewTaskQueue(database.sessions, clock=clock)
    queue.record_heartbeat("worker-1", WorkerStatus.IDLE)

    assert queue.heartbeat_is_fresh("worker-1", timedelta(seconds=15)) is True
    clock.value += timedelta(seconds=16)
    assert queue.heartbeat_is_fresh("worker-1", timedelta(seconds=15)) is False
