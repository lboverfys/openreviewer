from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from domain.enums import (
    FindingCategory,
    LocationSide,
    ModelApiProtocol,
    ModelCallStatus,
    ModelProvider,
    Severity,
)
from persistence.database import Database
from persistence.finding_backfill import SqlAlchemyFindingBackfill
from persistence.models import (
    Base,
    FindingEvaluationRecord,
    FindingLifecycleRecord,
    ModelCallRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
)


def _seed_finding(
    database: Database,
    *,
    sequence: int,
    created_at: datetime,
    verification_status: str,
    location_in_diff: bool,
) -> tuple[str, str]:
    run_id = f"backfill-run-{sequence}"
    plan_id = f"backfill-plan-{sequence}"
    call_id = f"backfill-call-{sequence}"
    finding_id = f"backfill-finding-{sequence}"
    head_sha = str(sequence) * 40
    with database.sessions.begin() as session:
        session.add(
            ReviewRunRecord(
                id=run_id,
                review_version_key=f"42:128:{head_sha}",
                installation_id=10,
                repository_id=42,
                repository="lboverfys/NiuMa",
                pull_request_number=128,
                head_sha=head_sha,
                execution_status="completed",
                workflow_status="completed",
                review_conclusion="findings_present",
                coverage_status="complete",
                idempotency_key=f"backfill-source-{sequence}",
                request_fingerprint=str(sequence) * 64,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.add(
            ReviewPlanRecord(
                id=plan_id,
                review_run_id=run_id,
                pull_request_version_id=f"missing-version-{sequence}",
                review_version_key=f"42:128:{head_sha}",
                head_sha=head_sha,
                plan_fingerprint=str(sequence + 2) * 64,
                planner_version="backfill-test",
                rules_complete=True,
                incomplete_files=[],
                rule_issues=[],
                candidate_count=1,
                requested_candidate_count=1,
                rule_count=0,
                unit_count=1,
                file_count=1,
                total_estimated_input_bytes=10,
                model_review_completed_at=created_at,
                created_at=created_at,
            )
        )
        session.add(
            ModelCallRecord(
                id=call_id,
                review_plan_id=plan_id,
                configuration_revision=1,
                provider=ModelProvider.OPENAI.value,
                api_protocol=ModelApiProtocol.RESPONSES.value,
                model="backfill-test",
                status=ModelCallStatus.SUCCEEDED.value,
                prompt_version="backfill-test",
                request_fingerprint=str(sequence + 4) * 64,
                response_status=200,
                duration_ms=1,
                input_tokens=1,
                output_tokens=1,
                cache_read_input_tokens=0,
                cache_write_input_tokens=0,
                reasoning_output_tokens=0,
                estimated_cost_microusd=0,
                finding_count=1,
                created_at=created_at,
            )
        )
        has_location = sequence == 2
        session.add(
            ReviewFindingRecord(
                id=finding_id,
                review_run_id=run_id,
                review_plan_id=plan_id,
                model_call_id=call_id,
                source_unit_key="a" * 64,
                fingerprint="f" * 64,
                head_sha=head_sha,
                severity=Severity.HIGH.value,
                category=FindingCategory.SECURITY.value,
                location_file="src/app.py" if has_location else None,
                location_blob_sha="b" * 40 if has_location else None,
                location_start_line=4 if has_location else None,
                location_end_line=4 if has_location else None,
                location_side=LocationSide.RIGHT.value if has_location else None,
                location_in_diff=location_in_diff,
                location_symbol=None,
                title="历史问题",
                evidence="历史证据",
                impact="历史影响",
                suggestion="历史建议",
                required_test=None,
                confidence=0.9,
                verification_status=verification_status,
                rule_reference=None,
                reviewed_at=created_at,
                reviewed_by="historical-reviewer",
                created_at=created_at,
            )
        )
    return run_id, finding_id


def test_finding_backfill_is_bounded_ordered_and_idempotent(tmp_path: Path) -> None:
    database = Database.connect(
        f"sqlite:///{(tmp_path / 'finding-backfill.sqlite3').as_posix()}"
    )
    Base.metadata.create_all(database.engine)
    first_at = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
    try:
        first_run, first_finding = _seed_finding(
            database,
            sequence=1,
            created_at=first_at,
            verification_status="verified",
            location_in_diff=False,
        )
        second_run, second_finding = _seed_finding(
            database,
            sequence=2,
            created_at=first_at + timedelta(days=1),
            verification_status="rejected",
            location_in_diff=True,
        )

        backfill = SqlAlchemyFindingBackfill(database.sessions)
        first_batch = backfill.run_batch(1, now=first_at + timedelta(days=2))
        assert first_batch.lifecycle_groups == 1
        assert first_batch.lifecycle_findings == 1
        assert first_batch.adjudications == 1
        assert first_batch.evaluations == 1

        second_batch = backfill.run_batch(1, now=first_at + timedelta(days=2))
        assert second_batch.lifecycle_groups == 1
        assert second_batch.lifecycle_findings == 1
        assert second_batch.adjudications == 1
        assert second_batch.evaluations == 1
        assert not backfill.run_batch(1).changed

        with database.sessions() as session:
            lifecycle = session.scalar(select(FindingLifecycleRecord))
            assert lifecycle is not None
            assert lifecycle.first_seen_review_run_id == first_run
            assert lifecycle.last_seen_review_run_id == second_run
            assert lifecycle.previous_seen_review_run_id == first_run
            assert lifecycle.occurrence_count == 2
            assert lifecycle.last_occurrence_status == "still_present"
            assert lifecycle.historical_backfilled_at is not None

            findings = {
                finding.id: finding
                for finding in session.scalars(
                    select(ReviewFindingRecord).order_by(ReviewFindingRecord.created_at)
                )
            }
            assert findings[first_finding].lifecycle_status == "new"
            assert findings[first_finding].occurrence_count == 1
            assert findings[first_finding].previous_review_run_id is None
            assert findings[first_finding].lifecycle_backfilled_at is not None
            assert findings[first_finding].adjudication_status == "valid"
            assert findings[first_finding].verification_status == "unverified"
            assert findings[second_finding].lifecycle_status == "still_present"
            assert findings[second_finding].occurrence_count == 2
            assert findings[second_finding].previous_review_run_id == first_run
            assert findings[second_finding].lifecycle_backfilled_at is not None
            assert findings[second_finding].adjudication_status == "false_positive"
            assert findings[second_finding].verification_status == "verified"

            evaluations = list(
                session.scalars(
                    select(FindingEvaluationRecord).order_by(
                        FindingEvaluationRecord.finding_id
                    )
                )
            )
            assert {item.finding_id for item in evaluations} == {
                first_finding,
                second_finding,
            }
            assert {item.verdict for item in evaluations} == {
                "valid",
                "false_positive",
            }
    finally:
        database.dispose()
