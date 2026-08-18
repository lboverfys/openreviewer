"""Container healthcheck for the database worker heartbeat."""

from datetime import timedelta
import os

from apps.worker.main import WorkerSettings
from persistence.database import Database
from persistence.task_queue import SqlAlchemyReviewTaskQueue


def main() -> None:
    """检查当前 Worker 的数据库心跳是否在允许窗口内。

    容器编排会周期性调用这个入口；配置解析或数据库连接失败会直接使健康检查
    失败，心跳超过四个轮询周期（且至少 15 秒）也会被视为失联。最后无论检查
    成功与否都释放数据库连接。

    执行步骤：
        1. 读取与 Worker 主进程相同的 ID 和轮询配置；
        2. 创建一个短生命周期数据库队列实例；
        3. 以 ``max(15 秒, 4 * poll_interval)`` 计算允许的最大心跳年龄；
        4. 查询同一 ``worker_id`` 的心跳，过旧时以非零退出码结束；
        5. 在 ``finally`` 中释放连接池。

    该检查只判断心跳新鲜度，不执行任务领取、恢复或状态推进，也不会修改业务表。
    """
    settings = WorkerSettings.from_environment()
    database = Database.from_environment()
    try:
        queue = SqlAlchemyReviewTaskQueue(database.sessions)
        maximum_age = timedelta(
            seconds=max(15, settings.poll_interval.total_seconds() * 4)
        )
        if not queue.heartbeat_is_fresh(settings.worker_id, maximum_age):
            raise SystemExit("worker heartbeat is stale")
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
