"""把 changed files 和规则作用域编译成可自动分批的确定性 Review Plan。"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Protocol

from domain.enums import PatchState, ReviewFileDecision
from domain.github import PullRequestFile
from domain.identifiers import build_review_version_key
from domain.model_budget import ModelBudgetPolicy
from domain.review_planning import (
    RepositoryRule,
    RepositoryRulesSnapshot,
    ReviewFilePlan,
    ReviewPlan,
    ReviewUnit,
    repository_rule_candidate_paths,
)
from services.task_queue import ReviewTarget

PLANNER_VERSION = "review-planner-v3"

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
_RELATION_DIRECTORY_NAMES = {
    "controller",
    "controllers",
    "dto",
    "dtos",
    "entity",
    "entities",
    "java",
    "lib",
    "main",
    "model",
    "models",
    "repository",
    "repositories",
    "service",
    "services",
    "src",
    "test",
    "tests",
}
_RELATION_PREFIX_TOKENS = {"test", "tests"}
_RELATION_SUFFIX_TOKENS = {
    "controller",
    "dto",
    "entity",
    "handler",
    "mapper",
    "model",
    "repository",
    "request",
    "response",
    "service",
    "spec",
    "test",
    "tests",
    "usecase",
}


class ReviewPlanner(Protocol):
    def plan(
        self,
        target: ReviewTarget,
        files: Sequence[PullRequestFile],
        rules: RepositoryRulesSnapshot,
    ) -> ReviewPlan: ...


@dataclass(frozen=True, slots=True)
class ReviewPlanningSettings:
    """规划边界；旧输入预算字段仅为配置兼容，不再用于漏掉可审查文件。"""

    max_units: int = 100
    max_scope_depth: int = 32
    max_unit_input_bytes: int = 192 * 1024
    max_total_input_bytes: int = 2 * 1024 * 1024
    max_related_files: int = 8
    model_budget: ModelBudgetPolicy = ModelBudgetPolicy()
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
        if not 1 <= self.max_related_files <= 32:
            raise ValueError("related review file limit must be between 1 and 32")
        if not self.planner_version or len(self.planner_version) > 50:
            raise ValueError("planner version must contain 1 to 50 characters")


class DeterministicReviewPlanner:
    """不访问网络或数据库，构造确定性文件输入和关联审查组。"""

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
        total_patch_bytes = 0

        decisions: dict[str, ReviewFileDecision | None] = {}
        reviewable_paths: list[str] = []
        for item in ordered_files:
            decision = self._non_reviewable_decision(item)
            if decision is None and (
                item.path.count("/") > self._settings.max_scope_depth
                or item.path in incomplete_files
            ):
                decision = ReviewFileDecision.RULES_INCOMPLETE
            decisions[item.path] = decision
            if decision is None:
                reviewable_paths.append(item.path)
        group_keys = _related_group_keys(
            reviewable_paths,
            max_group_size=self._settings.max_related_files,
        )

        for item in ordered_files:
            decision = decisions[item.path]
            if decision is not None:
                file_plans.append(ReviewFilePlan(file=item.path, decision=decision))
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
            unit = self._build_unit(
                target,
                item,
                applicable_rules,
                patch_bytes,
                group_keys[item.path],
            )
            units.append(unit)
            total_patch_bytes += patch_bytes
            file_plans.append(
                ReviewFilePlan(
                    file=item.path,
                    decision=ReviewFileDecision.PLANNED,
                    unit_key=unit.unit_key,
                )
            )

        group_first_file: dict[str, str] = {}
        for path, group_key in group_keys.items():
            current = group_first_file.get(group_key)
            if current is None or path < current:
                group_first_file[group_key] = path
        units.sort(
            key=lambda unit: (
                group_first_file[unit.group_key or unit.unit_key],
                unit.file,
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
            total_estimated_input_bytes=(
                total_patch_bytes + sum(rule.byte_size for rule in rules.rules)
            ),
            model_budget=self._settings.model_budget,
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
        group_key: str,
    ) -> ReviewUnit:
        if item.patch is None:
            raise AssertionError("review units require patch text")
        patch_sha256 = sha256(item.patch.encode("utf-8")).hexdigest()
        identity = {
            "planner_version": self._settings.planner_version,
            "review_version_key": target.review_version_key,
            "head_sha": target.head_sha,
            "group_key": group_key,
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
            group_key=group_key,
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
            "model_budget": self._settings.model_budget.model_dump(mode="json"),
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


def _related_group_keys(
    paths: Sequence[str],
    *,
    max_group_size: int,
) -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    for path in paths:
        grouped.setdefault(_relationship_name(path), []).append(path)
    result: dict[str, str] = {}
    for relationship in sorted(grouped):
        members = sorted(grouped[relationship])
        for partition, offset in enumerate(range(0, len(members), max_group_size)):
            group_key = sha256(
                _canonical_json(
                    {
                        "version": 1,
                        "relationship": relationship,
                        "partition": partition,
                    }
                )
            ).hexdigest()
            for path in members[offset : offset + max_group_size]:
                result[path] = group_key
    return result


def _relationship_name(path: str) -> str:
    pure_path = PurePosixPath(path)
    stem = pure_path.stem
    stem = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", stem)
    name_tokens = [
        token
        for token in re.split(r"[^A-Za-z0-9]+", stem.casefold())
        if token
    ]
    while name_tokens and name_tokens[0] in _RELATION_PREFIX_TOKENS:
        name_tokens.pop(0)
    while name_tokens and name_tokens[-1] in _RELATION_SUFFIX_TOKENS:
        name_tokens.pop()
    directory_tokens = [
        part.casefold()
        for part in pure_path.parts[:-1]
        if part.casefold() not in _RELATION_DIRECTORY_NAMES
    ]
    identity_tokens = directory_tokens + name_tokens
    return "/".join(identity_tokens) if identity_tokens else path.casefold()
