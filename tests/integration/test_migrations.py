from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from services.operations import EXPECTED_DATABASE_REVISION

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
    expected_revision = ScriptDirectory.from_config(configuration).get_current_head()
    assert expected_revision == EXPECTED_DATABASE_REVISION

    command.upgrade(configuration, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == {
            "admin_sessions",
            "ai_provider_configs",
            "ai_provider_secrets",
            "ai_agent_configs",
            "ai_agent_secrets",
            "ai_settings",
            "alembic_version",
            "configuration_audits",
            "external_actions",
            "finding_evaluations",
            "finding_lifecycles",
            "github_installations",
            "github_webhook_deliveries",
            "knowledge_document_versions",
            "knowledge_documents",
            "knowledge_library",
            "login_rate_limits",
            "model_calls",
            "model_http_calls",
            "model_review_batches",
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
            "review_quota_buckets",
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
        assert {
            "author_login",
            "html_url",
            "head_repository",
            "head_ref",
            "base_repository",
            "base_ref",
            "identity_fetched_at",
        } <= {
            column["name"]
            for column in inspector.get_columns("pull_request_versions")
        }
        assert "ix_review_tasks_expired_lease" in {
            index["name"] for index in inspector.get_indexes("review_tasks")
        }
        assert "ix_outbox_events_aggregate_occurred" in {
            index["name"] for index in inspector.get_indexes("outbox_events")
        }
        assert {
            "publish_lease_owner",
            "publish_lease_expires_at",
            "next_publish_attempt_at",
            "last_publish_error",
        } <= {
            column["name"] for column in inspector.get_columns("outbox_events")
        }
        assert {
            "ix_review_runs_created_at",
            "ix_review_runs_execution_status",
            "ix_review_runs_repository_pr_status",
            "ix_review_runs_status_created",
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
            "ck_review_runs_workflow_paused_from_value",
            "ck_review_runs_workflow_status_value",
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
            "ck_review_tasks_workflow_paused_from_value",
            "ck_review_tasks_workflow_status_value",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("outbox_events")
        } == {
            "ck_outbox_events_publish_attempts_nonnegative",
            "ck_outbox_events_publish_lease_shape",
        }
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
        assert "group_key" in {
            column["name"] for column in inspector.get_columns("review_units")
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
        assert "context_window_tokens" in {
            column["name"]
            for column in inspector.get_columns("ai_provider_configs")
        }
        assert {"reasoning_effort", "max_batch_input_tokens"} <= {
            column["name"]
            for column in inspector.get_columns("ai_provider_configs")
        }
        assert {
            "ck_ai_provider_configs_reasoning_effort_value",
            "ck_ai_provider_configs_max_batch_input_tokens_range",
        } <= {
            constraint["name"]
            for constraint in inspector.get_check_constraints(
                "ai_provider_configs"
            )
        }
        assert {
            "workflow_status",
            "publish_attempt_token",
        } <= {
            column["name"] for column in inspector.get_columns("review_runs")
        }
        assert {
            "lease_owner",
            "lease_expires_at",
        } <= {
            column["name"] for column in inspector.get_columns("external_actions")
        }
        assert "ix_external_actions_claimable" in {
            index["name"] for index in inspector.get_indexes("external_actions")
        }
        assert "ck_external_actions_lease_shape" in {
            constraint["name"]
            for constraint in inspector.get_check_constraints("external_actions")
        }
        assert {
            "workflow_status",
        } <= {
            column["name"] for column in inspector.get_columns("review_tasks")
        }
        assert {
            "agent",
            "provider",
            "model",
            "api_protocol",
            "max_batch_input_tokens",
            "enabled",
            "test_status",
        } <= {
            column["name"] for column in inspector.get_columns("ai_agent_configs")
        }
        assert {"agent", "ciphertext", "nonce", "key_version"} <= {
            column["name"] for column in inspector.get_columns("ai_agent_secrets")
        }
        assert {
            "ix_ai_agent_configs_enabled",
        } <= {
            index["name"] for index in inspector.get_indexes("ai_agent_configs")
        }
        assert {
            "ix_model_review_batches_claimable",
            "ix_model_review_batches_plan_agent",
        } <= {
            index["name"] for index in inspector.get_indexes("model_review_batches")
        }
        assert {
            "uq_model_review_batches_plan_agent_number",
        } == {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("model_review_batches")
        }
        assert {
            "ck_model_review_batches_attempt_count_nonnegative",
            "ck_model_review_batches_batch_count_positive",
            "ck_model_review_batches_batch_number_positive",
            "ck_model_review_batches_batch_number_within_count",
            "ck_model_review_batches_duration_ms_nonnegative",
            "ck_model_review_batches_estimated_input_tokens_nonnegative",
            "ck_model_review_batches_response_status_range",
            "ck_model_review_batches_status_value",
        } == {
            constraint["name"]
            for constraint in inspector.get_check_constraints(
                "model_review_batches"
            )
        }
        assert "ck_ai_agent_configs_reasoning_effort_value" in {
            constraint["name"]
            for constraint in inspector.get_check_constraints("ai_agent_configs")
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("ai_settings")
        } == {"uq_ai_settings_revision"}
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("review_findings")
        } == {"uq_review_findings_run_fingerprint"}
        assert {
            "reviewed_at",
            "reviewed_by",
            "lifecycle_status",
            "occurrence_count",
            "previous_review_run_id",
            "lifecycle_backfilled_at",
            "evidence_verification_status",
            "evidence_verification_reason",
            "evidence_verified_at",
        } <= {
            column["name"] for column in inspector.get_columns("review_findings")
        }
        assert "historical_backfilled_at" in {
            column["name"]
            for column in inspector.get_columns("finding_lifecycles")
        }
        assert "ix_review_findings_lifecycle_backfill" in {
            index["name"]
            for index in inspector.get_indexes("review_findings")
        }
        assert {
            "ix_finding_lifecycles_pr_state",
            "ix_finding_lifecycles_fixed_run",
        } == {
            index["name"]
            for index in inspector.get_indexes("finding_lifecycles")
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
        assert {
            "ix_knowledge_documents_active_source",
            "ix_knowledge_documents_updated_at",
        } <= {
            index["name"]
            for index in inspector.get_indexes("knowledge_documents")
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                "knowledge_document_versions"
            )
        } == {"uq_knowledge_document_versions_document_id"}
        assert {
            "session_hash",
            "username",
            "role",
            "issued_at",
            "expires_at",
            "revoked_at",
        } == {
            column["name"] for column in inspector.get_columns("admin_sessions")
        }
        assert {
            "ix_admin_sessions_active",
            "ix_admin_sessions_expires_at",
        } == {
            index["name"] for index in inspector.get_indexes("admin_sessions")
        }
        assert {
            "key_hash",
            "attempt_count",
            "window_started_at",
            "updated_at",
        } == {
            column["name"]
            for column in inspector.get_columns("login_rate_limits")
        }
        assert {"ix_login_rate_limits_updated_at"} == {
            index["name"]
            for index in inspector.get_indexes("login_rate_limits")
        }
        assert {
            "max_model_http_calls",
            "max_model_input_tokens",
            "max_model_output_tokens",
            "max_model_cost_microusd",
            "max_model_duration_seconds",
        } <= {
            column["name"] for column in inspector.get_columns("ai_settings")
        }
        assert {
            "max_model_http_calls",
            "max_model_input_tokens",
            "max_model_output_tokens",
            "max_model_cost_microusd",
            "max_model_duration_seconds",
            "model_http_calls",
            "model_input_tokens",
            "model_output_tokens",
            "model_estimated_cost_microusd",
            "model_budget_resume_count",
            "model_budget_started_at",
            "model_budget_exhausted_at",
            "model_budget_exhausted_reason",
        } <= {
            column["name"] for column in inspector.get_columns("review_plans")
        }
        assert "instance_id" in {
            column["name"]
            for column in inspector.get_columns("worker_heartbeats")
        }
        assert "repository_key" in {
            column["name"] for column in inspector.get_columns("review_runs")
        }
        assert "ix_review_runs_installation_repository_key" in {
            index["name"] for index in inspector.get_indexes("review_runs")
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("model_http_calls")
        } == {"uq_model_http_calls_plan_sequence"}
        assert "ix_model_http_calls_plan_started" in {
            index["name"] for index in inspector.get_indexes("model_http_calls")
        }
        assert "adjudication_status" in {
            column["name"] for column in inspector.get_columns("review_findings")
        }
        assert "ix_review_findings_run_adjudication" in {
            index["name"] for index in inspector.get_indexes("review_findings")
        }
        assert "ix_review_findings_run_evidence_verification" in {
            index["name"] for index in inspector.get_indexes("review_findings")
        }
        assert "ck_review_findings_evidence_verification_status_value" in {
            constraint["name"]
            for constraint in inspector.get_check_constraints("review_findings")
        }
        assert "ix_review_quota_buckets_cleanup" in {
            index["name"]
            for index in inspector.get_indexes("review_quota_buckets")
        }
        assert "ix_finding_evaluations_adjudicated_cleanup" in {
            index["name"]
            for index in inspector.get_indexes("finding_evaluations")
        }
        assert {
            "ck_review_quota_buckets_scope_value",
            "ck_review_quota_buckets_window_value",
            "ck_review_quota_buckets_request_count_nonnegative",
        } <= {
            constraint["name"]
            for constraint in inspector.get_check_constraints(
                "review_quota_buckets"
            )
        }
        assert "ix_review_runs_created_id" in {
            index["name"] for index in inspector.get_indexes("review_runs")
        }
        assert "ix_outbox_events_occurred_id" in {
            index["name"] for index in inspector.get_indexes("outbox_events")
        }
        assert revision == expected_revision
    finally:
        engine.dispose()

    command.downgrade(configuration, "20260824_0003")
    command.upgrade(configuration, "head")
    command.check(configuration)


def test_finding_evaluation_samples_survive_missing_findings(
    tmp_path: Path,
) -> None:
    """评测样本允许引用已按保留期删除的 Finding。"""

    database_path = (tmp_path / "finding-evaluation-retention.sqlite3").as_posix()
    database_url = f"sqlite:///{database_path}"
    configuration = Config(str(PROJECT_ROOT / "alembic.ini"))
    configuration.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(configuration, "head")

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            foreign_keys = inspect(connection).get_foreign_keys("finding_evaluations")
            assert all(
                item["name"] != "fk_finding_evaluations_finding"
                for item in foreign_keys
            )
            connection.execute(
                text(
                    "INSERT INTO finding_evaluations ("
                    "finding_id, repository_id, category, severity, verdict, "
                    "adjudicated_at, adjudicated_by, updated_at"
                    ") VALUES ("
                    ":finding_id, :repository_id, :category, :severity, :verdict, "
                    ":adjudicated_at, :adjudicated_by, :updated_at"
                    ")"
                ),
                {
                    "finding_id": "retained-after-cleanup",
                    "repository_id": 42,
                    "category": "security",
                    "severity": "high",
                    "verdict": "valid",
                    "adjudicated_at": "2026-08-20 00:00:00",
                    "adjudicated_by": "reviewer",
                    "updated_at": "2026-08-20 00:00:00",
                },
            )
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM finding_evaluations "
                    "WHERE finding_id = 'retained-after-cleanup'"
                )
            ) == 1
    finally:
        engine.dispose()


