"""供应商无关的模型审查输入、输出和计量契约。"""

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain.enums import (
    EvidenceVerificationStatus,
    FindingCategory,
    LocationSide,
    ModelApiProtocol,
    ModelCallStatus,
    ModelProvider,
    ModelReviewVerdict,
    ReviewAgent,
    Severity,
    VerificationStatus,
)
from domain.identifiers import build_review_version_key, normalize_sha
from domain.models import FindingLocation, ReviewFinding
from domain.paths import normalize_repository_path
from domain.repository_policy import RepositoryPolicySnapshot
from domain.retrieval import ContextEvidence
from domain.review_planning import RepositoryRule, ReviewUnit

PROMPT_VERSION = "structured-review-v5"
MAX_MODEL_FINDINGS = 200
MAX_MODEL_CHECKED_AREAS = 12
MAX_MODEL_SUMMARY_LENGTH = 4_000
POSTGRES_INTEGER_MAX = 2_147_483_647
POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


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

    context_references: tuple[str, ...] = Field(default=(), max_length=8)
    unit_key: str = Field(pattern=r"^(?:[0-9a-f]{64}|u_[1-9][0-9]{0,3})$")
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
    # 可选的机器稳定身份提示；它不展示给用户，也不允许包含控制字符。
    identity_hint: str | None = Field(default=None, max_length=256)

    @field_validator("rule_reference")
    @classmethod
    def validate_rule_reference(cls, value: str | None) -> str | None:
        return normalize_repository_path(value) if value is not None else None

    @field_validator("identity_hint")
    @classmethod
    def validate_identity_hint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("model finding identity_hint is invalid")
        return normalized


class ModelReviewOutput(ModelContract):
    """两个供应商共同返回的严格结构化输出。"""

    # 这三个字段带默认值只为兼容升级前已经持久化的批次 JSON；新模型响应的
    # 自定义 JSON Schema 会把它们全部声明为必填。
    verdict: ModelReviewVerdict | None = None
    summary: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_MODEL_SUMMARY_LENGTH,
    )
    checked_areas: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_MODEL_CHECKED_AREAS,
    )
    findings: tuple[ModelFindingCandidate, ...] = Field(
        max_length=MAX_MODEL_FINDINGS
    )

    @field_validator("checked_areas")
    @classmethod
    def validate_checked_areas(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or len(value) > 300 for value in values):
            raise ValueError("model checked area must contain 1 to 300 characters")
        if len(values) != len(set(values)):
            raise ValueError("model checked areas must be unique")
        return values

    @model_validator(mode="after")
    def validate_conclusion_shape(self) -> Self:
        if self.verdict is None:
            if self.summary is not None or self.checked_areas:
                raise ValueError("legacy model output cannot contain a partial conclusion")
            return self
        if self.summary is None:
            raise ValueError("model verdict must include a summary")
        if (
            self.verdict is ModelReviewVerdict.NO_ACTIONABLE_ISSUE
            and self.findings
        ):
            raise ValueError("no-actionable-issue output cannot contain findings")
        if self.verdict is ModelReviewVerdict.ISSUES_FOUND and not self.findings:
            raise ValueError("issues-found output must contain at least one finding")
        return self


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

    context_evidence: tuple[ContextEvidence, ...] = Field(default=(), max_length=60, exclude=True)
    repository_policy: RepositoryPolicySnapshot | None = Field(default=None, exclude=True)
    knowledge_versions: dict[str, str] = Field(default_factory=dict, exclude=True)
    reuse_dependencies: dict[str, str] = Field(default_factory=dict, exclude=True)
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
    review_agent: ReviewAgent | None = Field(default=None, exclude=True)
    # 以下标记只由应用边界注入，不参与 Review Plan 指纹。连接测试和真实
    # 批次需要不同的副作用策略：前者只能发送一次最小请求，后者在输出
    # 截断时由 Worker 拆分输入，而不是重复发送同一份大请求。
    connection_test: bool = Field(default=False, exclude=True)
    allow_truncation_retry: bool = Field(default=True, exclude=True)
    evaluation_batch_number: int | None = Field(default=None, ge=1, exclude=True)
    evaluation_split_depth: int = Field(default=0, ge=0, le=8, exclude=True)

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
        if any(item.head_sha != self.head_sha for item in self.context_evidence):
            raise ValueError("retrieval context does not match review SHA")
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
        if self.planner_version == "review-planner-v3":
            group_first_file: dict[str | None, str] = {}
            for unit in self.units:
                current = group_first_file.get(unit.group_key)
                if current is None or unit.file < current:
                    group_first_file[unit.group_key] = unit.file
            expected_units = sorted(
                self.units,
                key=lambda unit: (group_first_file[unit.group_key], unit.file),
            )
            if list(self.units) != expected_units:
                raise ValueError("model review units must keep related files adjacent")
        elif unit_files != sorted(unit_files):
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
        if self.planner_version in {"review-planner-v2", "review-planner-v3"}:
            expected_input_bytes += sum(rule.byte_size for rule in self.rules)
        if expected_input_bytes != self.total_estimated_input_bytes:
            raise ValueError("model review input byte total does not match its units")
        return self


