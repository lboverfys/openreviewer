"""把 changed files、规则作用域和输入预算编译成确定性 Review Plan。"""

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import PurePosixPath
from typing import Protocol

from domain.enums import PatchState, ReviewFileDecision
from domain.github import PullRequestFile
from domain.identifiers import build_review_version_key
from domain.review_planning import (
    RepositoryRule,
    RepositoryRulesSnapshot,
    ReviewFilePlan,
    ReviewPlan,
    ReviewUnit,
    repository_rule_candidate_paths,
)
from services.task_queue import ReviewTarget


PLANNER_VERSION = "review-planner-v1"

_LANGUAGE_BY_SUFFIX = {
    ".bash": "shell",
    ".c": "c",
    ".cc": "cpp",
    ".cfg": "configuration",
    ".conf": "configuration",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".go": "go",
    ".gradle": "gradle",
    ".graphql": "graphql",
    ".gql": "graphql",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".ini": "configuration",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".less": "less",
    ".md": "markdown",
    ".php": "php",
    ".properties": "configuration",
    ".proto": "protobuf",
    ".ps1": "powershell",
    ".py": "python",
    ".pyi": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".scss": "scss",
    ".sh": "shell",
    ".sql": "sql",
    ".svelte": "svelte",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".vue": "vue",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}
_LANGUAGE_BY_NAME = {
    "dockerfile": "dockerfile",
    "jenkinsfile": "groovy",
    "makefile": "makefile",
}
_GENERATED_DIRECTORIES = {
    ".next",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "obj",
    "target",
    "vendor",
}
_GENERATED_NAMES = {
    "cargo.lock",
    "composer.lock",
    "gemfile.lock",
    "go.sum",
    "package-lock.json",
    "pipfile.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "yarn.lock",
}
_GENERATED_SUFFIXES = (
    ".map",
    ".min.css",
    ".min.js",
    ".designer.cs",
    "_pb2.py",
)


class ReviewPlanner(Protocol):
    def plan(
        self,
        target: ReviewTarget,
        files: Sequence[PullRequestFile],
        rules: RepositoryRulesSnapshot,
    ) -> ReviewPlan: ...


@dataclass(frozen=True, slots=True)
class ReviewPlanningSettings:
    """使用 UTF-8 字节近似模型输入，最终 Token 限制由模型适配器再执行。"""

    max_units: int = 100
    max_scope_depth: int = 32
    max_unit_input_bytes: int = 192 * 1024
    max_total_input_bytes: int = 2 * 1024 * 1024
    planner_version: str = PLANNER_VERSION

    def __post_init__(self) -> None:
        if not 1 <= self.max_units <= 3000:
            raise ValueError("review unit limit must be between 1 and 3000")
        if not 1 <= self.max_scope_depth <= 64:
            raise ValueError("review planning scope depth must be between 1 and 64")
        if not 4096 <= self.max_unit_input_bytes <= 10 * 1024 * 1024:
            raise ValueError("per-unit input limit must be between 4 KiB and 10 MiB")
        if not (
            self.max_unit_input_bytes
            <= self.max_total_input_bytes
            <= 100 * 1024 * 1024
        ):
            raise ValueError("total input limit must include one unit and stay below 100 MiB")
        if not self.planner_version or len(self.planner_version) > 50:
            raise ValueError("planner version must contain 1 to 50 characters")