def test_finding_evaluation_fk_downgrade_refuses_dangling_samples(
    tmp_path: Path,
) -> None:
    """降级不能悄悄恢复级联外键并丢弃历史评测引用。"""

    database_path = (tmp_path / "finding-evaluation-downgrade.sqlite3").as_posix()
    database_url = f"sqlite:///{database_path}"
    configuration = Config(str(PROJECT_ROOT / "alembic.ini"))
    configuration.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(configuration, "head")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO finding_evaluations ("
                    "finding_id, repository_id, category, severity, verdict, "
                    "adjudicated_at, adjudicated_by, updated_at"
                    ") VALUES ("
                    ":finding_id, 42, 'security', 'high', 'valid', "
                    "'2026-08-20 00:00:00', 'reviewer', '2026-08-20 00:00:00'"
                    ")"
                ),
                {"finding_id": "missing-finding"},
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="historical rows"):
        command.downgrade(configuration, "20260830_0033")


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
    assert (
        "ALTER TABLE finding_evaluations DROP CONSTRAINT "
        "fk_finding_evaluations_finding"
    ) in output
    assert (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_finding_evaluations_adjudicated_cleanup"
    ) in output
    assert (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_review_findings_run_adjudication"
    ) in output
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_review_runs_created_id" in output
    assert (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_outbox_events_occurred_id"
        in output
    )
    assert (
        "ck_review_findings_lifecycle_status_value "
        "CHECK (lifecycle_status IN ('new', 'still_present', 'reintroduced'))"
    ) in output
    assert (
        "ck_review_findings_occurrence_count_positive "
        "CHECK (occurrence_count > 0)"
    ) in output
    assert (
        "ck_review_findings_adjudication_status_value "
        "CHECK (adjudication_status IN ("
    ) in output
    assert "NOT VALID" not in output


