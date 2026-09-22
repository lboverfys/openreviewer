"""仓库规则快照与模型调用前 Review Plan 的严格领域契约。"""

from collections.abc import Sequence
from hashlib import sha256
from typing import Literal, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from domain.enums import RepositoryRuleIssueKind, ReviewAgent, ReviewFileDecision
from domain.github import MAX_PATCH_BYTES
from domain.identifiers import build_review_version_key, normalize_sha
from domain.model_budget import ModelBudgetPolicy
from domain.paths import normalize_repository_path

REVIEW_AGENTS: tuple[ReviewAgent, ...] = (
    ReviewAgent.SECURITY,
    ReviewAgent.CONVENTION,
    ReviewAgent.LOGIC,
)
DEFAULT_REVIEW_DOMAINS: tuple[ReviewAgent, ...] = REVIEW_AGENTS

_DOCUMENT_SUFFIXES = {".md", ".mdx", ".rst", ".txt"}


def infer_review_domains(
    file_path: str,
    patch: str,
    rule_paths: tuple[str, ...] = (),
) -> tuple[ReviewAgent, ...]:
    """可审查代码固定经过三路，纯文档只检查规范；历史计划保留原职责。"""

    normalized_path = normalize_repository_path(file_path)
    suffix = normalized_path.casefold().rsplit(".", 1)
    is_document = (
        len(suffix) == 2 and f".{suffix[-1]}" in _DOCUMENT_SUFFIXES
    ) or normalized_path.casefold().endswith(("/readme", "/readme.md"))
    if is_document:
        return (ReviewAgent.CONVENTION,)

    return REVIEW_AGENTS


def repository_rule_candidate_paths(
    file_path: str,
    *,
    max_parent_scopes: int | None = None,
) -> tuple[str, ...]:
    """返回根到文件所在目录的 AGENTS.md 候选路径。"""

    normalized = normalize_repository_path(file_path)
    directories = normalized.split("/")[:-1]
    if max_parent_scopes is not None:
        if max_parent_scopes < 0:
            raise ValueError("maximum parent rule scopes cannot be negative")
        directories = directories[:max_parent_scopes]
    candidates = ["AGENTS.md"]
    for depth in range(1, len(directories) + 1):
        candidates.append(f"{'/'.join(directories[:depth])}/AGENTS.md")
    return tuple(candidates)


