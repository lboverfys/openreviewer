"""评测纯逻辑与实际供应商适配器的版本记录。"""

import httpx

from domain.enums import ModelApiProtocol, ModelProvider
from domain.evaluation_workbench import (
    EvaluationBallot,
    EvaluationDecision,
    EvaluationFinding,
    ReferenceDefect,
    assessment_metrics,
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
