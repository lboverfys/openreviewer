import json
from hashlib import sha256

import pytest

from domain.enums import ModelProvider, ReviewAgent
from domain.model_review import (
    ModelReviewInput,
    materialize_findings,
    normalize_model_references,
)
from domain.retrieval import ContextEvidence, stable_key
from services.agent_workflow import scope_model_review_input
from services.model_review import (
    StructuredReviewPromptBuilder,
)
from tests.unit.test_model_review import make_model_input, make_output


def evidence(agent=ReviewAgent.LOGIC):
    content = "SELECT id FROM users WHERE id = ?"
    return ContextEvidence(
        reference_id=stable_key("index", "chunk"), chunk_id="chunk", index_id="index",
        head_sha="a" * 40, file="UserMapper.xml", blob_sha="b" * 40, symbol="UserMapper.get",
        start_line=1, end_line=1, content=content, content_hash=sha256(content.encode()).hexdigest(),
        routes=("bm25", "vector"), rank=1, fused_rank=2, fusion_score=0.03,
        selected=True, agent=agent,
    )


def test_model_context_has_stable_identity_and_is_scoped_to_agent():
    original = make_model_input()
    context = evidence()
    review_input = original.model_copy(update={"context_evidence": (context,)})
    assert scope_model_review_input(review_input, ReviewAgent.LOGIC).context_evidence == (context,)
    assert scope_model_review_input(review_input, ReviewAgent.SECURITY).context_evidence == ()
    prompt = StructuredReviewPromptBuilder().build(review_input, ModelProvider.OPENAI, "test")
    assert json.loads(prompt.user)["context_evidence"][0]["reference_id"] == "ctx_1"
    assert "fusion_score" not in prompt.user
    assert "rerank_score" not in prompt.user


def test_context_blob_hash_and_review_sha_are_checked():
    item = evidence()
    with pytest.raises(ValueError, match="哈希"):
        ContextEvidence.model_validate({**item.model_dump(), "content": "tampered"})
    source = make_model_input()
    values = source.model_dump()
    values["context_evidence"] = [item.model_copy(update={"head_sha": "c" * 40}).model_dump()]
    with pytest.raises(ValueError, match="SHA"):
        ModelReviewInput.model_validate(values)

def test_unknown_evidence_reference_is_rejected():
    review_input = make_model_input().model_copy(update={"context_evidence": (evidence(),)})
    output = make_output()
    candidate = output.findings[0].model_copy(update={"context_references": ("unknown",)})
    with pytest.raises(ValueError, match="unknown retrieval evidence"):
        materialize_findings(review_input, output.model_copy(update={"findings": (candidate,)}))
    known = candidate.model_copy(update={"context_references": (evidence().reference_id,)})
    result = materialize_findings(review_input, output.model_copy(update={"findings": (known,)}))
    assert result[0].finding.context_references == (evidence().reference_id,)


def test_short_context_and_knowledge_paths_keep_their_trusted_identity():
    context = evidence()
    review_input = make_model_input().model_copy(update={"context_evidence": (context,), "knowledge_versions": {"security.md": "version-1"}})
    output = make_output()
    candidate = output.findings[0].model_copy(update={"unit_key": "u_1", "context_references": ("ctx_1",), "rule_reference": "security.md#认证@v1"})
    normalized = normalize_model_references(review_input, output.model_copy(update={"findings": (candidate,)}))
    findings = materialize_findings(review_input, normalized)
    assert findings[0].finding.context_references == (context.reference_id,)
    assert findings[0].finding.rule_reference == "security.md"
    assert findings[0].source_unit_key == review_input.units[0].unit_key
    prompt = StructuredReviewPromptBuilder().build(review_input, ModelProvider.OPENAI, "test")
    assert "security.md" in json.loads(prompt.user)["allowed_rule_references"]
    with pytest.raises(ValueError, match="unknown retrieval evidence"):
        normalize_model_references(review_input, output.model_copy(update={"findings": (candidate.model_copy(update={"context_references": ("ctx_9",)}),)}))
