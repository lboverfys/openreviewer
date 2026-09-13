"""质量提示必须与当前方案、真实样本和人工理由绑定。"""

import pytest
from sqlalchemy import select, update

from domain.platform import PlatformConflictError, PlatformNotFoundError
from domain.project_evidence import project_evidence
from persistence.models import EvaluationObservationRecord, OutboxEventRecord
from services.rbac import ResourceScope
from tests.integration.test_evaluation_workbench import create_pair
from tests.integration.test_management_api import database as database
from tests.integration.test_review_profiles import ALL, draft, profile_services
from tests.support import TEST_USERNAME


def test_no_evaluation_requires_reason_and_records_audit(database, tmp_path):
    _, policy, repository, service, _, _ = profile_services(database, tmp_path)
    profile = service.create(draft(), TEST_USERNAME, ALL)
    quality = repository.quality(profile.id, ALL)
    assert quality.status == "unverified" and quality.report is None
    with pytest.raises(ValueError, match="启用理由"):
        repository.activate(profile.id, policy.revision, TEST_USERNAME, ALL)
    repository.activate(profile.id, policy.revision, TEST_USERNAME, ALL,
        evidence_token=quality.evidence_token, reason="先在受控仓库试用，随后补齐复核")
    with database.sessions() as session:
        payload = session.scalar(select(OutboxEventRecord.payload).where(
            OutboxEventRecord.event_type == "platform.profile.activated"))
    assert payload["quality_status"] == "unverified"
    assert payload["evidence_token"] == quality.evidence_token


def test_quality_rejects_foreign_scope_and_changed_evidence(database, tmp_path):
    _, policy, repository, service, _, _ = profile_services(database, tmp_path)
    profile = service.create(draft(), TEST_USERNAME, ALL)
    _, dataset, _, _ = create_pair(database)
    with pytest.raises(PlatformNotFoundError):
        repository.quality(profile.id, ResourceScope(), dataset.id)
    quality = repository.quality(profile.id, ALL, dataset.id)
    assert quality.status == "unverified"
    assert any("绑定" in reason for reason in quality.reasons)
    with database.sessions() as session, session.begin():
        session.execute(update(EvaluationObservationRecord).values(revision=EvaluationObservationRecord.revision + 1))
    with pytest.raises(PlatformConflictError, match="评测"):
        repository.activate(profile.id, policy.revision, TEST_USERNAME, ALL,
            dataset_id=dataset.id, evidence_token=quality.evidence_token, reason="试用")


def test_unreviewed_evidence_export_does_not_invent_metrics():
    evidence = project_evidence(None)
    assert evidence.evaluation_status == "awaiting_human_review"
    assert evidence.evaluation is None
    assert any("不得填写" in message for message in evidence.limitations)
