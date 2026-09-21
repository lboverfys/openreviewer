"""质量与成本必须使用相同且已确认的样本分母，人工归因不能由机器核验代填。"""

import pytest

from domain.evaluation_workbench import ReferenceReviewWrite, ReferenceUpdate
from tests.integration.test_evaluation_workbench import (
    ALL,
    create_pair,
    set_reference,
    submit_ballot,
)
from tests.integration.test_management_api import database as database
from tests.support import TEST_USERNAME


@pytest.mark.parametrize("priced", [True, False])
def test_confirmed_defect_cost_excludes_unknown_cost_pairs(database, priced):
    service, dataset, case_id, _ = create_pair(database, candidate_cost=2000 if priced else None)
    set_reference(service, case_id)
    submit_ballot(service, case_id, "baseline", "alice", [("valid", "auth"), ("false_positive", None)])
    submit_ballot(service, case_id, "candidate", "alice", [("valid", "auth")])
    report = service.report(dataset.id, ALL, "validation")
    assert report.baseline.priced_reference_pairs == int(priced)
    assert report.baseline.cost_per_confirmed_defect_usd == (0.001 if priced else None)
    assert report.candidate.cost_per_confirmed_defect_usd == (0.002 if priced else None)
    assert report.baseline.clean_pr_false_alarm_rate is None


def test_clean_pr_false_alarm_requires_confirmed_empty_reference(database):
    service, dataset, case_id, _ = create_pair(database, baseline_findings=["错误建议"], candidate_findings=[])
    submit_ballot(service, case_id, "baseline", "alice", [("false_positive", None)])
    submit_ballot(service, case_id, "candidate", "alice", [])
    assert service.report(dataset.id, ALL, "validation").baseline.clean_pr_false_alarm_rate is None
    sample = service.case(case_id, ALL)
    sample = service.update_reference(case_id, ReferenceUpdate(expected_revision=sample.revision,
        reference_defects=(), kind="normal", reset_reviews=True), TEST_USERNAME, ALL)
    service.review_reference(case_id, ReferenceReviewWrite(expected_revision=sample.revision,
        agrees=True), "alice", ALL)
    submit_ballot(service, case_id, "baseline", "alice", [("false_positive", None)])
    submit_ballot(service, case_id, "candidate", "alice", [])
    report = service.report(dataset.id, ALL, "validation")
    assert report.baseline.clean_pr_count == report.candidate.clean_pr_count == 1
    assert report.baseline.clean_pr_false_alarm_rate == 1
    assert report.candidate.clean_pr_false_alarm_rate == 0
    assert report.baseline.cost_per_confirmed_defect_usd is None
