from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from persistence.database import Database
from persistence.finding_downgrade import SqlAlchemyFindingDowngradePreparation

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _head_database(tmp_path: Path) -> tuple[Config, str]:
    database_url = f"sqlite:///{(tmp_path / 'finding-downgrade.sqlite3').as_posix()}"
    configuration = Config(str(PROJECT_ROOT / "alembic.ini"))
    configuration.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(configuration, "head")
    return configuration, database_url


def _seed_incompatible_data(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO review_findings ("
                    "id, review_run_id, review_plan_id, model_call_id, "
                    "source_unit_key, fingerprint, head_sha, severity, category, "
                    "location_in_diff, title, evidence, impact, suggestion, "
                    "confidence, evidence_verification_status, "
                    "evidence_verification_reason, evidence_verified_at, "
                    "verification_status, adjudication_status, "
                    "lifecycle_status, occurrence_count, created_at"
                    ") VALUES ("
                    "'downgrade-finding', 'missing-run', 'missing-plan', "
                    "'missing-call', :source_key, :fingerprint, :head_sha, "
                    "'high', 'security', 0, 'title', 'evidence', 'impact', "
                    "'suggestion', 0.9, 'unverified', 'legacy_not_reverified', "
                    "NULL, 'unverified', 'duplicate', 'new', 1, "
                    ":created_at)"
                ),
                {
                    "source_key": "a" * 64,
                    "fingerprint": "b" * 64,
                    "head_sha": "c" * 40,
                    "created_at": datetime(2026, 8, 28, tzinfo=UTC).isoformat(),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO finding_evaluations ("
                    "finding_id, repository_id, category, severity, verdict, "
                    "adjudicated_at, adjudicated_by, updated_at"
                    ") VALUES ("
                    "'downgrade-finding', 42, 'security', 'high', 'duplicate', "
                    ":timestamp, 'reviewer', :timestamp)"
                ),
                {"timestamp": datetime(2026, 8, 28, tzinfo=UTC).isoformat()},
            )
    finally:
        engine.dispose()


def test_finding_downgrade_requires_bounded_preparation(tmp_path: Path) -> None:
    configuration, database_url = _head_database(tmp_path)
    _seed_incompatible_data(database_url)

    with pytest.raises(RuntimeError, match="Finding 裁决数据尚未准备完成"):
        command.downgrade(configuration, "20260828_0025")

    database = Database.connect(database_url)
    try:
        preparation = SqlAlchemyFindingDowngradePreparation(database.sessions)
        batch = preparation.run_batch(1)
        assert batch.findings == 1
        assert batch.evaluations == 1
        assert not preparation.run_batch(1).changed
    finally:
        database.dispose()

    command.downgrade(configuration, "20260828_0025")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "adjudication_status" not in {
            column["name"] for column in inspector.get_columns("review_findings")
        }
        with engine.connect() as connection:
            verification_status = connection.scalar(
                text(
                    "SELECT verification_status FROM review_findings "
                    "WHERE id = 'downgrade-finding'"
                )
            )
            verdict = connection.scalar(
                text(
                    "SELECT verdict FROM finding_evaluations "
                    "WHERE finding_id = 'downgrade-finding'"
                )
            )
        assert verification_status == "rejected"
        assert verdict == "false_positive"
    finally:
        engine.dispose()
