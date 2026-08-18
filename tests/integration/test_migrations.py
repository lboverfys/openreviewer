from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_initial_migration_creates_durable_review_task_schema(
    tmp_path: Path,
) -> None:
    """验证空数据库执行 Alembic head 后得到完整持久化 Schema。

    参数：
        tmp_path: pytest 提供的隔离目录，用于创建一次性 SQLite 文件。

    动作：加载仓库真实 ``alembic.ini``，仅覆盖当前测试数据库 URL，然后执行
    ``upgrade head`` 并通过 SQLAlchemy inspector 读取实际结构。
    预期：版本表、运行、任务、Outbox、心跳五张表都存在；幂等键和运行-任务
    一对一唯一约束名称正确；Alembic 版本号为第二个迁移 revision。

    最后无论断言是否成功都释放检查引擎，避免 Windows 文件句柄阻止临时目录清理。
    """
    database_path = (tmp_path / "migration.sqlite3").as_posix()
    database_url = f"sqlite:///{database_path}"
    configuration = Config(str(PROJECT_ROOT / "alembic.ini"))
    configuration.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(configuration, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == {
            "alembic_version",
            "outbox_events",
            "review_runs",
            "review_tasks",
            "worker_heartbeats",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("review_runs")
        } == {"uq_review_runs_idempotency_key"}
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("review_tasks")
        } == {"uq_review_tasks_review_run_id"}
        with engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "20260818_0002"
    finally:
        engine.dispose()
