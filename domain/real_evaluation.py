"""真实模型观测、双人裁决与可重复质量报告契约。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha256
from math import sqrt
from typing import Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from domain.enums import (
    FindingCategory,
    FindingEvaluationVerdict,
    ModelProvider,
    Severity,
)
from domain.identifiers import normalize_sha
from domain.security import redact_text


class RealEvaluationModel(BaseModel):
    """真实评测文件的严格、不可变基础模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class HumanAdjudication(RealEvaluationModel):
    reviewer: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_.:@-]+$")
    verdict: FindingEvaluationVerdict
    location_correct: bool | None = None
    adjudicated_at: AwareDatetime


class ObservedFinding(RealEvaluationModel):
    finding_key: str = Field(min_length=1, max_length=512)
    fingerprint: str = Field(min_length=1, max_length=256)
    severity: Severity
    category: FindingCategory
    confidence: float = Field(ge=0, le=1)
    adjudications: tuple[HumanAdjudication, ...] = Field(
        default=(),
        max_length=2,
    )

    @model_validator(mode="after")
    def validate_reviewers(self) -> Self:
        reviewers = [item.reviewer.casefold() for item in self.adjudications]
        if len(reviewers) != len(set(reviewers)):
            raise ValueError("finding adjudicators must be distinct")
        return self

    @property
    def final_verdict(self) -> FindingEvaluationVerdict | None:
        if len(self.adjudications) != 2:
            return None
        first, second = self.adjudications
        return first.verdict if first.verdict is second.verdict else None

    @property
    def location_result(self) -> bool | None:
        if len(self.adjudications) != 2:
            return None
        first, second = self.adjudications
        if first.location_correct is None or second.location_correct is None:
            return None
        if first.location_correct != second.location_correct:
            return None
        return first.location_correct


