"""真实审查快照、人工复核和配对评测的纯数据契约。"""

from collections import Counter
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain.enums import FindingCategory, FindingEvaluationVerdict, Severity
from domain.paths import normalize_repository_path
from domain.security import redact_text

EvaluationSplit = Literal["tuning", "validation"]
EvaluationVariant = Literal["baseline", "candidate"]
EvaluationKind = Literal["normal", "known_defect", "cross_file"]
AssessmentStatus = Literal["pending", "partial", "disputed", "complete"]
MAX_EVALUATION_CASES = 200
MAX_EVALUATION_FINDINGS = 500


class EvaluationNotFoundError(LookupError):
    pass


class EvaluationConflictError(ValueError):
    pass


class EvaluationContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ReferenceDefect(EvaluationContract):
    key: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    title: str = Field(min_length=1, max_length=300)
    category: FindingCategory
    file: str | None = Field(default=None, max_length=1024)
    start_line: int | None = Field(default=None, ge=1)

    @field_validator("file")
    @classmethod
    def validate_file(cls, value: str | None) -> str | None:
        return normalize_repository_path(value) if value else None

    @field_validator("title")
    @classmethod
    def redact_title(cls, value: str) -> str:
        return redact_text(value)


class EvaluationFinding(EvaluationContract):
    id: str
    fingerprint: str
    title: str
    severity: Severity
    category: FindingCategory
    file: str | None
    start_line: int | None
    end_line: int | None
    evidence: str
    impact: str
    suggestion: str
    confidence: float
    location_status: str
    evidence_status: str
    context_references: tuple[str, ...] = ()


class EvaluationDecision(EvaluationContract):
    verdict: FindingEvaluationVerdict | Literal["uncertain"]
    location_correct: bool | None = None
    reference_key: str | None = Field(default=None, max_length=80)
    note: str = Field(default="", max_length=1000)

    @field_validator("note")
    @classmethod
    def redact_note(cls, value: str) -> str:
        return redact_text(value)


class EvaluationBallot(EvaluationContract):
    reviewer: str
    decisions: dict[str, EvaluationDecision] = Field(
        default_factory=dict, max_length=MAX_EVALUATION_FINDINGS,
    )
    submitted_at: datetime | None = None
    updated_at: datetime


class ReferenceReview(EvaluationContract):
    reviewer: str
    agrees: bool
    note: str = Field(default="", max_length=1000)
    reviewed_at: datetime


class ModelVersion(EvaluationContract):
    agent: str
    provider: str
    model: str
    protocol: str
    prompt_version: str
    application_revision: str | None = None
    knowledge_versions: dict[str, str] = Field(default_factory=dict)
    context_recorded: bool = False
    reused_from_run_id: str | None = None


class RetrievalVersion(EvaluationContract):
    agent: str | None
    index_id: str
    index_head_sha: str
    embedding_model: str
    strategy: str | None = None


class EvaluationChange(EvaluationContract):
    file: str
    blob_sha: str
    patch: str


class EvaluationSource(EvaluationContract):
    source_kind: Literal["normalized_review_run"] = "normalized_review_run"
    review_run_id: str
    installation_id: int
    repository_id: int
    repository: str
    repository_key: str
    pull_request_number: int
    head_sha: str
    title: str
    plan_fingerprint: str
    planner_version: str
    configuration_revision: int | None
    repository_policy: dict[str, object] | None
    rule_versions: tuple[dict[str, str], ...] = ()
    models: tuple[ModelVersion, ...] = ()
    retrieval: tuple[RetrievalVersion, ...] = ()
    findings: tuple[EvaluationFinding, ...] = Field(max_length=MAX_EVALUATION_FINDINGS)
    changes: tuple[EvaluationChange, ...] = Field(default=(), max_length=500)
    input_tokens: int
    output_tokens: int
    model_duration_ms: int
    turnaround_ms: int
    estimated_cost_microusd: int | None
    completed_at: datetime
    captured_at: datetime
    limitations: tuple[str, ...] = ()


