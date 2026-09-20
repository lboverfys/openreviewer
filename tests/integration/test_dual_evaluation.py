"""双人验收的独立判断、分歧、严格筛选和并发参与者边界。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest
from sqlalchemy import update

from apps.api.main import create_app
from domain.evaluation_workbench import (
    EvaluationConflictError,
    EvaluationDecision,
    FindingReviewWrite,
    ReferenceReviewWrite,
    ReferenceUpdate,
)
from domain.project_evidence import project_evidence
from persistence.models import EvaluationObservationRecord
from services.auth import AuthService, AuthSettings, UserCredential
from services.rbac import AccessRole
from tests.integration.test_evaluation_workbench import (
    ALL,
    OWN,
    create_pair,
    set_reference,
    submit_ballot,
)
from tests.integration.test_management_api import database as database
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)
from tests.support import TEST_HASHER, TEST_PASSWORD, TEST_PASSWORD_HASH, TEST_USERNAME


def dual_pair(database, **kwargs):
    return create_pair(database, baseline_findings=["问题"], review_mode="dual", **kwargs)


def test_dual_requires_two_distinct_submissions_and_hides_independent_votes(database):
    service, dataset, case_id, _ = dual_pair(database)
    set_reference(service, case_id)
    submit_ballot(service, case_id, "baseline", "alice", [("valid", "auth")])
    assert service.observation(case_id, "baseline", ALL).observation.assessment_status == "partial"
    assert not service.findings(case_id, "baseline", ALL, actor="bob").items[0].reviews
    assert not service.observation(case_id, "baseline", ALL, actor="bob").ballots
    assert len(service.findings(case_id, "baseline", ALL, actor="ALICE").items[0].reviews) == 1
    repeated = submit_ballot(service, case_id, "baseline", "ALICE", [("valid", "auth")])
    assert len(repeated.ballots) == 1 and repeated.observation.assessment_status == "partial"
    for variant in ("baseline", "candidate"):
        for actor in (("bob",) if variant == "baseline" else ("alice", "bob")):
            submit_ballot(service, case_id, variant, actor, [("valid", "auth")])
    report = service.report(dataset.id, ALL)
    assert report.review_mode == "dual" and report.reference_pairs == report.quality_pairs == 1
    assert project_evidence(report).evaluation_status == "reviewed_samples"
    assert len(service.findings(case_id, "baseline", ALL, actor="bob").items[0].reviews) == 2
    before = service.observation(case_id, "baseline", ALL, actor="alice")
    finding = service.findings(case_id, "baseline", ALL, actor="alice").items[0].finding
    changed = service.review_finding(case_id, "baseline", finding.id, FindingReviewWrite(
        expected_revision=before.observation.revision,
        decision=EvaluationDecision(verdict="valid", reference_key="auth"),
    ), "alice", ALL)
    assert changed.observation.assessment_status == "partial"
    assert len(changed.ballots) == 2  # 首次提交后的可见性不随草稿修改回退。
    assert service.report(dataset.id, ALL).quality_pairs == 0
    assert service.report(dataset.id, ALL).data_version != report.data_version
    with pytest.raises(EvaluationConflictError):
        service.submit_review(case_id, "baseline", before.observation.revision, "alice", ALL)
    with pytest.raises(EvaluationConflictError, match="两位"):
        service.submit_review(case_id, "baseline", changed.observation.revision, "charlie", ALL)


def test_dual_disposition_disputes_and_missing_provenance_are_excluded(database):
    service, dataset, case_id, _ = dual_pair(database)
    for variant in ("baseline", "candidate"):
        submit_ballot(service, case_id, variant, "alice", [("valid", None)])
        submit_ballot(service, case_id, variant, "bob", [("false_positive" if variant == "baseline" else "valid", None)])
    report = service.report(dataset.id, ALL)
    assert report.disputed_pairs == 1 and report.quality_pairs == 0
    submit_ballot(service, case_id, "baseline", "bob", [("valid", None)])
    assert service.report(dataset.id, ALL).quality_pairs == 1
    with database.sessions() as session, session.begin():
        session.execute(update(EvaluationObservationRecord).where(
            EvaluationObservationRecord.case_id == case_id,
            EvaluationObservationRecord.variant == "baseline",
        ).values(provenance_complete=False))
    report = service.report(dataset.id, ALL)
    assert report.quality_pairs == 0 and report.provenance_missing_pairs == 1
    assert report.performance_pairs == 1


def test_reference_disagreement_keeps_validity_and_location_disagreement_has_own_denominator(database):
    service, dataset, case_id, _ = dual_pair(database)
    set_reference(service, case_id)
    for variant in ("baseline", "candidate"):
        submit_ballot(service, case_id, variant, "alice", [("valid", "auth")])
        submit_ballot(service, case_id, variant, "bob", [("valid", None)])
        detail = service.observation(case_id, variant, ALL)
        finding = service.findings(case_id, variant, ALL).items[0].finding
        detail = service.review_finding(case_id, variant, finding.id, FindingReviewWrite(
            expected_revision=detail.observation.revision,
            decision=EvaluationDecision(verdict="valid", location_correct=False),
        ), "bob", ALL)
        service.submit_review(case_id, variant, detail.observation.revision, "bob", ALL)
    report = service.report(dataset.id, ALL)
    assert report.quality_pairs == 1 and report.reference_pairs == 0
    assert report.reference_disputed_pairs == 1 and report.location_disagreements == 2
    assert report.baseline.precision == 1 and report.baseline.location_accuracy is None


def test_dual_empty_findings_and_empty_confirmed_reference_are_not_fabricated_recall(database):
    service, dataset, case_id, _ = create_pair(database, review_mode="dual", baseline_findings=[], candidate_findings=[])
    sample = service.case(case_id, ALL)
    sample = service.update_reference(case_id, ReferenceUpdate(expected_revision=sample.revision, reference_defects=()), "alice", ALL)
    sample = service.review_reference(case_id, ReferenceReviewWrite(expected_revision=sample.revision, agrees=True), "alice", ALL)
    assert sample.reference_status == "partial"
    assert not service.case(case_id, ALL, actor="bob").reference_reviews
    sample = service.review_reference(case_id, ReferenceReviewWrite(expected_revision=sample.revision, agrees=False), "bob", ALL)
    assert sample.reference_status == "disputed"
    sample = service.review_reference(case_id, ReferenceReviewWrite(expected_revision=sample.revision, agrees=True), "bob", ALL)
    with pytest.raises(EvaluationConflictError, match="两位"):
        service.review_reference(case_id, ReferenceReviewWrite(expected_revision=sample.revision, agrees=True), "charlie", ALL)
    for variant in ("baseline", "candidate"):
        submit_ballot(service, case_id, variant, "alice", [])
    assert service.report(dataset.id, ALL).quality_pairs == 0
    for variant in ("baseline", "candidate"):
        submit_ballot(service, case_id, variant, "bob", [])
    report = service.report(dataset.id, ALL)
    assert report.reference_pairs == 1
    assert report.baseline.recall is report.baseline.precision is None


def test_dual_api_and_audit_do_not_disclose_other_reviewers_judgments(database):
    service, dataset, case_id, _ = dual_pair(database)
    set_reference(service, case_id)
    submit_ballot(service, case_id, "baseline", "alice", [("valid", "auth")])
    auth = AuthService(AuthSettings(username=TEST_USERNAME, password_hash=TEST_PASSWORD_HASH,
        session_secret=b"test-dual-evaluation-secret-long-enough", cookie_secure=False,
        additional_users=(UserCredential(username="bob", password_hash=TEST_PASSWORD_HASH,
            role=AccessRole.ADJUDICATOR, resource_scope=OWN),)), password_hasher=TEST_HASHER)
    application = create_app(auth_service=auth, evaluation_service=service)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://testserver") as client:
            await client.post("/api/v1/auth/login", json={"username":"bob", "password":TEST_PASSWORD})
            prefix = f"/api/v1/evaluations/cases/{case_id}/observations/baseline"
            assert (await client.get(prefix)).json()["ballots"] == []
            assert (await client.get(prefix + "/findings")).json()["items"][0]["reviews"] == []
            audit = await client.get(f"/api/v1/evaluations/datasets/{dataset.id}/audits")
            assert audit.status_code == 200
            assert all(not {"verdict", "reference_key", "agrees"}.intersection(item["payload"]) for item in audit.json()["items"])
    asyncio.run(exercise())


def test_postgres_competing_second_and_third_reviewers_cannot_both_join(postgres_database):
    service, _, case_id, _ = dual_pair(postgres_database)
    submit_ballot(service, case_id, "baseline", "alice", [("valid", None)])
    detail = service.observation(case_id, "baseline", ALL)
    finding = service.findings(case_id, "baseline", ALL).items[0].finding
    barrier = Barrier(2)

    def join(actor):
        barrier.wait(timeout=10)
        try:
            service.review_finding(case_id, "baseline", finding.id, FindingReviewWrite(
                expected_revision=detail.observation.revision,
                decision=EvaluationDecision(verdict="valid"),
            ), actor, ALL)
            return True
        except EvaluationConflictError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(join, ("bob", "charlie"))) == 1
    assert len(service.observation(case_id, "baseline", ALL, actor="alice").ballots) == 2