class RealModelSample(RealEvaluationModel):
    sample_id: str = Field(min_length=1, max_length=200)
    repository: str = Field(
        min_length=3,
        max_length=255,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    pull_request_number: int = Field(gt=0)
    head_sha: str = Field(min_length=40, max_length=64)
    openreviewer_commit_sha: str = Field(min_length=40, max_length=64)
    provider: ModelProvider
    model: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=200)
    sampled_at: AwareDatetime
    redacted_raw_output: str = Field(min_length=1, max_length=262_144)
    raw_output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_tokens: int = Field(ge=0, le=2_147_483_647)
    output_tokens: int = Field(ge=0, le=2_147_483_647)
    latency_ms: int = Field(ge=0, le=86_400_000)
    cost_usd: float = Field(ge=0, le=100_000)
    findings: tuple[ObservedFinding, ...] = Field(default=(), max_length=500)
    expected_finding_keys: tuple[str, ...] | None = Field(
        default=None,
        max_length=500,
    )
    reference_reviewers: tuple[str, ...] = Field(default=(), max_length=2)

    @field_validator("head_sha", "openreviewer_commit_sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        return normalize_sha(value)

    @field_validator("raw_output_sha256")
    @classmethod
    def normalize_output_hash(cls, value: str) -> str:
        return value.casefold()

    @model_validator(mode="after")
    def validate_sample(self) -> Self:
        if redact_text(self.redacted_raw_output) != self.redacted_raw_output:
            raise ValueError("raw model output still contains a recognizable secret")
        actual_hash = sha256(self.redacted_raw_output.encode("utf-8")).hexdigest()
        if actual_hash != self.raw_output_sha256:
            raise ValueError("raw model output hash does not match redacted content")
        finding_keys = [item.finding_key for item in self.findings]
        if len(finding_keys) != len(set(finding_keys)):
            raise ValueError("finding keys must be unique within a sample")
        reviewers = [item.casefold() for item in self.reference_reviewers]
        if len(reviewers) != len(set(reviewers)):
            raise ValueError("reference reviewers must be distinct")
        if self.expected_finding_keys is None:
            if self.reference_reviewers:
                raise ValueError("reference reviewers require expected finding labels")
        else:
            if len(self.reference_reviewers) != 2:
                raise ValueError("expected finding labels require two reference reviewers")
            if len(self.expected_finding_keys) != len(set(self.expected_finding_keys)):
                raise ValueError("expected finding keys must be unique")
            if any(not key or len(key) > 512 for key in self.expected_finding_keys):
                raise ValueError("expected finding key is invalid")
        return self


class RealEvaluationDataset(RealEvaluationModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    dataset_id: str = Field(min_length=1, max_length=200)
    samples: tuple[RealModelSample, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_unique_samples(self) -> Self:
        sample_ids = [item.sample_id for item in self.samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("real evaluation sample ids must be unique")
        return self


@dataclass(slots=True)
class _MetricsAccumulator:
    sample_count: int = 0
    finding_count: int = 0
    adjudicated_count: int = 0
    unadjudicated_count: int = 0
    disagreement_count: int = 0
    location_assessed_count: int = 0
    location_correct_count: int = 0
    location_disagreement_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    reference_sample_count: int = 0
    reference_expected_count: int = 0
    reference_true_positive_count: int = 0
    reference_false_negative_count: int = 0
    reference_unexpected_valid_count: int = 0
    verdicts: Counter[FindingEvaluationVerdict] = field(default_factory=Counter)

    def add(self, sample: RealModelSample) -> None:
        self.sample_count += 1
        self.finding_count += len(sample.findings)
        self.input_tokens += sample.input_tokens
        self.output_tokens += sample.output_tokens
        self.latency_ms += sample.latency_ms
        self.cost_usd += sample.cost_usd
        final_valid_keys: set[str] = set()
        for finding in sample.findings:
            if len(finding.adjudications) < 2:
                self.unadjudicated_count += 1
                continue
            verdict = finding.final_verdict
            if verdict is None:
                self.disagreement_count += 1
                continue
            self.adjudicated_count += 1
            self.verdicts[verdict] += 1
            if verdict is FindingEvaluationVerdict.VALID:
                final_valid_keys.add(finding.finding_key)
            location_values = {
                item.location_correct
                for item in finding.adjudications
                if item.location_correct is not None
            }
            if len(location_values) > 1:
                self.location_disagreement_count += 1
            elif finding.location_result is not None:
                self.location_assessed_count += 1
                self.location_correct_count += int(finding.location_result)
        if sample.expected_finding_keys is not None:
            expected = set(sample.expected_finding_keys)
            self.reference_sample_count += 1
            self.reference_expected_count += len(expected)
            self.reference_true_positive_count += len(expected & final_valid_keys)
            self.reference_false_negative_count += len(expected - final_valid_keys)
            self.reference_unexpected_valid_count += len(final_valid_keys - expected)

    def report(self) -> dict[str, object]:
        precision = _ratio(
            self.verdicts[FindingEvaluationVerdict.VALID],
            self.adjudicated_count,
        )
        recall = _ratio(
            self.reference_true_positive_count,
            self.reference_expected_count,
        )
        return {
            "sample_count": self.sample_count,
            "finding_count": self.finding_count,
            "adjudicated_count": self.adjudicated_count,
            "unadjudicated_count": self.unadjudicated_count,
            "disagreement_count": self.disagreement_count,
            "valid_count": self.verdicts[FindingEvaluationVerdict.VALID],
            "false_positive_count": self.verdicts[
                FindingEvaluationVerdict.FALSE_POSITIVE
            ],
            "duplicate_count": self.verdicts[FindingEvaluationVerdict.DUPLICATE],
            "out_of_scope_count": self.verdicts[
                FindingEvaluationVerdict.OUT_OF_SCOPE
            ],
            "known_issue_count": self.verdicts[
                FindingEvaluationVerdict.KNOWN_ISSUE
            ],
            "precision": precision,
            "precision_ci95": _wilson_interval(
                self.verdicts[FindingEvaluationVerdict.VALID],
                self.adjudicated_count,
            ),
            "duplicate_rate": _ratio(
                self.verdicts[FindingEvaluationVerdict.DUPLICATE],
                self.adjudicated_count,
            ),
            "location_assessed_count": self.location_assessed_count,
            "location_correct_count": self.location_correct_count,
            "location_disagreement_count": self.location_disagreement_count,
            "location_accuracy": _ratio(
                self.location_correct_count,
                self.location_assessed_count,
            ),
            "reference_sample_count": self.reference_sample_count,
            "reference_expected_count": self.reference_expected_count,
            "reference_true_positive_count": self.reference_true_positive_count,
            "reference_false_negative_count": self.reference_false_negative_count,
            "reference_unexpected_valid_count": self.reference_unexpected_valid_count,
            "recall": recall,
            "recall_ci95": _wilson_interval(
                self.reference_true_positive_count,
                self.reference_expected_count,
            ),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_cost_usd": round(self.cost_usd, 8),
            "mean_cost_usd": (
                round(self.cost_usd / self.sample_count, 8)
                if self.sample_count
                else None
            ),
            "mean_latency_ms": (
                round(self.latency_ms / self.sample_count, 3)
                if self.sample_count
                else None
            ),
        }


def summarize_real_evaluation(dataset: RealEvaluationDataset) -> dict[str, object]:
    """单次遍历生成总体和 provider/model/prompt 分组报告。"""

    overall = _MetricsAccumulator()
    grouped: dict[tuple[str, str, str], _MetricsAccumulator] = {}
    for sample in dataset.samples:
        overall.add(sample)
        key = (sample.provider.value, sample.model, sample.prompt_version)
        grouped.setdefault(key, _MetricsAccumulator()).add(sample)
    return {
        "schema_version": 1,
        "execution_mode": "real_model_observation",
        "dataset_id": dataset.dataset_id,
        "overall": overall.report(),
        "groups": [
            {
                "provider": key[0],
                "model": key[1],
                "prompt_version": key[2],
                **grouped[key].report(),
            }
            for key in sorted(grouped)
        ],
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 8) if denominator else None


def _wilson_interval(successes: int, total: int) -> dict[str, float] | None:
    if total == 0:
        return None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * sqrt(
            proportion * (1 - proportion) / total
            + z * z / (4 * total * total)
        )
        / denominator
    )
    return {
        "lower": round(max(0.0, center - margin), 8),
        "upper": round(min(1.0, center + margin), 8),
    }