class EvaluationSourceMetadata(EvaluationSource):
    findings: tuple[EvaluationFinding, ...] = Field(default=(), exclude=True)
    changes: tuple[EvaluationChange, ...] = Field(default=(), exclude=True)
    change_count: int = 0


class EvaluationDatasetView(EvaluationContract):
    id: str
    name: str
    repository: str
    case_count: int
    revision: int
    archived_at: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime


class EvaluationRunOption(EvaluationContract):
    review_run_id: str
    repository: str
    pull_request_number: int
    head_sha: str
    title: str | None
    finding_count: int
    model: str
    completed_at: datetime
    created_at: datetime


class EvaluationAuditView(EvaluationContract):
    id: str
    event_type: str
    payload: dict[str, object]
    occurred_at: datetime


class ObservationView(EvaluationContract):
    id: str
    variant: EvaluationVariant
    source_run_id: str
    snapshot_sha256: str
    model_label: str
    provenance_complete: bool
    finding_count: int
    assessment_status: AssessmentStatus
    revision: int
    captured_by: str
    created_at: datetime
    updated_at: datetime


class EvaluationCaseView(EvaluationContract):
    id: str
    dataset_id: str
    repository: str
    pull_request_number: int
    head_sha: str
    title: str
    split: EvaluationSplit
    kind: EvaluationKind
    reference_status: str
    reference_count: int | None
    revision: int
    created_at: datetime
    updated_at: datetime
    baseline: ObservationView | None
    candidate: ObservationView | None


class EvaluationCaseDetail(EvaluationCaseView):
    reference_defects: tuple[ReferenceDefect, ...] | None
    reference_reviews: tuple[ReferenceReview, ...]


class BallotSummary(EvaluationContract):
    reviewer: str
    decision_count: int
    submitted_at: datetime | None
    updated_at: datetime


class ObservationDetail(EvaluationContract):
    observation: ObservationView
    source: EvaluationSourceMetadata
    ballots: tuple[BallotSummary, ...]


class FindingReview(EvaluationContract):
    reviewer: str
    decision: EvaluationDecision
    submitted_at: datetime | None


class EvaluationFindingView(EvaluationContract):
    finding: EvaluationFinding
    reviews: tuple[FindingReview, ...]


class ObservationImport(EvaluationContract):
    review_run_ids: tuple[str, ...] = Field(min_length=1, max_length=30)
    variant: EvaluationVariant = "baseline"
    split: EvaluationSplit = "tuning"
    kind: EvaluationKind = "normal"

    @field_validator("review_run_ids")
    @classmethod
    def validate_runs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or len(value) > 36 for value in values) or len(set(values)) != len(values):
            raise ValueError("运行标识无效或重复")
        return tuple(sorted(values))


class EvaluationDatasetCreate(ObservationImport):
    name: str = Field(min_length=1, max_length=120)


class EvaluationOverview(EvaluationContract):
    review_source: str = "单人核对，以最近保存的判断为准"
    uncertain_findings: int = 0
    model_duration_ms: int = 0
    turnaround_ms: int = 0
    estimated_cost_microusd: int | None = None
    unpriced_observations: int = 0
    case_count: int
    observation_count: int
    reviewed_observations: int
    reviewed_findings: int
    valid_findings: int
    false_positive_findings: int
    unreviewed_findings: int
    missing_reference_cases: int


class EvaluationRevision(EvaluationContract):
    expected_revision: int = Field(ge=1)


class ReferenceUpdate(EvaluationRevision):
    reference_defects: tuple[ReferenceDefect, ...] | None = Field(default=None, max_length=30)
    kind: EvaluationKind | None = None
    reset_reviews: bool = False

    @field_validator("reference_defects")
    @classmethod
    def unique_keys(cls, values: tuple[ReferenceDefect, ...] | None):
        if values is not None and len({item.key for item in values}) != len(values):
            raise ValueError("参考缺陷标识不能重复")
        return values


class ReferenceReviewWrite(EvaluationRevision):
    agrees: bool
    note: str = Field(default="", max_length=1000)


class FindingReviewWrite(EvaluationRevision):
    decision: EvaluationDecision


class ObservationReplace(EvaluationRevision):
    review_run_id: str = Field(min_length=1, max_length=36)
    reset_reviews: bool = False