def test_workflow_downgrade_restores_legacy_execution_constraints(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "workflow-downgrade.sqlite3").as_posix()
    database_url = f"sqlite:///{database_path}"
    configuration = Config(str(PROJECT_ROOT / "alembic.ini"))
    configuration.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(configuration, "head")
    command.downgrade(configuration, "20260827_0013")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        for table in ("review_runs", "review_tasks"):
            assert "workflow_status" not in {
                column["name"] for column in inspector.get_columns(table)
            }
            execution_constraint = next(
                constraint
                for constraint in inspector.get_check_constraints(table)
                if constraint["name"] == f"ck_{table}_execution_status_value"
            )
            sql = execution_constraint["sqltext"]
            assert "ready_for_review" in sql
            assert "aggregating" not in sql
            assert "awaiting_approval" not in sql
    finally:
        engine.dispose()


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
                ("openai", "deepseek-ai/deepseek-v4-flash"),
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
                    "SELECT provider, api_protocol, api_base_url, "
                    "context_window_tokens, reasoning_effort, "
                    "max_batch_input_tokens "
                    "FROM ai_provider_configs"
                )
            ).all()
            protocols = {row.provider: row.api_protocol for row in rows}
            api_base_urls = {row.provider: row.api_base_url for row in rows}
            context_windows = {
                row.provider: row.context_window_tokens for row in rows
            }
            reasoning_efforts = {
                row.provider: row.reasoning_effort for row in rows
            }
            batch_limits = {
                row.provider: row.max_batch_input_tokens for row in rows
            }
        assert protocols == {
            "openai": "responses",
            "anthropic": "messages",
        }
        assert api_base_urls == {
            "openai": None,
            "anthropic": None,
        }
        assert context_windows == {
            "openai": 1_000_000,
            "anthropic": 128_000,
        }
        assert reasoning_efforts == {"openai": "none", "anthropic": "none"}
        assert batch_limits == {"openai": 64_000, "anthropic": 64_000}
    finally:
        engine.dispose()


