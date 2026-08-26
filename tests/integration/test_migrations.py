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
    一对一唯一约束名称正确；Alembic 版本号为当前最新迁移 revision。

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
            "ai_provider_configs",
            "ai_provider_secrets",
            "ai_settings",
            "alembic_version",
            "configuration_audits",
            "external_actions",
            "github_installations",
            "github_webhook_deliveries",
            "model_calls",
            "outbox_events",
            "pull_request_ci_checks",
            "pull_request_files",
            "pull_request_versions",
            "review_file_plans",
            "review_findings",
            "review_plan_rules",
            "review_plans",
            "review_runs",
            "review_tasks",
            "review_units",
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
        task_columns = {
            column["name"] for column in inspector.get_columns("review_tasks")
        }
        assert {
            "last_error_code",
            "last_error_retryable",
            "last_error_details",
            "ci_wait_started_at",
            "ci_deadline_at",
            "ci_poll_count",
            "claimed_from_status",
            "model_attempt_count",
        } <= task_columns
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                "pull_request_versions"
            )
        } == {"uq_pull_request_versions_review_version_key"}
        assert "ix_review_tasks_expired_lease" in {
            index["name"] for index in inspector.get_indexes("review_tasks")
        }
        assert "ix_outbox_events_aggregate_occurred" in {
            index["name"] for index in inspector.get_indexes("outbox_events")
        }
        assert {
            "ix_review_runs_created_at",
            "ix_review_runs_execution_status",
            "ix_review_runs_repository_pr_status",
        } <= {index["name"] for index in inspector.get_indexes("review_runs")}
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("review_runs")
        } == {
            "ck_review_runs_coverage_status_value",
            "ck_review_runs_execution_status_value",
            "ck_review_runs_installation_id_positive",
            "ck_review_runs_pull_request_number_positive",
            "ck_review_runs_repository_id_positive",
            "ck_review_runs_review_conclusion_value",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("review_tasks")
        } == {
            "ck_review_tasks_attempt_count_nonnegative",
            "ck_review_tasks_execution_status_value",
            "ck_review_tasks_max_attempts_positive",
            "ck_review_tasks_ci_poll_count_nonnegative",
            "ck_review_tasks_claimed_from_status_value",
            "ck_review_tasks_model_attempt_count_nonnegative",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("outbox_events")
        } == {"ck_outbox_events_publish_attempts_nonnegative"}
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints(
                "worker_heartbeats"
            )
        } == {"ck_worker_heartbeats_status_value"}
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints(
                "pull_request_versions"
            )
        } == {
            "ck_pull_request_versions_changed_files_count_nonnegative",
            "ck_pull_request_versions_ci_state_value",
            "ck_pull_request_versions_installation_id_positive",
            "ck_pull_request_versions_pr_state_value",
            "ck_pull_request_versions_pull_request_number_positive",
            "ck_pull_request_versions_repository_id_positive",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("review_plans")
        } == {"uq_review_plans_review_run_id"}
        assert "model_review_completed_at" in {
            column["name"] for column in inspector.get_columns("review_plans")
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("model_calls")
        } == {"uq_model_calls_review_plan_id"}
        assert "configuration_revision" in {
            column["name"] for column in inspector.get_columns("model_calls")
        }
        assert "api_protocol" in {
            column["name"] for column in inspector.get_columns("model_calls")
        }
        assert "api_protocol" in {
            column["name"]
            for column in inspector.get_columns("ai_provider_configs")
        }
        assert "api_base_url" in {
            column["name"]
            for column in inspector.get_columns("ai_provider_configs")
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("ai_settings")
        } == {"uq_ai_settings_revision"}
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("review_findings")
        } == {"uq_review_findings_run_fingerprint"}
        assert {"reviewed_at", "reviewed_by"} <= {
            column["name"] for column in inspector.get_columns("review_findings")
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints(
                "review_file_plans"
            )
        } == {
            "ck_review_file_plans_decision_unit_consistency",
            "ck_review_file_plans_decision_value",
            "ck_review_file_plans_ordinal_nonnegative",
        }
        assert revision == "20260826_0010"
    finally:
        engine.dispose()

    command.downgrade(configuration, "20260824_0003")
    command.upgrade(configuration, "head")
    command.check(configuration)


def test_postgres_migration_keeps_execution_constraint_names_fixed(
    capsys,
) -> None:
    """验证 PostgreSQL 离线迁移不会再次生成重复表名前缀。"""
    configuration = Config(str(PROJECT_ROOT / "alembic.ini"))
    configuration.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://openreviewer_test:ci-only-postgres-password"
        "@127.0.0.1:5432/openreviewer_test",
    )

    command.upgrade(configuration, "head", sql=True)

    output = capsys.readouterr().out
    assert (
        "ALTER TABLE review_runs DROP CONSTRAINT "
        "ck_review_runs_execution_status_value"
    ) in output
    assert (
        "ALTER TABLE review_tasks DROP CONSTRAINT "
        "ck_review_tasks_execution_status_value"
    ) in output
    assert "DROP CONSTRAINT ck_review_runs_ck_review_runs_" not in output
    assert "DROP CONSTRAINT ck_review_tasks_ck_review_tasks_" not in output


def test_protocol_migration_backfills_existing_provider_configs(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "protocol-backfill.sqlite3").as_posix()
    database_url = f"sqlite:///{database_path}"
    configuration = Config(str(PROJECT_ROOT / "alembic.ini"))
    configuration.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(configuration, "20260825_0007")

    engine = create_engine(database_url)
    try:
        values = {
            "max_output_tokens": 8192,
            "connect_timeout_seconds": 5,
            "read_timeout_seconds": 180,
            "write_timeout_seconds": 30,
            "pool_timeout_seconds": 5,
            "max_request_bytes": 4194304,
            "max_response_bytes": 2097152,
            "updated_by": "migration-test",
            "updated_at": "2026-08-25 00:00:00",
        }
        with engine.begin() as connection:
            for provider, model in (
                ("openai", "gpt-test"),
                ("anthropic", "claude-test"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO ai_provider_configs ("
                        "provider, model, max_output_tokens, "
                        "connect_timeout_seconds, read_timeout_seconds, "
                        "write_timeout_seconds, pool_timeout_seconds, "
                        "max_request_bytes, max_response_bytes, updated_by, updated_at"
                        ") VALUES ("
                        ":provider, :model, :max_output_tokens, "
                        ":connect_timeout_seconds, :read_timeout_seconds, "
                        ":write_timeout_seconds, :pool_timeout_seconds, "
                        ":max_request_bytes, :max_response_bytes, :updated_by, :updated_at"
                        ")"
                    ),
                    {**values, "provider": provider, "model": model},
                )
    finally:
        engine.dispose()

    command.upgrade(configuration, "head")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT provider, api_protocol, api_base_url "
                    "FROM ai_provider_configs"
                )
            ).all()
            protocols = {row.provider: row.api_protocol for row in rows}
            api_base_urls = {row.provider: row.api_base_url for row in rows}
        assert protocols == {
            "openai": "responses",
            "anthropic": "messages",
        }
        assert api_base_urls == {
            "openai": None,
            "anthropic": None,
        }
    finally:
        engine.dispose()