class EvaluationArchive(EvaluationRevision):
    archived: bool


class EvaluationImportResult(EvaluationContract):
    dataset_id: str
    case_ids: tuple[str, ...]
    imported: int


class EvaluationScore(EvaluationContract):
    sample_count: int
    finding_count: int
    valid_count: int
    false_positive_count: int
    duplicate_count: int
    out_of_scope_count: int
    known_issue_count: int
    precision: float | None
    precision_ci95: dict[str, float] | None
    recall: float | None
    recall_ci95: dict[str, float] | None
    location_accuracy: float | None
    duplicate_rate: float | None
    reference_expected_count: int
    reference_true_positive_count: int
    reference_false_negative_count: int
    reference_unexpected_valid_count: int
    mean_turnaround_ms: float | None
    mean_model_duration_ms: float | None
    mean_estimated_cost_usd: float | None
    input_tokens: int
    output_tokens: int
    configuration_count: int


class EvaluationComparisonReport(EvaluationContract):
    metric_scope: Literal["paired_review_workflow"] = "paired_review_workflow"
    dataset_id: str
    dataset_name: str
    repository: str
    split: EvaluationSplit
    generated_at: datetime
    data_version: str
    case_count: int
    normal_count: int
    known_defect_count: int
    cross_file_count: int
    performance_pairs: int
    quality_pairs: int
    reference_pairs: int
    priced_pairs: int
    missing_baseline: int
    missing_candidate: int
    pending_pairs: int
    disputed_pairs: int
    identical_run_pairs: int
    provenance_missing_pairs: int
    baseline: EvaluationScore
    candidate: EvaluationScore
    deltas: dict[str, float | None]
    notices: tuple[str, ...]


def assessment_metrics(
    findings: tuple[EvaluationFinding, ...],
    ballots: tuple[EvaluationBallot, ...],
    references: tuple[ReferenceDefect, ...] | None,
) -> tuple[AssessmentStatus, dict[str, int]]:
    """使用最近一位核对者保存的判断；不确定和未核对不计为有效。"""
    counts: dict[str, int] = {
        "finding_count": len(findings), "adjudicated_count": 0,
        "unadjudicated_count": len(findings), "disagreement_count": 0,
        "valid_count": 0, "false_positive_count": 0, "duplicate_count": 0,
        "out_of_scope_count": 0, "known_issue_count": 0,
        "location_assessed_count": 0, "location_correct_count": 0,
        "reference_true_positive_count": 0, "reference_unexpected_valid_count": 0,
    }
    if not ballots:
        return "pending", counts
    latest = max(reversed(ballots), key=lambda ballot: ballot.updated_at)
    expected = {item.key for item in references or ()}
    matched: set[str] = set()
    verdicts: Counter[str] = Counter()
    for finding in findings:
        left = latest.decisions.get(finding.id)
        if left is None or left.verdict == "uncertain":
            continue
        verdicts[str(left.model_dump(mode="json")["verdict"])] += 1
        counts["adjudicated_count"] += 1
        if left.location_correct is not None:
            counts["location_assessed_count"] += 1
            counts["location_correct_count"] += int(left.location_correct)
        if left.verdict is FindingEvaluationVerdict.VALID:
            if left.reference_key is not None and left.reference_key in expected:
                matched.add(left.reference_key)
            else:
                counts["reference_unexpected_valid_count"] += 1
    for verdict in FindingEvaluationVerdict:
        counts[f"{verdict.value}_count"] = verdicts[verdict.value]
    counts["unadjudicated_count"] = (
        len(findings) - counts["adjudicated_count"] - counts["disagreement_count"]
    )
    counts["reference_true_positive_count"] = len(matched)
    counts["uncertain_count"] = sum(item.verdict == "uncertain" for item in latest.decisions.values())
    status: AssessmentStatus = "complete" if latest.submitted_at is not None else "partial"
    return status, counts


def reference_status(reviews: tuple[ReferenceReview, ...]) -> str:
    if not reviews:
        return "pending"
    return "confirmed" if max(reviews, key=lambda review: review.reviewed_at).agrees else "pending"