class ReviewExecutionProvenance(ModelContract):
    application_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    knowledge_versions: dict[str, str] | None = None


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
    provenance: ReviewExecutionProvenance | None = None
    reused_from_run_id: str | None = Field(default=None, max_length=36)
    reused_input_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_status_shape(self) -> Self:
        if self.status is ModelCallStatus.SKIPPED:
            if self.response_status is not None or self.provider_response_id is not None:
                raise ValueError("skipped model calls cannot contain response identity")
            if (
                self.output.findings
                or self.output.verdict is not None
                or self.output.summary is not None
                or self.output.checked_areas
                or self.usage.total_input_tokens
                or self.usage.output_tokens
            ):
                raise ValueError("skipped model calls cannot contain output or usage")
        elif self.response_status is None and self.reused_from_run_id is None:
            raise ValueError("successful model calls must contain an HTTP status")
        return self


@dataclass(frozen=True, slots=True)
class MaterializedFinding:
    """平台补齐身份后的 Finding 及其来源 Review Unit。"""

    source_unit_key: str
    finding: ReviewFinding


@dataclass(frozen=True, slots=True)
class _DiffLocationIndex:
    left_lines: frozenset[int]
    right_lines: frozenset[int]

    def contains(self, start_line: int, end_line: int, side: LocationSide) -> bool:
        lines = self.left_lines if side is LocationSide.LEFT else self.right_lines
        line_count = end_line - start_line + 1
        return (
            line_count <= len(lines)
            and start_line in lines
            and end_line in lines
            and all(line in lines for line in range(start_line, end_line + 1))
        )


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
            "identity_hint": nullable_string,
            "context_references": {"type": "array", "items": {"type": "string"}},
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
            "identity_hint",
            "context_references",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {
                "type": "string",
                "enum": [item.value for item in ModelReviewVerdict],
            },
            "summary": {"type": "string"},
            "checked_areas": {
                "type": "array",
                "items": {"type": "string"},
            },
            "findings": {
                "type": "array",
                "items": finding,
            }
        },
        "required": ["verdict", "summary", "checked_areas", "findings"],
    }