def test_response_limit_migration_invalidates_old_provider_tests(
    tmp_path: Path,
) -> None:
    """放宽响应上限后，旧连接测试指纹必须重新验证。"""

    database_path = (tmp_path / "response-limit.sqlite3").as_posix()
    database_url = f"sqlite:///{database_path}"
    configuration = Config(str(PROJECT_ROOT / "alembic.ini"))
    configuration.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(configuration, "20260831_0040")

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO ai_provider_configs ("
                    "provider, model, api_protocol, context_window_tokens, "
                    "reasoning_effort, "
                    "max_output_tokens, max_batch_input_tokens, "
                    "connect_timeout_seconds, read_timeout_seconds, "
                    "write_timeout_seconds, pool_timeout_seconds, "
                    "max_request_bytes, max_response_bytes, "
                    "tested_configuration_fingerprint, test_status, tested_at, "
                    "updated_by, updated_at"
                    ") VALUES ("
                    "'openai', 'test-model', 'responses', 128000, 'none', 8192, 64000, "
                    "5, 180, 30, 5, 4194304, 2097152, "
                    "printf('%064d', 0), 'succeeded', "
                    "'2026-08-31 00:00:00', 'migration-test', "
                    "'2026-08-31 00:00:00'"
                    ")"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(configuration, "head")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT max_response_bytes, tested_configuration_fingerprint, "
                    "test_status, tested_at FROM ai_provider_configs "
                    "WHERE provider = 'openai'"
                )
            ).one()
        assert row.max_response_bytes == 16 * 1024 * 1024
        assert row.tested_configuration_fingerprint is None
        assert row.test_status is None
        assert row.tested_at is None
    finally:
        engine.dispose()