class DeterministicReviewPlanner:
    """不访问网络或数据库，按稳定文件顺序构造一文件一 Unit 的计划。"""

    def __init__(self, settings: ReviewPlanningSettings | None = None) -> None:
        self._settings = settings or ReviewPlanningSettings()

    def plan(
        self,
        target: ReviewTarget,
        files: Sequence[PullRequestFile],
        rules: RepositoryRulesSnapshot,
    ) -> ReviewPlan:
        ordered_files = self._validate_inputs(target, files, rules)
        rules_by_path = {rule.path: rule for rule in rules.rules}
        incomplete_files = set(rules.incomplete_files)
        units: list[ReviewUnit] = []
        file_plans: list[ReviewFilePlan] = []
        total_input_bytes = 0

        for item in ordered_files:
            decision = self._non_reviewable_decision(item)
            if decision is not None:
                file_plans.append(ReviewFilePlan(file=item.path, decision=decision))
                continue
            if (
                item.path.count("/") > self._settings.max_scope_depth
                or item.path in incomplete_files
            ):
                file_plans.append(
                    ReviewFilePlan(
                        file=item.path,
                        decision=ReviewFileDecision.RULES_INCOMPLETE,
                    )
                )
                continue

            if item.patch is None:
                raise AssertionError("available patches must include text")
            applicable_rules = tuple(
                rules_by_path[path]
                for path in repository_rule_candidate_paths(
                    item.path,
                    max_parent_scopes=self._settings.max_scope_depth,
                )
                if path in rules_by_path
            )
            patch_bytes = len(item.patch.encode("utf-8"))
            estimated_input_bytes = patch_bytes + sum(
                rule.byte_size for rule in applicable_rules
            )
            exceeds_budget = (
                estimated_input_bytes > self._settings.max_unit_input_bytes
                or len(units) >= self._settings.max_units
                or total_input_bytes + estimated_input_bytes
                > self._settings.max_total_input_bytes
            )
            if exceeds_budget:
                file_plans.append(
                    ReviewFilePlan(
                        file=item.path,
                        decision=ReviewFileDecision.OMITTED_BY_BUDGET,
                    )
                )
                continue

            unit = self._build_unit(target, item, applicable_rules, estimated_input_bytes)
            units.append(unit)
            total_input_bytes += estimated_input_bytes
            file_plans.append(
                ReviewFilePlan(
                    file=item.path,
                    decision=ReviewFileDecision.PLANNED,
                    unit_key=unit.unit_key,
                )
            )

        fingerprint = self._plan_fingerprint(
            target,
            rules.rules,
            file_plans,
            units,
        )
        return ReviewPlan(
            plan_fingerprint=fingerprint,
            planner_version=self._settings.planner_version,
            review_version_key=target.review_version_key,
            repository_id=target.repository_id,
            repository=target.repository,
            pull_request_number=target.pull_request_number,
            head_sha=target.head_sha,
            rules=rules.rules,
            units=tuple(units),
            files=tuple(file_plans),
            total_estimated_input_bytes=total_input_bytes,
        )

    @staticmethod
    def _validate_inputs(
        target: ReviewTarget,
        files: Sequence[PullRequestFile],
        rules: RepositoryRulesSnapshot,
    ) -> tuple[PullRequestFile, ...]:
        if len(files) > 3000:
            raise ValueError("review planning accepts at most 3000 changed files")
        if target.review_version_key != build_review_version_key(
            target.repository_id,
            target.pull_request_number,
            target.head_sha,
        ):
            raise ValueError("review target version key does not match its identity")
        ordered = tuple(sorted(files, key=lambda item: item.path))
        paths = [item.path for item in ordered]
        if len(paths) != len(set(paths)):
            raise ValueError("review planning requires unique changed file paths")
        if (
            rules.repository_id != target.repository_id
            or rules.repository != target.repository
            or rules.head_sha != target.head_sha
        ):
            raise ValueError("repository rules must match the exact review target")
        if not set(rules.incomplete_files).issubset(paths):
            raise ValueError("incomplete rule files must belong to the changed file set")
        return ordered

    @staticmethod
    def _non_reviewable_decision(item: PullRequestFile) -> ReviewFileDecision | None:
        if item.patch_state is PatchState.BINARY:
            return ReviewFileDecision.BINARY
        if _is_generated(item.path):
            return ReviewFileDecision.GENERATED
        if _language_for(item.path) is None:
            return ReviewFileDecision.UNSUPPORTED
        if item.patch_state is PatchState.MISSING:
            return ReviewFileDecision.PATCH_MISSING
        if item.patch_state is PatchState.TOO_LARGE:
            return ReviewFileDecision.PATCH_TOO_LARGE
        return None

    def _build_unit(
        self,
        target: ReviewTarget,
        item: PullRequestFile,
        rules: tuple[RepositoryRule, ...],
        estimated_input_bytes: int,
    ) -> ReviewUnit:
        if item.patch is None:
            raise AssertionError("review units require patch text")
        patch_sha256 = sha256(item.patch.encode("utf-8")).hexdigest()
        identity = {
            "planner_version": self._settings.planner_version,
            "review_version_key": target.review_version_key,
            "head_sha": target.head_sha,
            "file": item.path,
            "blob_sha": item.blob_sha,
            "patch_sha256": patch_sha256,
            "rules": [
                {"path": rule.path, "content_sha256": rule.content_sha256}
                for rule in rules
            ],
        }
        unit_key = sha256(_canonical_json(identity)).hexdigest()
        language = _language_for(item.path)
        if language is None:
            raise AssertionError("review units require a supported language")
        return ReviewUnit(
            unit_key=unit_key,
            review_version_key=target.review_version_key,
            head_sha=target.head_sha,
            file=item.path,
            blob_sha=item.blob_sha,
            language=language,
            patch=item.patch,
            patch_sha256=patch_sha256,
            rule_paths=tuple(rule.path for rule in rules),
            estimated_input_bytes=estimated_input_bytes,
            planner_version=self._settings.planner_version,
        )

    def _plan_fingerprint(
        self,
        target: ReviewTarget,
        rules: tuple[RepositoryRule, ...],
        files: list[ReviewFilePlan],
        units: list[ReviewUnit],
    ) -> str:
        identity = {
            "planner_version": self._settings.planner_version,
            "review_version_key": target.review_version_key,
            "rules": [
                {"path": rule.path, "content_sha256": rule.content_sha256}
                for rule in rules
            ],
            "files": [
                {
                    "file": item.file,
                    "decision": item.decision.value,
                    "unit_key": item.unit_key,
                }
                for item in files
            ],
            "units": [unit.unit_key for unit in units],
        }
        return sha256(_canonical_json(identity)).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _is_generated(path: str) -> bool:
    normalized = path.casefold()
    parts = normalized.split("/")
    name = parts[-1]
    return (
        bool(set(parts[:-1]) & _GENERATED_DIRECTORIES)
        or name in _GENERATED_NAMES
        or any(name.endswith(suffix) for suffix in _GENERATED_SUFFIXES)
        or ".generated." in name
    )


def _language_for(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1].casefold()
    if name in _LANGUAGE_BY_NAME:
        return _LANGUAGE_BY_NAME[name]
    if name.startswith("dockerfile."):
        return "dockerfile"
    return _LANGUAGE_BY_SUFFIX.get(PurePosixPath(name).suffix)
