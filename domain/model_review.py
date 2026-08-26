"""供应商无关的模型审查输入、输出和计量契约。"""

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain.enums import (
    FindingCategory,
    LocationSide,
    ModelApiProtocol,
    ModelCallStatus,
    ModelProvider,
    Severity,
    VerificationStatus,
)
from domain.identifiers import build_review_version_key, normalize_sha
from domain.models import FindingLocation, ReviewFinding
from domain.paths import normalize_repository_path
from domain.review_planning import RepositoryRule, ReviewUnit


PROMPT_VERSION = "structured-review-v1"
MAX_MODEL_FINDINGS = 200
POSTGRES_INTEGER_MAX = 2_147_483_647
POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807


class ModelContract(BaseModel):
    """模型边界使用的严格、不可变基础契约。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ModelFindingLocation(ModelContract):
    """模型声明的位置；blob 身份和 diff 复核由平台补齐。"""

    file: str = Field(min_length=1, max_length=1024)
    start_line: int = Field(gt=0, le=POSTGRES_INTEGER_MAX)
    end_line: int = Field(gt=0, le=POSTGRES_INTEGER_MAX)
    side: LocationSide
    symbol: str | None = Field(max_length=512)

    @field_validator("file")
    @classmethod
    def validate_file(cls, value: str) -> str:
        return normalize_repository_path(value)

    @model_validator(mode="after")
    def validate_line_range(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("model finding end line must not precede start line")
        return self


class ModelFindingCandidate(ModelContract):
    """模型可生成的 Finding 候选，不含平台拥有的身份和复核字段。"""

    unit_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    severity: Severity
    category: FindingCategory
    location: ModelFindingLocation | None
    title: str = Field(min_length=1, max_length=300)
    evidence: str = Field(min_length=1, max_length=10_000)
    impact: str = Field(min_length=1, max_length=10_000)
    suggestion: str = Field(min_length=1, max_length=10_000)
    required_test: str | None = Field(max_length=10_000)
    confidence: float = Field(ge=0, le=1)
    rule_reference: str | None = Field(max_length=1024)

    @field_validator("rule_reference")
    @classmethod
    def validate_rule_reference(cls, value: str | None) -> str | None:
        return normalize_repository_path(value) if value is not None else None


class ModelReviewOutput(ModelContract):
    """两个供应商共同返回的严格结构化输出。"""

    findings: tuple[ModelFindingCandidate, ...] = Field(
        max_length=MAX_MODEL_FINDINGS
    )


class ModelTokenUsage(ModelContract):
    """按计费类别归一化的 Token 用量。"""

    input_tokens: int = Field(ge=0, le=POSTGRES_INTEGER_MAX)
    output_tokens: int = Field(ge=0, le=POSTGRES_INTEGER_MAX)
    cache_read_input_tokens: int = Field(
        default=0,
        ge=0,
        le=POSTGRES_INTEGER_MAX,
    )
    cache_write_input_tokens: int = Field(
        default=0,
        ge=0,
        le=POSTGRES_INTEGER_MAX,
    )
    reasoning_output_tokens: int = Field(
        default=0,
        ge=0,
        le=POSTGRES_INTEGER_MAX,
    )

    @model_validator(mode="after")
    def reasoning_is_part_of_output(self) -> Self:
        if self.reasoning_output_tokens > self.output_tokens:
            raise ValueError("reasoning tokens cannot exceed output tokens")
        return self

    @property
    def total_input_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_input_tokens
            + self.cache_write_input_tokens
        )


class ModelReviewInput(ModelContract):
    """一次数据库批量读取生成的、绑定精确计划的模型输入。"""

    review_plan_id: str = Field(min_length=1, max_length=36)
    review_run_id: str = Field(min_length=1, max_length=36)
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    planner_version: str = Field(min_length=1, max_length=50)
    review_version_key: str = Field(min_length=44, max_length=400)
    repository_id: int = Field(gt=0)
    repository: str = Field(
        min_length=3,
        max_length=255,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    pull_request_number: int = Field(gt=0)
    head_sha: str = Field(min_length=40, max_length=64)
    rules: tuple[RepositoryRule, ...] = Field(max_length=256)
    units: tuple[ReviewUnit, ...] = Field(max_length=3000)
    total_estimated_input_bytes: int = Field(ge=0, le=100 * 1024 * 1024)
    # 这些字段只在模型调用前由编排器注入，不属于 Review Plan 身份或持久化快照。
    knowledge_references: tuple[str, ...] = Field(
        default=(),
        max_length=16,
        exclude=True,
    )
    prior_agent_results: tuple[str, ...] = Field(
        default=(),
        max_length=64,
        exclude=True,
    )

    @field_validator("knowledge_references", "prior_agent_results")
    @classmethod
    def validate_ephemeral_context(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 2_000 for value in values):
            raise ValueError("ephemeral model context item is invalid")
        return values

    @field_validator("head_sha")
    @classmethod
    def validate_head_sha(cls, value: str) -> str:
        return normalize_sha(value)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        expected_key = build_review_version_key(
            self.repository_id,
            self.pull_request_number,
            self.head_sha,
        )
        if self.review_version_key != expected_key:
            raise ValueError("model review version key does not match its target")
        rule_paths = [rule.path for rule in self.rules]
        if len(rule_paths) != len(set(rule_paths)):
            raise ValueError("model review rules must be unique")
        if rule_paths != sorted(rule_paths, key=lambda path: (path.count("/"), path)):
            raise ValueError("model review rules must be ordered")
        unit_keys = [unit.unit_key for unit in self.units]
        unit_files = [unit.file for unit in self.units]
        if len(unit_keys) != len(set(unit_keys)) or len(unit_files) != len(
            set(unit_files)
        ):
            raise ValueError("model review units must have unique keys and files")
        if unit_files != sorted(unit_files):
            raise ValueError("model review units must be ordered by file")
        known_rules = set(rule_paths)
        for unit in self.units:
            if (
                unit.review_version_key != self.review_version_key
                or unit.head_sha != self.head_sha
                or unit.planner_version != self.planner_version
                or not set(unit.rule_paths).issubset(known_rules)
            ):
                raise ValueError("model review unit identity does not match its plan")
        expected_input_bytes = sum(
            unit.estimated_input_bytes for unit in self.units
        )
        if self.planner_version == "review-planner-v2":
            expected_input_bytes += sum(rule.byte_size for rule in self.rules)
        if expected_input_bytes != self.total_estimated_input_bytes:
            raise ValueError("model review input byte total does not match its units")
        return self


class ModelReviewResult(ModelContract):
    """模型适配器完成一次调用后交给持久化层的统一结果。"""

    provider: ModelProvider
    api_protocol: ModelApiProtocol
    model: str = Field(min_length=1, max_length=200)
    status: ModelCallStatus
    prompt_version: str = Field(min_length=1, max_length=50)
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_response_id: str | None = Field(default=None, max_length=200)
    provider_request_id: str | None = Field(default=None, max_length=200)
    response_status: int | None = Field(default=None, ge=100, le=599)
    duration_ms: int = Field(ge=0, le=POSTGRES_INTEGER_MAX)
    usage: ModelTokenUsage
    estimated_cost_microusd: int | None = Field(
        default=None,
        ge=0,
        le=POSTGRES_BIGINT_MAX,
    )
    output: ModelReviewOutput

    @model_validator(mode="after")
    def validate_status_shape(self) -> Self:
        if self.status is ModelCallStatus.SKIPPED:
            if self.response_status is not None or self.provider_response_id is not None:
                raise ValueError("skipped model calls cannot contain response identity")
            if self.output.findings or self.usage.total_input_tokens or self.usage.output_tokens:
                raise ValueError("skipped model calls cannot contain output or usage")
        elif self.response_status is None:
            raise ValueError("successful model calls must contain an HTTP status")
        return self


@dataclass(frozen=True, slots=True)
class MaterializedFinding:
    """平台补齐身份后的 Finding 及其来源 Review Unit。"""

    source_unit_key: str
    finding: ReviewFinding


def model_review_output_schema() -> dict[str, object]:
    """返回 OpenAI 与 Anthropic 都支持的严格 JSON Schema 子集。"""

    nullable_string = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    location = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "file": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "side": {"type": "string", "enum": [item.value for item in LocationSide]},
            "symbol": nullable_string,
        },
        "required": ["file", "start_line", "end_line", "side", "symbol"],
    }
    finding = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "unit_key": {"type": "string"},
            "severity": {"type": "string", "enum": [item.value for item in Severity]},
            "category": {
                "type": "string",
                "enum": [item.value for item in FindingCategory],
            },
            "location": {"anyOf": [location, {"type": "null"}]},
            "title": {"type": "string"},
            "evidence": {"type": "string"},
            "impact": {"type": "string"},
            "suggestion": {"type": "string"},
            "required_test": nullable_string,
            "confidence": {"type": "number"},
            "rule_reference": nullable_string,
        },
        "required": [
            "unit_key",
            "severity",
            "category",
            "location",
            "title",
            "evidence",
            "impact",
            "suggestion",
            "required_test",
            "confidence",
            "rule_reference",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "findings": {
                "type": "array",
                "items": finding,
            }
        },
        "required": ["findings"],
    }


def materialize_findings(
    review_input: ModelReviewInput,
    output: ModelReviewOutput,
) -> tuple[MaterializedFinding, ...]:
    """校验模型引用，并补齐 SHA、blob、指纹和未复核状态。"""

    units_by_key = {unit.unit_key: unit for unit in review_input.units}
    known_rule_paths = {rule.path for rule in review_input.rules}
    findings_by_fingerprint: dict[str, MaterializedFinding] = {}
    for candidate in output.findings:
        unit = units_by_key.get(candidate.unit_key)
        if unit is None:
            raise ValueError("model finding references an unknown review unit")
        if candidate.rule_reference not in known_rule_paths | {None}:
            raise ValueError("model finding references an unknown repository rule")
        location = None
        if candidate.location is not None:
            if candidate.location.file != unit.file:
                raise ValueError("model finding location does not match its review unit")
            location = FindingLocation(
                file=unit.file,
                blob_sha=unit.blob_sha,
                start_line=candidate.location.start_line,
                end_line=candidate.location.end_line,
                side=candidate.location.side,
                in_diff=False,
                symbol=candidate.location.symbol,
            )
        fingerprint = _finding_fingerprint(candidate, unit.file)
        materialized = MaterializedFinding(
            source_unit_key=unit.unit_key,
            finding=ReviewFinding(
                fingerprint=fingerprint,
                head_sha=review_input.head_sha,
                severity=candidate.severity,
                category=candidate.category,
                location=location,
                title=candidate.title,
                evidence=candidate.evidence,
                impact=candidate.impact,
                suggestion=candidate.suggestion,
                required_test=candidate.required_test,
                confidence=candidate.confidence,
                verification_status=VerificationStatus.UNVERIFIED,
                rule_reference=candidate.rule_reference,
            ),
        )
        existing = findings_by_fingerprint.get(fingerprint)
        if existing is None or materialized.finding.confidence > existing.finding.confidence:
            findings_by_fingerprint[fingerprint] = materialized
    return tuple(findings_by_fingerprint[key] for key in sorted(findings_by_fingerprint))


def _finding_fingerprint(candidate: ModelFindingCandidate, unit_file: str) -> str:
    location_symbol = candidate.location.symbol if candidate.location else None
    identity = {
        "version": 1,
        "category": candidate.category.value,
        "file": unit_file,
        "symbol": _normalize_identity_text(location_symbol),
        "title": _normalize_identity_text(candidate.title),
        "rule_reference": candidate.rule_reference,
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _normalize_identity_text(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.casefold().split())
