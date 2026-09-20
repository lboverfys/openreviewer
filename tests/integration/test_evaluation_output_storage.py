"""输出证据的账本关联、容量、范围与来源清理后的独立生命周期。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import delete, func, select, update

from domain.evaluation_outputs import (
    MAX_RUN_OUTPUT_BYTES,
    CapturedModelOutput,
    ModelOutputSource,
    capture_output_text,
)
from domain.evaluation_workbench import EvaluationNotFoundError
from persistence.evaluation_outputs import list_output_evidence
from persistence.models import (
    EvaluationModelOutputRecord,
    ModelUsageRequestRecord,
    ReviewRunRecord,
)
from persistence.operations import SqlAlchemyOperationsRepository
from services.operations import RetentionCutoffs
from services.rbac import ResourceScope
from tests.integration.test_management_api import database as database
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)
from tests.integration.test_team_platform import ALL, NOW, request, settle, setup_ledger


def capture_request(database):
    _, lease, clock, ledger = setup_ledger(database)
    lease = replace(lease, review_plan_id="capture-plan")
    with database.sessions() as session:
        sha = session.scalar(select(ReviewRunRecord.head_sha).where(ReviewRunRecord.id == lease.review_run_id))
    source = ModelOutputSource(lease.review_run_id, lease.review_plan_id, sha, "a" * 64, "b" * 40, batch_number=1)
    return lease, clock, ledger, request(output_source=source, request_sha256="c" * 64)


def enable_capture(database, lease):
    with database.sessions() as session, session.begin():
        session.execute(update(ReviewRunRecord).where(ReviewRunRecord.id == lease.review_run_id).values(capture_model_outputs=True))


def test_capture_defaults_off_and_enabled_request_uses_current_ledger_identity(database):
    lease, _, ledger, draft = capture_request(database)
    disabled = ledger.reserve(lease, "logic", draft)
    assert not disabled.capture_output
    settle(ledger, disabled)
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(EvaluationModelOutputRecord)) == 0
    enable_capture(database, lease)
    reservation = ledger.reserve(lease, "logic", draft)
    assert reservation.capture_output
    captured = capture_output_text({"content":[{"type":"text", "text":"示例回答"}]}, "messages", streamed=False)
    ledger.record_output(reservation, captured)
    ledger.record_output(reservation, captured)
    ledger.record_output(reservation, CapturedModelOutput(status="parse_failed", error_code="invalid_contract"))
    settle(ledger, reservation)
    with database.sessions() as session:
        row = session.get(EvaluationModelOutputRecord, reservation.id)
        assert session.get(ModelUsageRequestRecord, row.id).status == "settled"
        assert row.output_text == "示例回答" and row.status == "parse_failed"
        assert row.batch_number == 1 and row.head_sha == draft.output_source.head_sha
        assert session.get(ReviewRunRecord, lease.review_run_id).evaluation_output_bytes == captured.byte_size
        assert list_output_evidence(session, lease.review_run_id, ALL).items[0].id == row.id
        page = list_output_evidence(session, lease.review_run_id, ALL)
        assert "output_text" not in page.items[0].model_dump()
        assert captured.text not in page.model_dump_json()
        with pytest.raises(EvaluationNotFoundError):
            list_output_evidence(session, lease.review_run_id, ResourceScope(repositories=frozenset({"other/repo"})))


def test_per_run_limit_does_not_remove_charge_or_claim_complete_evidence(database):
    lease, _, ledger, draft = capture_request(database)
    enable_capture(database, lease)
    with database.sessions() as session, session.begin():
        session.execute(update(ReviewRunRecord).where(ReviewRunRecord.id == lease.review_run_id).values(evaluation_output_bytes=MAX_RUN_OUTPUT_BYTES - 1))
    reservation = ledger.reserve(lease, "logic", draft)
    ledger.record_output(reservation, capture_output_text({"content":[{"type":"text", "text":"超额"}]}, "messages", streamed=False))
    settle(ledger, reservation)
    with database.sessions() as session:
        row = session.get(EvaluationModelOutputRecord, reservation.id)
        assert row.status == "run_limit" and row.output_text is None and row.output_sha256 is None
        assert session.get(ModelUsageRequestRecord, reservation.id).estimated_cost_microusd == 20


def test_source_cleanup_preserves_capture_and_expiry_removes_only_body(database):
    lease, _, ledger, draft = capture_request(database)
    enable_capture(database, lease)
    reservation = ledger.reserve(lease, "logic", draft)
    output = capture_output_text({"content":[{"type":"text", "text":"受控输出"}]}, "messages", streamed=False)
    ledger.record_output(reservation, output)
    settle(ledger, reservation)
    with database.sessions() as session, session.begin():
        session.execute(delete(ReviewRunRecord).where(ReviewRunRecord.id == lease.review_run_id))
    with database.sessions() as session:
        assert list_output_evidence(session, lease.review_run_id, ALL).items[0].output_sha256 == output.sha256
        assert session.get(EvaluationModelOutputRecord, reservation.id).output_text == output.text
    old = datetime(2000, 1, 1, tzinfo=UTC)
    result = SqlAlchemyOperationsRepository(database.sessions).cleanup(
        RetentionCutoffs(old, old, old, old, old, evaluation_outputs_now=NOW + timedelta(days=31)), batch_size=10,
    )
    assert result.evaluation_outputs == 1
    with database.sessions() as session:
        row = session.get(EvaluationModelOutputRecord, reservation.id)
        assert row.status == "expired" and row.output_text is None
        assert row.output_sha256 == output.sha256  # 元数据明确标明已过期，不伪装正文仍在。


def test_postgres_concurrent_capture_obeys_the_shared_run_byte_limit(postgres_database):
    lease, _, ledger, draft = capture_request(postgres_database)
    enable_capture(postgres_database, lease)
    output = capture_output_text({"content":[{"type":"text", "text":"并发输出"}]}, "messages", streamed=False)
    with postgres_database.sessions() as session, session.begin():
        session.execute(update(ReviewRunRecord).where(ReviewRunRecord.id == lease.review_run_id).values(evaluation_output_bytes=MAX_RUN_OUTPUT_BYTES - output.byte_size))
    reservations = [ledger.reserve(lease, "logic", draft) for _ in range(2)]
    barrier = Barrier(2)

    def capture(reservation):
        barrier.wait(timeout=10)
        ledger.record_output(reservation, output)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(capture, reservations))
    with postgres_database.sessions() as session:
        statuses = session.scalars(select(EvaluationModelOutputRecord.status).where(EvaluationModelOutputRecord.review_run_id == lease.review_run_id)).all()
        assert sorted(statuses) == ["captured", "run_limit"]
        assert session.get(ReviewRunRecord, lease.review_run_id).evaluation_output_bytes == MAX_RUN_OUTPUT_BYTES
