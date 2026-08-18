from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_initial_migration_creates_durable_review_task_schema(
    tmp_path: Path,
) -> None:
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