def test_model_budget_mode_migration_backfills_legacy_plans(
    tmp_path: Path,
) -> None:
    """预算改为观测模式后，历史计划不能因 NULL 继续硬阻断。"""

    database_path = (tmp_path / "model-budget-mode.sqlite3").as_posix()
    database_url = f"sqlite:///{database_path}"
    configuration = Config(str(PROJECT_ROOT / "alembic.ini"))
    configuration.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(configuration, "20260830_0039")

    # 在 0040 之前创建一条最小但完整的旧计划快照。外键默认未开启的
    # SQLite 迁移测试只需满足 review_plans 自身的非空/检查约束；真实
    # PostgreSQL 上 0040 的 UPDATE 语义相同，并按主键逐行回填 NULL。
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO review_plans ("
                    "id, review_run_id, pull_request_version_id, "
                    "review_version_key, head_sha, plan_fingerprint, "
                    "planner_version, rules_complete, incomplete_files, "
                    "rule_issues, candidate_count, requested_candidate_count, "
                    "rule_count, unit_count, file_count, "
                    "total_estimated_input_bytes, max_model_http_calls, "
                    "max_model_input_tokens, max_model_output_tokens, "
                    "max_model_cost_microusd, max_model_duration_seconds, "
                    "model_http_calls, model_input_tokens, model_output_tokens, "
                    "model_estimated_cost_microusd, model_budget_resume_count"
                    ") VALUES ("
                    "'legacy-plan', 'legacy-run', 'legacy-version', "
                    "'42:48:cccccccccccccccccccccccccccccccccccccccc', "
                    "'cccccccccccccccccccccccccccccccccccccccc', "
                    "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
                    "'review-planner-v3', 1, '[]', '[]', 0, 0, 0, 0, 0, 0, "
                    "64, 2000000, 250000, NULL, 900, 0, 0, 0, 0, 0"
                    ")"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(configuration, "20260831_0040")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT model_budget_mode, plan_fingerprint FROM review_plans "
                    "WHERE id = 'legacy-plan'"
                )
            ).one()
        assert row.model_budget_mode == "observe"
        # 模式回填改变执行策略，不改变已经持久化并被批次/结果引用的计划身份。
        assert row.plan_fingerprint == "a" * 64
    finally:
        engine.dispose()