def normalize_model_references(review_input: ModelReviewInput, output: ModelReviewOutput) -> ModelReviewOutput:
    """把本次请求中的短引用还原为真实身份，拒绝模型编造的引用。"""
    aliases = {f"ctx_{number}": item.reference_id for number, item in enumerate(review_input.context_evidence, 1)}
    known_context = set(aliases.values())
    known_rules = {rule.path for rule in review_input.rules} | set(review_input.knowledge_versions)
    units = {unit.unit_key: unit for unit in review_input.units}
    unit_aliases = {f"u_{number}": unit.unit_key for number, unit in enumerate(review_input.units, 1)}
    findings = []
    for candidate in output.findings:
        references = tuple(dict.fromkeys(aliases.get(key, key) for key in candidate.context_references))
        if not set(references) <= known_context:
            raise ValueError("model finding references unknown retrieval evidence")
        rule = candidate.rule_reference
        if rule is not None and rule not in known_rules:
            # 规则字段保存文件路径；仅移除已提供文档的章节/版本后缀，不接受新来源。
            rule = rule.split("#", 1)[0].split("@", 1)[0]
        if rule is not None and rule not in known_rules:
            raise ValueError("model finding references an unknown repository rule")
        unit_key = unit_aliases.get(candidate.unit_key, candidate.unit_key)
        unit = units.get(unit_key)
        if unit is None:
            raise ValueError("model finding references an unknown review unit")
        if candidate.location is not None and candidate.location.file != unit.file:
            raise ValueError("model finding location does not match its review unit")
        findings.append(candidate.model_copy(update={"unit_key": unit_key, "rule_reference": rule, "context_references": references}))
    return output.model_copy(update={"findings": tuple(findings)})


def materialize_findings(
    review_input: ModelReviewInput,
    output: ModelReviewOutput,
) -> tuple[MaterializedFinding, ...]:
    """校验模型引用，并用可信 diff 补齐身份和定位复核状态。"""

    units_by_key = {unit.unit_key: unit for unit in review_input.units}
    diff_locations_by_key = {
        unit.unit_key: _index_diff_locations(unit.patch) for unit in review_input.units
    }
    known_rule_paths = {rule.path for rule in review_input.rules} | set(review_input.knowledge_versions)
    known_context = {item.reference_id for item in review_input.context_evidence}
    verification_rank = {
        VerificationStatus.REJECTED: 0,
        VerificationStatus.UNVERIFIED: 1,
        VerificationStatus.VERIFIED: 2,
    }
    findings_by_fingerprint: dict[str, MaterializedFinding] = {}
    for candidate in output.findings:
        if not set(candidate.context_references) <= known_context:
            raise ValueError("model finding references unknown retrieval evidence")
        unit = units_by_key.get(candidate.unit_key)
        if unit is None:
            raise ValueError("model finding references an unknown review unit")
        if candidate.rule_reference is not None and candidate.rule_reference not in known_rule_paths:
            raise ValueError("model finding references an unknown repository rule")
        location = None
        verification_status = VerificationStatus.UNVERIFIED
        evidence_status = EvidenceVerificationStatus.UNVERIFIED
        evidence_reason = "finding_has_no_location"
        if candidate.location is not None:
            if candidate.location.file != unit.file:
                raise ValueError("model finding location does not match its review unit")
            in_diff = diff_locations_by_key[unit.unit_key].contains(
                candidate.location.start_line,
                candidate.location.end_line,
                candidate.location.side,
            )
            location = FindingLocation(
                file=unit.file,
                blob_sha=unit.blob_sha,
                start_line=candidate.location.start_line,
                end_line=candidate.location.end_line,
                side=candidate.location.side,
                in_diff=in_diff,
                symbol=candidate.location.symbol,
            )
            verification_status = (
                VerificationStatus.VERIFIED
                if in_diff
                else VerificationStatus.REJECTED
            )
            evidence_reason = "not_checked" if in_diff else "location_not_in_diff"
        fingerprint = finding_identity_fingerprint(candidate, unit.file)
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
                verification_status=verification_status,
                evidence_verification_status=evidence_status,
                evidence_verification_reason=evidence_reason,
                rule_reference=candidate.rule_reference,
                context_references=candidate.context_references,
            ),
        )
        existing = findings_by_fingerprint.get(fingerprint)
        if existing is None or (
            verification_rank[materialized.finding.verification_status],
            materialized.finding.confidence,
        ) > (
            verification_rank[existing.finding.verification_status],
            existing.finding.confidence,
        ):
            findings_by_fingerprint[fingerprint] = materialized
    return tuple(findings_by_fingerprint[key] for key in sorted(findings_by_fingerprint))


