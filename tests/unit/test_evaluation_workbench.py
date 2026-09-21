"""评测纯逻辑与实际供应商适配器的版本记录。"""

import httpx
import pytest

from domain.enums import ModelApiProtocol, ModelProvider
from domain.evaluation_workbench import (
    EvaluationBallot,
    EvaluationDecision,
    EvaluationFinding,
    ReferenceDefect,
    ReferenceReview,
    assessment_metrics,
    reference_status,
)
from domain.model_review import ModelReviewResult
from services.model_providers import create_model_reviewer
from services.model_review import (
    combine_model_review_results,
    plan_model_review_batches,
)
from tests.unit.test_model_providers import _settings
from tests.unit.test_model_review import make_model_input, make_output


def test_provider_and_batch_copies_record_actual_application_and_knowledge_versions(monkeypatch):
    monkeypatch.setenv("OPENREVIEWER_DEPLOYMENT_IMAGE","ghcr.io/example/reviewer:"+"d"*40)
    review_input=make_model_input().model_copy(update={
        "knowledge_versions":{"rules.md":"e"*16},
        "knowledge_references":("rules.md#安全@"+"e"*16+": 校验权限",),
    })
    def handler(request):
        return httpx.Response(200,json={
            "id":"test-evaluation-response",
            "choices":[{"finish_reason":"stop","message":{"content":make_output().model_dump_json()}}],
            "usage":{"prompt_tokens":100,"completion_tokens":20},
        })
    settings=_settings(ModelProvider.OPENAI,api_protocol=ModelApiProtocol.CHAT_COMPLETIONS)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=create_model_reviewer(settings,client=client).review(review_input)
    assert result.provenance.application_revision=="d"*40
    assert result.provenance.knowledge_versions=={"rules.md":"e"*16}
    restored=ModelReviewResult.model_validate(result.model_dump(mode="json"))
    assert restored.provenance==result.provenance
    assert all(batch.review_input.knowledge_versions==review_input.knowledge_versions
               for batch in plan_model_review_batches(review_input,settings))
    assert combine_model_review_results(review_input,(result,result)).provenance==result.provenance


def test_duplicate_reference_matches_count_once_and_saved_drafts_are_visible():
    from datetime import UTC, datetime

    now=datetime.now(UTC)
    findings=tuple(EvaluationFinding(
        id=str(index),fingerprint=str(index),title="权限问题",severity="high",category="authorization",
        file="service.py",start_line=1,end_line=1,evidence="未校验归属",impact="越权",
        suggestion="增加校验",confidence=0.9,location_status="verified",evidence_status="unverified",
    ) for index in range(2))
    decision=EvaluationDecision(verdict="valid",reference_key="auth",location_correct=True)
    votes=tuple(EvaluationBallot(reviewer=actor,decisions={"0":decision,"1":decision},
                                submitted_at=now,updated_at=now) for actor in ("alice","bob"))
    refs=(ReferenceDefect(key="auth",title="越权",category="authorization"),)
    status,counts=assessment_metrics(findings,votes,refs)
    assert status=="complete" and counts["valid_count"]==2
    assert counts["reference_true_positive_count"]==1
    status,counts=assessment_metrics(findings,(votes[0],votes[1].model_copy(update={"submitted_at":None})),refs)
    assert status=="partial" and counts["valid_count"]==2


@pytest.mark.parametrize("verdict,submitted,expected", [
    ("valid", True, "complete"), ("uncertain", True, "partial"),
    ("false_positive", True, "disputed"), ("valid", False, "partial"),
])
def test_dual_consensus_does_not_accept_uncertain_or_draft(verdict, submitted, expected):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    finding = EvaluationFinding(id="one", fingerprint="one", title="权限", severity="high", category="authorization",
        file=None, start_line=None, end_line=None, evidence="引用", impact="影响", suggestion="建议",
        confidence=1, location_status="unverified", evidence_status="unverified")
    ballots = (
        EvaluationBallot(reviewer="alice", decisions={"one":EvaluationDecision(verdict="valid")}, submitted_at=now, updated_at=now),
        EvaluationBallot(reviewer="bob", decisions={"one":EvaluationDecision(verdict=verdict)}, submitted_at=now if submitted else None, updated_at=now),
    )
    status, counts = assessment_metrics((finding,), ballots, None, "dual")
    assert status == expected
    assert counts["valid_count"] == (1 if expected == "complete" else 0)
    duplicate = (ballots[0], ballots[0].model_copy(update={"reviewer":"ALICE"}))
    assert assessment_metrics((finding,), duplicate, None, "dual")[0] == "partial"
    reviews = tuple(ReferenceReview(reviewer=name, agrees=True, reviewed_at=now) for name in ("alice", "ALICE"))
    assert reference_status(reviews, "dual") == "partial"


def test_stable_profile_fingerprint_does_not_change_with_executed_role_subset():
    from datetime import UTC, datetime

    from domain.evaluation_workbench import EvaluationSource, ModelVersion
    from services.evaluation_workbench import _observation_values

    source = EvaluationSource(review_run_id="run", installation_id=1, repository_id=1,
        repository="example/repo", repository_key="example/repo", pull_request_number=1,
        head_sha="a" * 40, title="示例", plan_fingerprint="b" * 64, planner_version="test",
        configuration_revision=1, repository_policy={"review_profile_id":"profile"},
        profile_fingerprint="c" * 64, profile_calls_consistent=True,
        models=(ModelVersion(agent="logic", provider="openai", protocol="responses", model="model",
            prompt_version="test", application_revision="d" * 40, context_recorded=True),),
        findings=(), input_tokens=1, output_tokens=1, model_duration_ms=1, turnaround_ms=1,
        estimated_cost_microusd=1, completed_at=datetime.now(UTC), captured_at=datetime.now(UTC))
    baseline = _observation_values(source)
    other = source.model_copy(update={"models": source.models + (source.models[0].model_copy(update={
        "agent":"security", "knowledge_versions":{"rules.md":"e" * 16},
    }),)})
    assert _observation_values(other)["configuration_fingerprint"] == baseline["configuration_fingerprint"]
    assert baseline["provenance_complete"] is True
    assert _observation_values(source.model_copy(update={"profile_calls_consistent":False}))["provenance_complete"] is False
    changed = source.model_copy(update={"models":(source.models[0].model_copy(update={"application_revision":"f" * 40}),)})
    assert _observation_values(changed)["configuration_fingerprint"] != baseline["configuration_fingerprint"]
    mixed = source.model_copy(update={"models":source.models + changed.models})
    assert _observation_values(mixed)["provenance_complete"] is False
def test_failure_reason_requires_human_agreement():
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    finding = EvaluationFinding(id="one", fingerprint="one", title="问题", severity="high", category="authorization",
        file=None, start_line=None, end_line=None, evidence="未匹配", impact="影响", suggestion="建议",
        confidence=1, location_status="unverified", evidence_status="unverified", evidence_reason="evidence_text_not_found")
    first = EvaluationDecision(verdict="false_positive", failure_reason="context_missing")
    def ballot(who, decision):
        return EvaluationBallot(reviewer=who, decisions={"one": decision}, submitted_at=now, updated_at=now)
    for other, context_count, disagreement in ((first, 1, 0), (first.model_copy(update={"failure_reason":"reasoning_error"}), 0, 1)):
        _, counts = assessment_metrics((finding,), (ballot("alice", first), ballot("bob", other)), None, "dual")
        assert counts["failure_context_missing"] == context_count
        assert counts["failure_reason_disagreements"] == disagreement
        assert counts["failure_evidence_mismatch"] == 0