def test_runtime_guard_migration_upgrades_legacy_defaults(
    tmp_path: Path,
) -> None:
    """运行保护迁移提升旧默认，同时保留其他显式时限。"""

    database_path = (tmp_path / "runtime-guard-default.sqlite3").as_posix()
    database_url = f"sqlite:///{database_path}"
    configuration = Config(str(PROJECT_ROOT / "alembic.ini"))
    configuration.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(configuration, "20260831_0041")

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO ai_settings ("
                    "id, revision, max_units, max_scope_depth, "
                    "max_unit_input_bytes, max_total_input_bytes, "
                    "max_model_http_calls, max_model_input_tokens, "
                    "max_model_output_tokens, max_model_cost_microusd, "
                    "max_model_duration_seconds, updated_at"
                    ") VALUES ("
                    "1, 0, 100, 32, 196608, 2097152, 64, 2000000, "
                    "250000, NULL, 900, '2026-08-31 00:00:00'"
                    ")"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO review_plans ("
                    "id, review_run_id, pull_request_version_id, "
                    "review_version_key, head_sha, plan_fingerprint, "
                    "planner_version, rules_complete, incomplete_files, "
                    "rule_issues, candidate_count, requested_candidate_count, "
                    "rule_count, unit_count, file_count, "
                    "total_estimated_input_bytes, max_model_http_calls, "
                    "max_model_input_tokens, max_model_output_tokens, "
                    "max_model_cost_microusd, max_model_duration_seconds, "
                    "model_http_calls, model_input_tokens, model_output_tokens, "
                    "model_estimated_cost_microusd, model_budget_resume_count, "
                    "model_budget_mode"
                    ") VALUES ("
                    ":id, :run_id, :version_id, :version_key, :head_sha, "
                    ":fingerprint, 'review-planner-v3', 1, '[]', '[]', "
                    "0, 0, 0, 0, 0, 0, 64, 2000000, 250000, NULL, "
                    ":duration, 0, 0, 0, 0, 0, :mode"
                    ")"
                ),
                [
                    {
                        "id": "legacy-runtime-plan",
                        "run_id": "legacy-runtime-run",
                        "version_id": "legacy-runtime-version",
                        "version_key": "42:49:" + "d" * 40,
                        "head_sha": "d" * 40,
                        "fingerprint": "b" * 64,
                        "duration": 900,
                        "mode": "observe",
                    },
                    {
                        "id": "custom-runtime-plan",
                        "run_id": "custom-runtime-run",
                        "version_id": "custom-runtime-version",
                        "version_key": "42:50:" + "e" * 40,
                        "head_sha": "e" * 40,
                        "fingerprint": "c" * 64,
                        "duration": 1200,
                        "mode": "observe",
                    },
                    {
                        "id": "enforced-runtime-plan",
                        "run_id": "enforced-runtime-run",
                        "version_id": "enforced-runtime-version",
                        "version_key": "42:51:" + "f" * 40,
                        "head_sha": "f" * 40,
                        "fingerprint": "d" * 64,
                        "duration": 900,
                        "mode": "enforce",
                    },
                ],
            )
    finally:
        engine.dispose()

    command.upgrade(configuration, "head")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        ai_settings_duration = next(
            column
            for column in inspector.get_columns("ai_settings")
            if column["name"] == "max_model_duration_seconds"
        )
        review_plan_duration = next(
            column
            for column in inspector.get_columns("review_plans")
            if column["name"] == "max_model_duration_seconds"
        )
        with engine.connect() as connection:
            current_value = connection.scalar(
                text(
                    "SELECT max_model_duration_seconds FROM ai_settings "
                    "WHERE id = 1"
                )
            )
            plan_values = dict(
                connection.execute(
                    text(
                        "SELECT id, max_model_duration_seconds FROM review_plans "
                        "WHERE id IN ('legacy-runtime-plan', 'custom-runtime-plan', "
                        "'enforced-runtime-plan')"
                    )
                ).all()
            )
        assert current_value == 3600
        assert plan_values == {
            "legacy-runtime-plan": 3600,
            "custom-runtime-plan": 1200,
            "enforced-runtime-plan": 900,
        }
        assert str(ai_settings_duration["default"]).strip("()'") == "3600"
        assert str(review_plan_duration["default"]).strip("()'") == "3600"
    finally:
        engine.dispose()

    command.downgrade(configuration, "20260831_0041")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        defaults = {
            table: str(
                next(
                    column
                    for column in inspector.get_columns(table)
                    if column["name"] == "max_model_duration_seconds"
                )["default"]
            ).strip("()'")
            for table in ("ai_settings", "review_plans")
        }
        with engine.begin() as connection:
            persisted_value = connection.scalar(
                text(
                    "SELECT max_model_duration_seconds FROM ai_settings "
                    "WHERE id = 1"
                )
            )
            connection.execute(
                text(
                    "UPDATE ai_settings SET max_model_duration_seconds = 1200 "
                    "WHERE id = 1"
                )
            )
        assert defaults == {"ai_settings": "900", "review_plans": "900"}
        assert persisted_value == 3600
    finally:
        engine.dispose()

    # 再次升级时，自定义为 1200 秒的全局配置不能被当成历史默认覆盖。
    command.upgrade(configuration, "head")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            custom_setting = connection.scalar(
                text(
                    "SELECT max_model_duration_seconds FROM ai_settings "
                    "WHERE id = 1"
                )
            )
        assert custom_setting == 1200
    finally:
        engine.dispose()
