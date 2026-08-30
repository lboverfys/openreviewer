from datetime import UTC, datetime
from hashlib import sha256

import pytest
from pydantic import ValidationError

from domain.enums import FindingEvaluationVerdict
from domain.real_evaluation import (
    HumanAdjudication,
    ObservedFinding,
    RealEvaluationDataset,
    RealModelSample,
    summarize_real_evaluation,
)


def _adjudication(
    reviewer: str,
    verdict: FindingEvaluationVerdict,
    *,
    location_correct: bool | None = None,
) -> HumanAdjudication:
    return HumanAdjudication(
        reviewer=reviewer,
        verdict=verdict,
        location_correct=location_correct,
        adjudicated_at=datetime(2026, 8, 30, tzinfo=UTC),
    )


def _finding(
    key: str,
    *adjudications: HumanAdjudication,
) -> ObservedFinding:
    return ObservedFinding(
        finding_key=key,
        fingerprint=f"fingerprint:{key}",
        severity="high",
        category="security",
        confidence=0.95,
        adjudications=adjudications,
    )


def _sample(**overrides: object) -> RealModelSample:
    raw_output = '{"findings": []}'
    values: dict[str, object] = {
        "sample_id": "sample-1",
        "repository": "owner/repository",
        "pull_request_number": 19,
        "head_sha": "a" * 40,
        "openreviewer_commit_sha": "b" * 40,
        "provider": "openai",
        "model": "gpt-test-snapshot",
        "prompt_version": "structured-review-v4",
        "sampled_at": datetime(2026, 8, 30, tzinfo=UTC),
        "redacted_raw_output": raw_output,
        "raw_output_sha256": sha256(raw_output.encode()).hexdigest(),
        "input_tokens": 100,
        "output_tokens": 20,
        "latency_ms": 500,
        "cost_usd": 0.25,
        "findings": (),
    }
    values.update(overrides)
    return RealModelSample.model_validate(values)


def test_real_evaluation_reports_adjudication_location_cost_and_recall() -> None:
    valid = (
        _adjudication("reviewer-a", FindingEvaluationVerdict.VALID, location_correct=True),
        _adjudication("reviewer-b", FindingEvaluationVerdict.VALID, location_correct=True),
    )
    false_positive = (
        _adjudication(
            "reviewer-a",
            FindingEvaluationVerdict.FALSE_POSITIVE,
            location_correct=False,
        ),
        _adjudication(
            "reviewer-b",
            FindingEvaluationVerdict.FALSE_POSITIVE,
            location_correct=False,
        ),
    )
    duplicate = (
        _adjudication("reviewer-a", FindingEvaluationVerdict.DUPLICATE),
        _adjudication("reviewer-b", FindingEvaluationVerdict.DUPLICATE),
    )
    disagreement = (
        _adjudication("reviewer-a", FindingEvaluationVerdict.VALID),
        _adjudication("reviewer-b", FindingEvaluationVerdict.OUT_OF_SCOPE),
    )
    sample = _sample(
        findings=(
            _finding("expected", *valid),
            _finding("noise", *false_positive),
            _finding("duplicate", *duplicate),
            _finding("pending", valid[0]),
            _finding("disagreement", *disagreement),
        ),
        expected_finding_keys=("expected", "missed"),
        reference_reviewers=("reference-a", "reference-b"),
    )
    report = summarize_real_evaluation(
        RealEvaluationDataset(dataset_id="real-pr-shadow", samples=(sample,))
    )
    overall = report["overall"]

    assert isinstance(overall, dict)
    assert overall["adjudicated_count"] == 3
    assert overall["unadjudicated_count"] == 1
    assert overall["disagreement_count"] == 1
    assert overall["precision"] == pytest.approx(1 / 3)
    assert overall["duplicate_rate"] == pytest.approx(1 / 3)
    assert overall["location_accuracy"] == pytest.approx(0.5)
    assert overall["recall"] == pytest.approx(0.5)
    assert overall["total_cost_usd"] == pytest.approx(0.25)
    assert overall["precision_ci95"] is not None
    assert report["execution_mode"] == "real_model_observation"


def test_real_evaluation_rejects_unredacted_or_tampered_output() -> None:
    secret_output = "Authorization: Bearer sk-secret-value-123456789"
    with pytest.raises(ValidationError, match="recognizable secret"):
        _sample(
            redacted_raw_output=secret_output,
            raw_output_sha256=sha256(secret_output.encode()).hexdigest(),
        )
    with pytest.raises(ValidationError, match="hash does not match"):
        _sample(raw_output_sha256="0" * 64)


def test_real_evaluation_requires_distinct_double_review() -> None:
    duplicate_reviewers = (
        _adjudication("reviewer-a", FindingEvaluationVerdict.VALID),
        _adjudication("REVIEWER-A", FindingEvaluationVerdict.VALID),
    )
    with pytest.raises(ValidationError, match="adjudicators must be distinct"):
        _finding("same-reviewer", *duplicate_reviewers)
    with pytest.raises(ValidationError, match="require two reference reviewers"):
        _sample(expected_finding_keys=("expected",))
