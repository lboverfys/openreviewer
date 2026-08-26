"""在精确 head SHA 上批量读取并限定 AGENTS.md 仓库规则。"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from domain.enums import RepositoryRuleIssueKind
from domain.github import PullRequestFile
from domain.review_planning import (
    RepositoryRule,
    RepositoryRuleIssue,
    RepositoryRulesSnapshot,
)
from domain.security import ErrorCode, SafeApplicationError, SafeError
from services.github import GitHubApiClient, GitHubResponseTooLargeError
from services.github_context import InstallationTokenProvider
from services.task_queue import ReviewTarget


class RepositoryRuleLoader(Protocol):
    def load(
        self,
        target: ReviewTarget,
        files: Sequence[PullRequestFile],
    ) -> RepositoryRulesSnapshot: ...


@dataclass(frozen=True, slots=True)
class GitHubRuleSettings:
    """限制单次 GraphQL 查询、规则文件和规则总内容的大小。"""

    max_candidate_paths: int = 128
    max_scope_depth: int = 32
    max_rule_bytes: int = 64 * 1024
    max_total_rule_bytes: int = 256 * 1024
    max_response_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        if not 1 <= self.max_candidate_paths <= 128:
            raise ValueError("AGENTS.md candidate limit must be between 1 and 128")
        if not 1 <= self.max_scope_depth <= 64:
            raise ValueError("AGENTS.md scope depth must be between 1 and 64")
        if not 1024 <= self.max_rule_bytes <= 256 * 1024:
            raise ValueError("AGENTS.md file limit must be between 1 KiB and 256 KiB")
        if not self.max_rule_bytes <= self.max_total_rule_bytes <= 4 * 1024 * 1024:
            raise ValueError("AGENTS.md total limit must include one file and stay below 4 MiB")
        if not 1024 <= self.max_response_bytes <= 10 * 1024 * 1024:
            raise ValueError("AGENTS.md GraphQL response limit must be 1 KiB to 10 MiB")


class GitHubRepositoryRuleLoader:
    """用一个 GraphQL 请求读取所有有界候选，避免逐文件 HTTP N+1。"""

    def __init__(
        self,
        api: GitHubApiClient,
        tokens: InstallationTokenProvider,
        settings: GitHubRuleSettings | None = None,
    ) -> None:
        self._api = api
        self._tokens = tokens
        self._settings = settings or GitHubRuleSettings()

    def load(
        self,
        target: ReviewTarget,
        files: Sequence[PullRequestFile],
    ) -> RepositoryRulesSnapshot:
        ordered_files = self._validate_files(files)
        if not ordered_files:
            return self._empty_snapshot(target)

        directory_parts = {
            item.path: tuple(item.path.split("/")[:-1]) for item in ordered_files
        }
        depth_limited_files = {
            path
            for path, parts in directory_parts.items()
            if len(parts) > self._settings.max_scope_depth
        }
        candidate_limited_files: set[str] = set()
        selected_items: list[str] = []
        candidate_count = 0
        for depth in range(self._settings.max_scope_depth + 1):
            candidates_at_depth: dict[str, set[str]] = defaultdict(set)
            for file_path, parts in directory_parts.items():
                if len(parts) < depth:
                    continue
                candidate = (
                    "AGENTS.md"
                    if depth == 0
                    else f"{'/'.join(parts[:depth])}/AGENTS.md"
                )
                candidates_at_depth[candidate].add(file_path)
            ordered_candidates = sorted(candidates_at_depth)
            candidate_count += len(ordered_candidates)
            remaining = self._settings.max_candidate_paths - len(selected_items)
            for candidate in ordered_candidates[: max(0, remaining)]:
                selected_items.append(candidate)
            for candidate in ordered_candidates[max(0, remaining) :]:
                candidate_limited_files.update(candidates_at_depth[candidate])

        selected = tuple(selected_items)
        incomplete_files = depth_limited_files | candidate_limited_files
        issues: list[RepositoryRuleIssue] = []
        if depth_limited_files:
            issues.append(
                RepositoryRuleIssue(
                    kind=RepositoryRuleIssueKind.SCOPE_DEPTH_LIMIT,
                    rule_path=None,
                    affected_file_count=len(depth_limited_files),
                )
            )
        if candidate_limited_files:
            issues.append(
                RepositoryRuleIssue(
                    kind=RepositoryRuleIssueKind.CANDIDATE_LIMIT,
                    rule_path=None,
                    affected_file_count=len(candidate_limited_files),
                )
            )

        try:
            payload = self._fetch_candidates(target, selected)
        except GitHubResponseTooLargeError:
            issues.append(
                RepositoryRuleIssue(
                    kind=RepositoryRuleIssueKind.RESPONSE_TOO_LARGE,
                    rule_path=None,
                    affected_file_count=len(ordered_files),
                )
            )
            return RepositoryRulesSnapshot(
                repository_id=target.repository_id,
                repository=target.repository,
                head_sha=target.head_sha,
                rules=(),
                incomplete_files=tuple(item.path for item in ordered_files),
                issues=tuple(issues),
                candidate_count=candidate_count,
                requested_candidate_count=len(selected),
            )
        rules: list[RepositoryRule] = []
        total_rule_bytes = 0
        for index, path in enumerate(selected):
            alias = f"rule{index}"
            if alias not in payload:
                raise self._invalid_response("GitHub GraphQL 规则查询缺少请求字段")
            raw = payload[alias]
            if raw is None:
                continue
            issue_kind, rule = self._parse_rule(path, raw)
            if issue_kind is None and rule is None:
                continue
            if issue_kind is None and rule is not None:
                if total_rule_bytes + rule.byte_size <= self._settings.max_total_rule_bytes:
                    rules.append(rule)
                    total_rule_bytes += rule.byte_size
                    continue
                issue_kind = RepositoryRuleIssueKind.TOTAL_LIMIT
            if issue_kind is None:
                raise AssertionError("rule parser must return a rule or an issue")
            affected = self._affected_files(path, ordered_files)
            incomplete_files.update(affected)
            issues.append(
                RepositoryRuleIssue(
                    kind=issue_kind,
                    rule_path=path,
                    affected_file_count=len(affected),
                )
            )

        return RepositoryRulesSnapshot(
            repository_id=target.repository_id,
            repository=target.repository,
            head_sha=target.head_sha,
            rules=tuple(rules),
            incomplete_files=tuple(sorted(incomplete_files)),
            issues=tuple(issues),
            candidate_count=candidate_count,
            requested_candidate_count=len(selected),
        )

    @staticmethod
    def _validate_files(files: Sequence[PullRequestFile]) -> tuple[PullRequestFile, ...]:
        if len(files) > 3000:
            raise ValueError("repository rule loading accepts at most 3000 changed files")
        ordered = tuple(sorted(files, key=lambda item: item.path))
        paths = [item.path for item in ordered]
        if len(paths) != len(set(paths)):
            raise ValueError("repository rule loading requires unique changed file paths")
        return ordered

    @staticmethod
    def _affected_files(
        rule_path: str,
        files: tuple[PullRequestFile, ...],
    ) -> set[str]:
        if rule_path == "AGENTS.md":
            return {item.path for item in files}
        scope_prefix = f"{rule_path.removesuffix('/AGENTS.md')}/"
        return {item.path for item in files if item.path.startswith(scope_prefix)}

    def _fetch_candidates(
        self,
        target: ReviewTarget,
        candidates: tuple[str, ...],
    ) -> dict[str, object]:
        owner, name = target.repository.split("/", 1)
        variables: dict[str, str] = {"owner": owner, "name": name}
        declarations = ["$owner: String!", "$name: String!"]
        selections: list[str] = []
        for index, path in enumerate(candidates):
            variable = f"expression{index}"
            declarations.append(f"${variable}: String!")
            variables[variable] = f"{target.head_sha}:{path}"
            selections.append(
                f"rule{index}: object(expression: ${variable}) {{ "
                "__typename ... on Blob { oid byteSize isBinary text } }"
            )
        query = (
            f"query RepositoryRules({', '.join(declarations)}) {{ "
            "repository(owner: $owner, name: $name) { "
            "databaseId nameWithOwner "
            f"{' '.join(selections)}"
            " } }"
        )
        response_limit = min(
            self._settings.max_response_bytes,
            self._api.settings.max_response_bytes,
        )
        result = self._api.request_json(
            "POST",
            "/graphql",
            bearer_token=self._tokens.get_token(target.installation_id),
            json_body={"query": query, "variables": variables},
            max_response_bytes=response_limit,
        ).payload
        if not isinstance(result, dict) or result.get("errors"):
            raise self._invalid_response("GitHub GraphQL 规则查询未完整成功")
        data = result.get("data")
        repository = data.get("repository") if isinstance(data, dict) else None
        if not isinstance(repository, dict):
            raise self._invalid_response("GitHub GraphQL 仓库响应格式无效")
        if (
            repository.get("databaseId") != target.repository_id
            or repository.get("nameWithOwner") != target.repository
        ):
            raise self._invalid_response("GitHub GraphQL 仓库身份与审查任务不一致")
        return repository

    def _parse_rule(
        self,
        path: str,
        raw: object,
    ) -> tuple[RepositoryRuleIssueKind | None, RepositoryRule | None]:
        if not isinstance(raw, dict) or raw.get("__typename") != "Blob":
            return RepositoryRuleIssueKind.CONTENT_UNAVAILABLE, None
        byte_size = raw.get("byteSize")
        is_binary = raw.get("isBinary")
        text = raw.get("text")
        blob_sha = raw.get("oid")
        if is_binary is True:
            return RepositoryRuleIssueKind.BINARY, None
        if is_binary is not False:
            return RepositoryRuleIssueKind.CONTENT_UNAVAILABLE, None
        if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size < 0:
            return RepositoryRuleIssueKind.CONTENT_UNAVAILABLE, None
        if byte_size > self._settings.max_rule_bytes:
            return RepositoryRuleIssueKind.TOO_LARGE, None
        if not isinstance(text, str) or not isinstance(blob_sha, str):
            return RepositoryRuleIssueKind.CONTENT_UNAVAILABLE, None
        try:
            encoded = text.encode("utf-8")
        except UnicodeError:
            return RepositoryRuleIssueKind.CONTENT_UNAVAILABLE, None
        if len(encoded) != byte_size:
            return RepositoryRuleIssueKind.CONTENT_UNAVAILABLE, None
        if not text:
            return None, None
        scope = path.rsplit("/", 1)[0] if "/" in path else None
        try:
            return None, RepositoryRule(
                path=path,
                scope=scope,
                blob_sha=blob_sha,
                content=text,
                content_sha256=sha256(encoded).hexdigest(),
                byte_size=byte_size,
            )
        except (UnicodeError, ValueError):
            return RepositoryRuleIssueKind.CONTENT_UNAVAILABLE, None

    @staticmethod
    def _empty_snapshot(target: ReviewTarget) -> RepositoryRulesSnapshot:
        return RepositoryRulesSnapshot(
            repository_id=target.repository_id,
            repository=target.repository,
            head_sha=target.head_sha,
            rules=(),
            incomplete_files=(),
            issues=(),
            candidate_count=0,
            requested_candidate_count=0,
        )

    @staticmethod
    def _invalid_response(message: str) -> SafeApplicationError:
        return SafeApplicationError(
            SafeError(
                code=ErrorCode.GITHUB_INVALID_RESPONSE,
                safe_message=message,
                retryable=False,
            )
        )