class PlanningContractModel(BaseModel):
    """拒绝未知字段、禁止修改，同时保留规则和 diff 的原始空白。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RepositoryRule(PlanningContractModel):
    """绑定精确提交和目录作用域的一份 AGENTS.md。"""

    path: str = Field(min_length=1, max_length=1024)
    scope: str | None = Field(default=None, min_length=1, max_length=1014)
    blob_sha: str = Field(min_length=40, max_length=64)
    content: str = Field(min_length=1, max_length=262_144)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0, le=262_144)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_repository_path(value)

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str | None) -> str | None:
        return normalize_repository_path(value) if value is not None else None

    @field_validator("blob_sha")
    @classmethod
    def validate_blob_sha(cls, value: str) -> str:
        return normalize_sha(value)

    @model_validator(mode="after")
    def validate_content_identity(self) -> Self:
        expected_path = "AGENTS.md" if self.scope is None else f"{self.scope}/AGENTS.md"
        if self.path != expected_path:
            raise ValueError("repository rule path must match its directory scope")
        encoded = self.content.encode("utf-8")
        if self.byte_size != len(encoded):
            raise ValueError("repository rule byte_size must match UTF-8 content")
        if self.content_sha256 != sha256(encoded).hexdigest():
            raise ValueError("repository rule content_sha256 must match content")
        return self


class RepositoryRuleIssue(PlanningContractModel):
    """一次规则加载被有界降级的原因，不包含 GitHub 原始错误文本。"""

    kind: RepositoryRuleIssueKind
    rule_path: str | None = Field(default=None, min_length=1, max_length=1024)
    affected_file_count: int = Field(gt=0, le=3000)

    @field_validator("rule_path")
    @classmethod
    def validate_rule_path(cls, value: str | None) -> str | None:
        return normalize_repository_path(value) if value is not None else None

    @model_validator(mode="after")
    def candidate_limit_has_no_single_path(self) -> Self:
        aggregate_kinds = {
            RepositoryRuleIssueKind.CANDIDATE_LIMIT,
            RepositoryRuleIssueKind.RESPONSE_TOO_LARGE,
            RepositoryRuleIssueKind.SCOPE_DEPTH_LIMIT,
        }
        if (
            self.kind in aggregate_kinds
            and self.rule_path is not None
        ):
            raise ValueError("aggregate rule limit issues must not include one path")
        if (
            self.kind not in aggregate_kinds
            and self.rule_path is None
        ):
            raise ValueError("rule-specific issues must include rule_path")
        return self


class RepositoryRulesSnapshot(PlanningContractModel):
    """一次 GraphQL 批量读取产生的规则、边界和不完整文件集合。"""

    repository_id: int = Field(gt=0)
    repository: str = Field(
        min_length=3,
        max_length=255,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    head_sha: str = Field(min_length=40, max_length=64)
    rules: tuple[RepositoryRule, ...] = Field(max_length=256)
    incomplete_files: tuple[str, ...] = Field(max_length=3000)
    issues: tuple[RepositoryRuleIssue, ...] = Field(max_length=257)
    candidate_count: int = Field(ge=0)
    requested_candidate_count: int = Field(ge=0, le=256)

    @field_validator("head_sha")
    @classmethod
    def validate_head_sha(cls, value: str) -> str:
        return normalize_sha(value)

    @field_validator("incomplete_files")
    @classmethod
    def validate_incomplete_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(normalize_repository_path(value) for value in values)

    @model_validator(mode="after")
    def validate_snapshot_shape(self) -> Self:
        if self.requested_candidate_count > self.candidate_count:
            raise ValueError("requested candidate count cannot exceed total candidates")
        rule_paths = [rule.path for rule in self.rules]
        if len(rule_paths) != len(set(rule_paths)):
            raise ValueError("repository rule paths must be unique")
        if rule_paths != sorted(rule_paths, key=lambda path: (path.count("/"), path)):
            raise ValueError("repository rules must be ordered from broad to specific")
        if len(self.incomplete_files) != len(set(self.incomplete_files)):
            raise ValueError("incomplete file paths must be unique")
        if list(self.incomplete_files) != sorted(self.incomplete_files):
            raise ValueError("incomplete file paths must be sorted")
        return self

    @property
    def complete(self) -> bool:
        return not self.incomplete_files


class ReviewUnit(PlanningContractModel):
    """一个文件的确定性输入；group_key 把需要联合审查的文件关联起来。"""

    unit_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    group_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    review_version_key: str = Field(min_length=44, max_length=400)
    head_sha: str = Field(min_length=40, max_length=64)
    file: str = Field(min_length=1, max_length=1024)
    blob_sha: str = Field(min_length=40, max_length=64)
    language: str = Field(min_length=1, max_length=50)
    patch: str = Field(min_length=1, max_length=MAX_PATCH_BYTES)
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rule_paths: tuple[str, ...] = Field(max_length=128)
    # 规划阶段确定的职责范围。旧版计划没有该列，读取时会回填全部三路，
    # 因而历史任务仍能完整重放；新计划使用它减少明显无关的重复输入。
    review_domains: tuple[ReviewAgent, ...] = Field(
        default=DEFAULT_REVIEW_DOMAINS,
        min_length=1,
        max_length=len(REVIEW_AGENTS),
        validation_alias=AliasChoices(
            "review_domains",
            "review_agents",
            "responsibilities",
        ),
    )
    estimated_input_bytes: int = Field(gt=0, le=10 * 1024 * 1024)
    planner_version: str = Field(min_length=1, max_length=50)
    # 这些字段只用于一次模型请求中的临时分片，不参与计划身份或数据库快照。
    # ``exclude=True`` 保证旧版持久化和计划指纹保持兼容。
    fragment_index: int = Field(default=0, ge=0, exclude=True)
    fragment_count: int = Field(default=1, ge=1, exclude=True)
    fragment_line_mode: Literal["global", "local"] = Field(
        default="global",
        exclude=True,
    )

    @field_validator("head_sha", "blob_sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        return normalize_sha(value)

    @field_validator("file")
    @classmethod
    def validate_file(cls, value: str) -> str:
        return normalize_repository_path(value)

    @field_validator("rule_paths")
    @classmethod
    def validate_rule_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(normalize_repository_path(value) for value in values)

    @field_validator("review_domains")
    @classmethod
    def validate_review_domains(
        cls,
        values: tuple[ReviewAgent, ...],
    ) -> tuple[ReviewAgent, ...]:
        if any(agent is ReviewAgent.SUMMARY for agent in values):
            raise ValueError("review units cannot target the summary Agent")
        if len(values) != len(set(values)):
            raise ValueError("review unit domains must be unique")
        expected_order = tuple(agent for agent in REVIEW_AGENTS if agent in values)
        if values != expected_order:
            raise ValueError("review unit domains must use the fixed Agent order")
        return values

    @model_validator(mode="after")
    def validate_input_identity(self) -> Self:
        if self.patch_sha256 != sha256(self.patch.encode("utf-8")).hexdigest():
            raise ValueError("review unit patch_sha256 must match patch")
        if len(self.rule_paths) != len(set(self.rule_paths)):
            raise ValueError("review unit rule paths must be unique")
        if list(self.rule_paths) != sorted(
            self.rule_paths,
            key=lambda path: (path.count("/"), path),
        ):
            raise ValueError("review unit rules must be ordered from broad to specific")
        if self.fragment_index >= self.fragment_count:
            raise ValueError("review unit fragment index must be below its count")
        if self.fragment_count == 1 and self.fragment_line_mode != "global":
            raise ValueError("unfragmented review units must use global line numbers")
        if self.planner_version == "review-planner-v3" and self.group_key is None:
            raise ValueError("review planner v3 units must include a related-file group")
        return self

    @property
    def review_agents(self) -> tuple[ReviewAgent, ...]:
        """兼容调用方使用的职责别名。"""

        return self.review_domains

    @property
    def responsibilities(self) -> tuple[ReviewAgent, ...]:
        """兼容旧客户端的职责别名。"""

        return self.review_domains


def ordered_review_units(
    units: Sequence[ReviewUnit], planner_version: str,
) -> tuple[ReviewUnit, ...]:
    """职责筛选和分片后重新规范组顺序，保留单元身份与内容。"""
    if planner_version != "review-planner-v3":
        return tuple(sorted(units, key=lambda unit: unit.file))
    first: dict[str | None, str] = {}
    for unit in units:
        first[unit.group_key] = min(first.get(unit.group_key, unit.file), unit.file)
    return tuple(sorted(units, key=lambda unit: (first[unit.group_key], unit.file)))


class ReviewFilePlan(PlanningContractModel):
    """changed file 的唯一规划结果；非 planned 文件不会伪造 unit_key。"""

    file: str = Field(min_length=1, max_length=1024)
    decision: ReviewFileDecision
    unit_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("file")
    @classmethod
    def validate_file(cls, value: str) -> str:
        return normalize_repository_path(value)

    @model_validator(mode="after")
    def planned_file_has_unit(self) -> Self:
        if self.decision is ReviewFileDecision.PLANNED and self.unit_key is None:
            raise ValueError("planned files must reference a review unit")
        if self.decision is not ReviewFileDecision.PLANNED and self.unit_key is not None:
            raise ValueError("files omitted from model review must not reference a unit")
        return self


class ReviewPlan(PlanningContractModel):
    """精确 PR 版本的一份可重放、资源有界的模型调用前计划。"""

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
    files: tuple[ReviewFilePlan, ...] = Field(max_length=3000)
    total_estimated_input_bytes: int = Field(ge=0, le=100 * 1024 * 1024)
    model_budget: ModelBudgetPolicy = Field(default_factory=ModelBudgetPolicy)

    @field_validator("head_sha")
    @classmethod
    def validate_head_sha(cls, value: str) -> str:
        return normalize_sha(value)

    @model_validator(mode="after")
    def validate_plan_shape(self) -> Self:
        expected_version_key = build_review_version_key(
            self.repository_id,
            self.pull_request_number,
            self.head_sha,
        )
        if self.review_version_key != expected_version_key:
            raise ValueError("review plan version key must match its repository and SHA")

        rule_paths = [rule.path for rule in self.rules]
        if len(rule_paths) != len(set(rule_paths)):
            raise ValueError("review plan rule paths must be unique")
        if rule_paths != sorted(rule_paths, key=lambda path: (path.count("/"), path)):
            raise ValueError("review plan rules must be ordered from broad to specific")

        file_paths = [item.file for item in self.files]
        if len(file_paths) != len(set(file_paths)):
            raise ValueError("review plan files must be unique")
        if file_paths != sorted(file_paths):
            raise ValueError("review plan files must be sorted")

        unit_paths = [unit.file for unit in self.units]
        unit_keys = [unit.unit_key for unit in self.units]
        if len(unit_paths) != len(set(unit_paths)) or len(unit_keys) != len(set(unit_keys)):
            raise ValueError("review plan units must have unique files and keys")
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
                raise ValueError("review plan units must keep related files adjacent")
        elif unit_paths != sorted(unit_paths):
            raise ValueError("review plan units must be sorted by file")

        planned = {
            item.file: item.unit_key
            for item in self.files
            if item.decision is ReviewFileDecision.PLANNED
        }
        actual = {unit.file: unit.unit_key for unit in self.units}
        if planned != actual:
            raise ValueError("planned file references must exactly match review units")

        known_rules = {rule.path for rule in self.rules}
        for unit in self.units:
            if (
                unit.review_version_key != self.review_version_key
                or unit.head_sha != self.head_sha
                or unit.planner_version != self.planner_version
            ):
                raise ValueError("review unit identity must match its plan")
            if not set(unit.rule_paths).issubset(known_rules):
                raise ValueError("review units may only reference rules in the plan")

        expected_total = sum(unit.estimated_input_bytes for unit in self.units)
        if self.planner_version in {"review-planner-v2", "review-planner-v3"}:
            expected_total += sum(rule.byte_size for rule in self.rules)
        if self.total_estimated_input_bytes != expected_total:
            raise ValueError("plan input byte total must equal its review units")
        return self
