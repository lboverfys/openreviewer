"""归档不覆盖历史产物，复算复用原有 SQL 聚合且检测篡改。"""

import json
from pathlib import Path

import pytest
from sqlalchemy import update

from apps.maintenance.export_workflow_evidence import export_archive, verify_archive
from domain.evaluation_workbench import EvaluationNotFoundError
from persistence.models import ReviewRunRecord
from tests.evaluation_support import seed_evaluation_runs
from tests.integration.test_evaluation_workbench import (
    ALL,
    DENIED,
    create_pair,
    set_reference,
    submit_ballot,
)
from tests.integration.test_management_api import database as database


def test_workflow_archive_recomputes_report_and_keeps_failures_without_duplicate_findings(database, tmp_path):
    service, dataset, case_id, _ = create_pair(database, review_mode="dual", baseline_findings=["问题"])
    set_reference(service, case_id)
    for variant in ("baseline", "candidate"):
        for actor in ("alice", "bob"):
            submit_ballot(service, case_id, variant, actor, [("valid", "auth")])
    failed = seed_evaluation_runs(database, [{"label":"failed", "pr":999}])["failed"]
    with database.sessions() as session, session.begin():
        session.execute(update(ReviewRunRecord).where(ReviewRunRecord.id == failed).values(execution_status="failed"))
    output = tmp_path / "archive"
    manifest = export_archive(database.sessions, dataset.id, output, ALL, run_ids=(failed,))
    assert manifest["sample_flow"]["planned_runs"] == 3
    assert manifest["sample_flow"]["workflow_snapshots"] == 2
    assert manifest["sample_flow"]["failed_or_interrupted_runs"] == 1
    assert manifest["sample_flow"]["not_in_workbench_runs"] == 1
    assert len([entry for entry in manifest["files"] if entry["kind"] == "observation"]) == 2
    first = verify_archive(output, tmp_path / "replay")
    second = verify_archive(output, tmp_path / "replay")
    assert first == second == service.report(dataset.id, ALL).model_dump(mode="json", exclude={"generated_at"})
    assert not list((tmp_path / "replay").iterdir())
    with pytest.raises(ValueError, match="拒绝覆盖"):
        export_archive(database.sessions, dataset.id, output, ALL)
    entry = next(entry for entry in manifest["files"] if entry["kind"] == "observation")
    path = output / entry["name"]
    content = path.read_bytes()
    path.write_bytes(content.replace(b'"valid"', b'"other"', 1))
    with pytest.raises(ValueError, match="哈希"):
        verify_archive(output, tmp_path / "replay")


def test_archive_rejects_foreign_scope_and_git_workspace(database, tmp_path):
    _, dataset, _, _ = create_pair(database)
    with pytest.raises(EvaluationNotFoundError):
        export_archive(database.sessions, dataset.id, tmp_path / "denied", DENIED)
    assert not (tmp_path / "denied").exists()
    with pytest.raises(ValueError, match="Git"):
        export_archive(database.sessions, dataset.id, Path(__file__).resolve().parents[2] / "forbidden-archive", ALL)
    with pytest.raises(EvaluationNotFoundError):
        export_archive(database.sessions, dataset.id, tmp_path / "missing-run", ALL, run_ids=("unknown-run",))
    assert not (tmp_path / "missing-run").exists()


def test_archive_verifier_rejects_paths_outside_archive(database, tmp_path):
    _, dataset, _, _ = create_pair(database)
    output = tmp_path / "archive"
    export_archive(database.sessions, dataset.id, output, ALL)
    path = output / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"][0]["name"] = "../private.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="越界"):
        verify_archive(output, tmp_path / "replay")