def finding_identity_fingerprint(
    candidate: ModelFindingCandidate,
    unit_file: str,
) -> str:
    """生成跨提交稳定身份；不把模型原始长文本作为主键。"""

    location_symbol = candidate.location.symbol if candidate.location else None
    identity_hint = _normalize_identity_text(candidate.identity_hint)
    rule_reference = _normalize_identity_text(candidate.rule_reference)
    symbol = _normalize_identity_text(location_symbol)
    # 只有模型明确提供 identity_hint 时才认为身份足够稳定，可以跨文件和
    # 自然语言措辞复用；其余情况保留标题、证据签名和文件名，避免同一规则
    # 或函数下的多个不同问题被错误合并。
    evidence_signature = _stable_evidence_signature(candidate.evidence)
    has_stable_identity = bool(identity_hint)
    identity = {
        "version": 3,
        "category": candidate.category.value,
        "rule_reference": rule_reference,
        "symbol": symbol,
        "identity_hint": identity_hint,
    }
    if has_stable_identity:
        # 稳定提示已经足够区分问题；不再把模型自然语言绑定进主键，
        # 避免同一问题因措辞变化产生新的生命周期记录。
        identity["identity_basis"] = "stable"
    else:
        identity["identity_basis"] = "evidence"
        identity["evidence_signature"] = evidence_signature
        identity["file"] = normalize_repository_path(unit_file)
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


_EVIDENCE_STOP_WORDS = frozenset(
    {
        "the",
        "this",
        "that",
        "with",
        "from",
        "into",
        "当前",
        "代码",
        "问题",
        "导致",
        "可能",
        "存在",
        "使用",
    }
)


def _stable_evidence_signature(value: str) -> str:
    """把证据压缩成顺序无关的短签名，降低模型措辞漂移的影响。"""

    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", value.casefold())
    normalized = sorted(
        {
            token
            for token in tokens
            if token not in _EVIDENCE_STOP_WORDS and len(token) >= 2
        }
    )
    return " ".join(normalized[:48])


def _index_diff_locations(patch: str) -> _DiffLocationIndex:
    left_lines: set[int] = set()
    right_lines: set[int] = set()
    left_line: int | None = None
    right_line: int | None = None
    left_remaining = 0
    right_remaining = 0
    valid = True
    for line in patch.splitlines():
        match = _HUNK_HEADER.match(line)
        if match is not None:
            if left_remaining or right_remaining:
                valid = False
                break
            left_line = int(match.group(1))
            right_line = int(match.group(3))
            left_remaining = int(match.group(2) or "1")
            right_remaining = int(match.group(4) or "1")
            continue
        if left_line is None or right_line is None:
            continue
        if line.startswith("\\ No newline at end of file"):
            continue
        if line.startswith("+"):
            if right_remaining <= 0:
                valid = False
                break
            right_lines.add(right_line)
            right_line += 1
            right_remaining -= 1
        elif line.startswith("-"):
            if left_remaining <= 0:
                valid = False
                break
            left_lines.add(left_line)
            left_line += 1
            left_remaining -= 1
        elif line.startswith(" "):
            if left_remaining <= 0 or right_remaining <= 0:
                valid = False
                break
            left_lines.add(left_line)
            right_lines.add(right_line)
            left_line += 1
            right_line += 1
            left_remaining -= 1
            right_remaining -= 1
        else:
            valid = False
            break
    if left_remaining or right_remaining:
        valid = False
    if not valid:
        return _DiffLocationIndex(frozenset(), frozenset())
    return _DiffLocationIndex(frozenset(left_lines), frozenset(right_lines))


def _normalize_identity_text(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.casefold().split())
