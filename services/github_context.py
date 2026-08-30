"""批量读取并规范化 GitHub PR、完整 diff 与 CI 上下文。"""

import difflib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import ValidationError
from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from domain.enums import (
    ChangedFileStatus,
    CiCheckKind,
    CiState,
    PatchState,
    PullRequestState,
)
from domain.github import (
    MAX_PATCH_BYTES,
    CiCheckSnapshot,
    CiSnapshot,
    GitHubReviewContext,
    PullRequestFile,
    PullRequestSnapshot,
)
from domain.security import ErrorCode, SafeApplicationError, SafeError
from services.github import GitHubApiClient, GitHubResponseTooLargeError
from services.task_queue import ReviewTarget


class InstallationTokenProvider(Protocol):
    @property
    def app_id(self) -> int: ...

    def get_token(self, installation_id: int) -> str: ...


class ReviewContextLoader(Protocol):
    def load(
        self,
        target: ReviewTarget,
    ) -> GitHubReviewContext: ...


def _required_str(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _optional_str(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_str(value, field_name)


def _required_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class GitHubContextSettings:
    """限制一次 PR 上下文读取的页数、条数与单文件补丁大小。"""

    page_size: int = 100
    max_files: int = 3000
    max_checks: int = 1000
    max_patch_bytes: int = MAX_PATCH_BYTES
    max_blob_fallback_files: int = 128
    max_blob_bytes: int = 4 * 1024 * 1024
    max_blob_fallback_total_bytes: int = 6 * 1024 * 1024
    max_load_seconds: float = 8 * 60

    def __post_init__(self) -> None:
        if not 1 <= self.page_size <= 100:
            raise ValueError("GitHub page size must be between 1 and 100")
        if not 1 <= self.max_files <= 3000:
            raise ValueError("GitHub changed file limit must be between 1 and 3000")
        if not 1 <= self.max_checks <= 1000:
            raise ValueError("GitHub CI check limit must be between 1 and 1000")
        if not 1024 <= self.max_patch_bytes <= MAX_PATCH_BYTES:
            raise ValueError(
                "GitHub per-file patch limit must be between 1 KiB and 8 MiB"
            )
        if not 1 <= self.max_blob_fallback_files <= 128:
            raise ValueError("GitHub Blob fallback file limit must be between 1 and 128")
        if not 1024 <= self.max_blob_bytes <= MAX_PATCH_BYTES:
            raise ValueError("GitHub Blob limit must be between 1 KiB and 8 MiB")
        if not (
            self.max_blob_bytes
            <= self.max_blob_fallback_total_bytes
            <= MAX_PATCH_BYTES
        ):
            raise ValueError(
                "GitHub Blob fallback total limit must include one Blob and stay below 8 MiB"
            )
        if not 30 <= self.max_load_seconds <= 30 * 60:
            raise ValueError("GitHub context time budget must be between 30 and 1800 seconds")


@dataclass(frozen=True, slots=True)
class _DiffEntry:
    text: str | None
    binary: bool
    too_large: bool = False


@dataclass(frozen=True, slots=True)
class _BlobSpec:
    alias: str
    expression: str


@dataclass(frozen=True, slots=True)
class _BlobMetadata:
    oid: str
    byte_size: int
    is_binary: bool
    text: str | None = None


@dataclass(frozen=True, slots=True)
class _BlobDiffCandidate:
    path: str
    base_path: str
    status: ChangedFileStatus
    base_alias: str | None
    head_alias: str | None


@dataclass(frozen=True, slots=True)
class _RequestBudget:
    """限制一轮分页读取总时长，且不依赖数据库续租。"""

    deadline: float
    monotonic: Callable[[], float]

    def ensure_available(self) -> None:
        if self.monotonic() >= self.deadline:
            raise SafeApplicationError(
                SafeError(
                    code=ErrorCode.GITHUB_TIMEOUT,
                    safe_message="GitHub PR 上下文读取超过总时间预算",
                    retryable=True,
                )
            )


class GitHubReviewContextLoader:
    """用短期 installation token 构造一份有界、可重放的 GitHub 快照。"""

    def __init__(
        self,
        api: GitHubApiClient,
        tokens: InstallationTokenProvider,
        settings: GitHubContextSettings | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._api = api
        self._tokens = tokens
        self._settings = settings or GitHubContextSettings()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic

    def load(
        self,
        target: ReviewTarget,
    ) -> GitHubReviewContext:
        budget = _RequestBudget(
            deadline=self._monotonic() + self._settings.max_load_seconds,
            monotonic=self._monotonic,
        )
        budget.ensure_available()
        token = self._tokens.get_token(target.installation_id)
        pull_request = self._fetch_pull_request(target, token, budget)
        is_current_open_pr = (
            pull_request.head_sha == target.head_sha
            and pull_request.state is PullRequestState.OPEN
            and not pull_request.draft
        )
        if not is_current_open_pr:
            return GitHubReviewContext(
                pull_request=pull_request,
                files=None,
                files_complete=False,
                diff_complete=False,
                ci=None,
            )

        files: tuple[PullRequestFile, ...] | None = None
        files_complete = False
        diff_complete = False
        if target.context_fetched_at is None:
            raw_files, files_complete = self._fetch_changed_files(
                target,
                pull_request.changed_files,
                token,
                budget,
            )
            diff_entries, _raw_diff_complete = self._fetch_diff(
                target,
                token,
                budget,
            )
            fallback_entries = self._fetch_blob_fallbacks(
                target,
                pull_request,
                raw_files,
                diff_entries,
                token,
                budget,
            )
            diff_entries.update(fallback_entries)
            files, patches_complete = self._merge_files(raw_files, diff_entries)
            diff_complete = files_complete and patches_complete

        ci = self._fetch_ci(target, token, budget)
        return GitHubReviewContext(
            pull_request=pull_request,
            files=files,
            files_complete=files_complete,
            diff_complete=diff_complete,
            ci=ci,
        )

    def load_pull_request(self, target: ReviewTarget) -> PullRequestSnapshot:
        """只读取单条 PR 元数据，供历史任务补全身份信息使用。"""

        budget = _RequestBudget(
            deadline=self._monotonic() + self._settings.max_load_seconds,
            monotonic=self._monotonic,
        )
        budget.ensure_available()
        token = self._tokens.get_token(target.installation_id)
        return self._fetch_pull_request(target, token, budget)

    def _fetch_pull_request(
        self,
        target: ReviewTarget,
        token: str,
        budget: _RequestBudget,
    ) -> PullRequestSnapshot:
        payload = self._request_json(
            f"/repos/{target.repository}/pulls/{target.pull_request_number}",
            token,
            budget,
        )
        try:
            if not isinstance(payload, dict):
                raise TypeError
            base = payload["base"]
            head = payload["head"]
            repository = base["repo"]
            author = payload.get("user")
            head_repository = head.get("repo")
            snapshot = PullRequestSnapshot(
                repository_id=repository["id"],
                repository=repository["full_name"],
                pull_request_number=payload["number"],
                author_login=(
                    author["login"] if isinstance(author, dict) else None
                ),
                html_url=payload["html_url"],
                head_repository=(
                    head_repository["full_name"]
                    if isinstance(head_repository, dict)
                    else None
                ),
                head_ref=head["ref"],
                base_repository=repository["full_name"],
                base_ref=base["ref"],
                base_sha=base["sha"],
                head_sha=head["sha"],
                state=payload["state"],
                draft=payload["draft"],
                title=payload["title"],
                changed_files=payload["changed_files"],
                updated_at=payload["updated_at"],
            )
        except (KeyError, TypeError, ValidationError) as exc:
            raise self._invalid_response("GitHub PR 元数据格式无效") from exc
        if (
            snapshot.repository_id != target.repository_id
            or snapshot.repository != target.repository
            or snapshot.pull_request_number != target.pull_request_number
        ):
            raise self._invalid_response("GitHub PR 身份与任务不一致")
        return snapshot

    def _fetch_changed_files(
        self,
        target: ReviewTarget,
        expected_count: int,
        token: str,
        budget: _RequestBudget,
    ) -> tuple[list[dict[str, object]], bool]:
        items: list[dict[str, object]] = []
        page = 1
        while len(items) < self._settings.max_files:
            payload = self._request_json(
                f"/repos/{target.repository}/pulls/{target.pull_request_number}/files",
                token,
                budget,
                params={"per_page": self._settings.page_size, "page": page},
            )
            if not isinstance(payload, list):
                raise self._invalid_response("GitHub changed files 响应格式无效")
            if any(not isinstance(item, dict) for item in payload):
                raise self._invalid_response("GitHub changed files 条目格式无效")
            remaining = self._settings.max_files - len(items)
            items.extend(payload[:remaining])
            if len(payload) < self._settings.page_size:
                break
            page += 1
        complete = len(items) == expected_count and expected_count <= self._settings.max_files
        return items, complete

    def _fetch_diff(
        self,
        target: ReviewTarget,
        token: str,
        budget: _RequestBudget,
    ) -> tuple[dict[str, _DiffEntry], bool]:
        budget.ensure_available()
        try:
            diff_text, _audit = self._api.request_text(
                "GET",
                f"/repos/{target.repository}/pulls/{target.pull_request_number}",
                bearer_token=token,
                accept="application/vnd.github.v3.diff",
            )
        except GitHubResponseTooLargeError:
            return {}, False
        except SafeApplicationError as exc:
            if (
                exc.error.code is ErrorCode.GITHUB_REQUEST_REJECTED
                and exc.error.details.get("status_code") in {406, 422}
            ):
                return {}, False
            raise
        try:
            patch_set = PatchSet(diff_text.splitlines(keepends=True))
        except (UnidiffParseError, UnicodeError, ValueError):
            return {}, False

        entries: dict[str, _DiffEntry] = {}
        try:
            for patched_file in patch_set:
                path = patched_file.path
                if not isinstance(path, str) or not path:
                    return {}, False
                if path in entries:
                    return {}, False
                entries[path] = _DiffEntry(
                    text=None if patched_file.is_binary_file else str(patched_file),
                    binary=bool(patched_file.is_binary_file),
                )
        except (AttributeError, TypeError, ValueError):
            return {}, False
        return entries, True

    def _fetch_blob_fallbacks(
        self,
        target: ReviewTarget,
        pull_request: PullRequestSnapshot,
        raw_files: list[dict[str, object]],
        diff_entries: dict[str, _DiffEntry],
        token: str,
        budget: _RequestBudget,
    ) -> dict[str, _DiffEntry]:
        """用固定最多两次 GraphQL 请求重建 REST 未提供的文本补丁。"""

        candidates, specs = self._build_blob_candidates(
            pull_request,
            raw_files,
            diff_entries,
        )
        if not specs:
            return {}
        try:
            metadata_payload = self._fetch_blob_objects(
                target,
                specs,
                token,
                budget,
                include_text=False,
            )
        except GitHubResponseTooLargeError:
            return {}

        metadata = {
            spec.alias: self._parse_blob(metadata_payload[spec.alias], require_text=False)
            for spec in specs
        }
        spec_by_alias = {spec.alias: spec for spec in specs}
        entries: dict[str, _DiffEntry] = {}
        content_candidates: list[_BlobDiffCandidate] = []
        content_aliases: set[str] = set()
        reserved_bytes = 0
        for candidate in candidates:
            aliases = tuple(
                alias
                for alias in (candidate.base_alias, candidate.head_alias)
                if alias is not None
            )
            blobs = tuple(metadata[alias] for alias in aliases)
            if any(blob is None for blob in blobs):
                continue
            available_blobs = tuple(blob for blob in blobs if blob is not None)
            if any(blob.is_binary for blob in available_blobs):
                entries[candidate.path] = _DiffEntry(text=None, binary=True)
                continue
            candidate_bytes = sum(blob.byte_size for blob in available_blobs)
            if (
                any(
                    blob.byte_size > self._settings.max_blob_bytes
                    for blob in available_blobs
                )
                or reserved_bytes + candidate_bytes
                > self._settings.max_blob_fallback_total_bytes
            ):
                entries[candidate.path] = _DiffEntry(
                    text=None,
                    binary=False,
                    too_large=True,
                )
                continue
            reserved_bytes += candidate_bytes
            content_candidates.append(candidate)
            content_aliases.update(aliases)

        if not content_aliases:
            return entries
        content_specs = tuple(
            spec_by_alias[alias]
            for alias in spec_by_alias
            if alias in content_aliases
        )
        try:
            content_payload = self._fetch_blob_objects(
                target,
                content_specs,
                token,
                budget,
                include_text=True,
            )
        except GitHubResponseTooLargeError:
            return entries
        contents = {
            spec.alias: self._parse_blob(content_payload[spec.alias], require_text=True)
            for spec in content_specs
        }
        for candidate in content_candidates:
            base_blob = (
                contents.get(candidate.base_alias)
                if candidate.base_alias is not None
                else None
            )
            head_blob = (
                contents.get(candidate.head_alias)
                if candidate.head_alias is not None
                else None
            )
            if (
                candidate.base_alias is not None
                and not self._same_blob(base_blob, metadata[candidate.base_alias])
            ) or (
                candidate.head_alias is not None
                and not self._same_blob(head_blob, metadata[candidate.head_alias])
            ):
                continue
            base_text = "" if base_blob is None else base_blob.text
            head_text = "" if head_blob is None else head_blob.text
            if base_text is None or head_text is None:
                continue
            entries[candidate.path] = _DiffEntry(
                text=self._build_unified_diff(candidate, base_text, head_text),
                binary=False,
            )
        return entries

    def _build_blob_candidates(
        self,
        pull_request: PullRequestSnapshot,
        raw_files: list[dict[str, object]],
        diff_entries: dict[str, _DiffEntry],
    ) -> tuple[tuple[_BlobDiffCandidate, ...], tuple[_BlobSpec, ...]]:
        candidates: list[_BlobDiffCandidate] = []
        specs: list[_BlobSpec] = []
        for raw in raw_files:
            if len(candidates) >= self._settings.max_blob_fallback_files:
                break
            path = raw.get("filename")
            if not isinstance(path, str) or not self._needs_blob_fallback(
                path,
                raw.get("patch"),
                diff_entries,
            ):
                continue
            try:
                status_value = raw.get("status")
                if not isinstance(status_value, str):
                    continue
                status = ChangedFileStatus(status_value)
            except (TypeError, ValueError):
                continue
            previous_path = raw.get("previous_filename")
            if status is ChangedFileStatus.RENAMED:
                if not isinstance(previous_path, str):
                    continue
                base_path = previous_path
            else:
                base_path = path
            index = len(candidates)
            base_alias = None
            head_alias = None
            if status is not ChangedFileStatus.ADDED:
                base_alias = f"base{index}"
                specs.append(
                    _BlobSpec(
                        alias=base_alias,
                        expression=f"{pull_request.base_sha}:{base_path}",
                    )
                )
            if status is not ChangedFileStatus.REMOVED:
                head_alias = f"head{index}"
                specs.append(
                    _BlobSpec(
                        alias=head_alias,
                        expression=f"{pull_request.head_sha}:{path}",
                    )
                )
            candidates.append(
                _BlobDiffCandidate(
                    path=path,
                    base_path=base_path,
                    status=status,
                    base_alias=base_alias,
                    head_alias=head_alias,
                )
            )
        return tuple(candidates), tuple(specs)

    def _needs_blob_fallback(
        self,
        path: str,
        rest_patch: object,
        diff_entries: dict[str, _DiffEntry],
    ) -> bool:
        entry = diff_entries.get(path)
        if entry is not None and entry.binary:
            return False
        candidates = (
            entry.text if entry is not None else None,
            rest_patch,
        )
        return not any(
            isinstance(candidate, str)
            and bool(candidate)
            and len(candidate.encode("utf-8")) <= self._settings.max_patch_bytes
            for candidate in candidates
        )

    def _fetch_blob_objects(
        self,
        target: ReviewTarget,
        specs: tuple[_BlobSpec, ...],
        token: str,
        budget: _RequestBudget,
        *,
        include_text: bool,
    ) -> dict[str, object]:
        owner, name = target.repository.split("/", 1)
        variables: dict[str, str] = {"owner": owner, "name": name}
        declarations = ["$owner: String!", "$name: String!"]
        selections: list[str] = []
        fields = "oid byteSize isBinary text" if include_text else "oid byteSize isBinary"
        for index, spec in enumerate(specs):
            variable = f"expression{index}"
            declarations.append(f"${variable}: String!")
            variables[variable] = spec.expression
            selections.append(
                f"{spec.alias}: object(expression: ${variable}) {{ "
                f"__typename ... on Blob {{ {fields} }} }}"
            )
        query = (
            f"query PullRequestBlobs({', '.join(declarations)}) {{ "
            "repository(owner: $owner, name: $name) { "
            "databaseId nameWithOwner "
            f"{' '.join(selections)}"
            " } }"
        )
        budget.ensure_available()
        payload = self._api.request_json(
            "POST",
            "/graphql",
            bearer_token=token,
            json_body={"query": query, "variables": variables},
        ).payload
        if not isinstance(payload, dict) or payload.get("errors"):
            raise self._invalid_response("GitHub GraphQL Blob 查询未完整成功")
        data = payload.get("data")
        repository = data.get("repository") if isinstance(data, dict) else None
        if not isinstance(repository, dict):
            raise self._invalid_response("GitHub GraphQL Blob 仓库响应格式无效")
        if (
            repository.get("databaseId") != target.repository_id
            or repository.get("nameWithOwner") != target.repository
        ):
            raise self._invalid_response("GitHub GraphQL Blob 仓库身份与审查任务不一致")
        if any(spec.alias not in repository for spec in specs):
            raise self._invalid_response("GitHub GraphQL Blob 查询缺少请求字段")
        return repository

    @staticmethod
    def _parse_blob(raw: object, *, require_text: bool) -> _BlobMetadata | None:
        if not isinstance(raw, dict) or raw.get("__typename") != "Blob":
            return None
        oid = raw.get("oid")
        byte_size = raw.get("byteSize")
        is_binary = raw.get("isBinary")
        text = raw.get("text") if require_text else None
        if (
            not isinstance(oid, str)
            or len(oid) not in range(40, 65)
            or any(character not in "0123456789abcdefABCDEF" for character in oid)
            or not isinstance(byte_size, int)
            or isinstance(byte_size, bool)
            or byte_size < 0
            or not isinstance(is_binary, bool)
        ):
            return None
        if require_text:
            if not isinstance(text, str) or len(text.encode("utf-8")) != byte_size:
                return None
        return _BlobMetadata(
            oid=oid.lower(),
            byte_size=byte_size,
            is_binary=is_binary,
            text=text,
        )

    @staticmethod
    def _same_blob(
        content: _BlobMetadata | None,
        metadata: _BlobMetadata | None,
    ) -> bool:
        return (
            content is not None
            and metadata is not None
            and content.oid == metadata.oid
            and content.byte_size == metadata.byte_size
            and content.is_binary == metadata.is_binary
            and not content.is_binary
        )

    @staticmethod
    def _build_unified_diff(
        candidate: _BlobDiffCandidate,
        base_text: str,
        head_text: str,
    ) -> str:
        from_path = (
            "/dev/null"
            if candidate.status is ChangedFileStatus.ADDED
            else f"a/{candidate.base_path}"
        )
        to_path = (
            "/dev/null"
            if candidate.status is ChangedFileStatus.REMOVED
            else f"b/{candidate.path}"
        )
        prefix = f"diff --git a/{candidate.base_path} b/{candidate.path}\n"
        if candidate.status is ChangedFileStatus.RENAMED:
            prefix += f"rename from {candidate.base_path}\nrename to {candidate.path}\n"
        body = difflib.unified_diff(
            [f"{line}\n" for line in base_text.splitlines()],
            [f"{line}\n" for line in head_text.splitlines()],
            fromfile=from_path,
            tofile=to_path,
            lineterm="\n",
        )
        return prefix + "".join(body)

    def _merge_files(
        self,
        raw_files: list[dict[str, object]],
        diff_entries: dict[str, _DiffEntry],
    ) -> tuple[tuple[PullRequestFile, ...], bool]:
        files: list[PullRequestFile] = []
        seen_paths: set[str] = set()
        patches_complete = True
        for raw in raw_files:
            try:
                path = raw["filename"]
                if not isinstance(path, str) or path in seen_paths:
                    raise TypeError
                seen_paths.add(path)
                diff_entry = diff_entries.get(path)
                fallback_patch = raw.get("patch")
                patch_text: str | None
                patch_state: PatchState
                if diff_entry is not None and diff_entry.binary:
                    patch_text = None
                    patch_state = PatchState.BINARY
                elif diff_entry is not None and diff_entry.too_large:
                    patch_text = None
                    patch_state = PatchState.TOO_LARGE
                    patches_complete = False
                else:
                    candidate = (
                        diff_entry.text
                        if diff_entry is not None
                        else fallback_patch
                    )
                    if not isinstance(candidate, str) or not candidate:
                        patch_text = None
                        patch_state = PatchState.MISSING
                        patches_complete = False
                    elif len(candidate.encode("utf-8")) > self._settings.max_patch_bytes:
                        patch_text = None
                        patch_state = PatchState.TOO_LARGE
                        patches_complete = False
                    else:
                        patch_text = candidate
                        patch_state = PatchState.AVAILABLE
                files.append(
                    PullRequestFile(
                        path=path,
                        previous_path=_optional_str(
                            raw.get("previous_filename"),
                            "previous_filename",
                        ),
                        status=ChangedFileStatus(
                            _required_str(raw["status"], "status")
                        ),
                        blob_sha=_required_str(raw["sha"], "sha"),
                        additions=_required_int(raw["additions"], "additions"),
                        deletions=_required_int(raw["deletions"], "deletions"),
                        changes=_required_int(raw["changes"], "changes"),
                        patch_state=patch_state,
                        patch=patch_text,
                    )
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                raise self._invalid_response("GitHub changed file 格式无效") from exc
        return tuple(files), patches_complete

    def _fetch_ci(
        self,
        target: ReviewTarget,
        token: str,
        budget: _RequestBudget,
    ) -> CiSnapshot:
        check_runs, checks_complete = self._fetch_check_runs(
            target,
            token,
            budget,
        )
        remaining_checks = self._settings.max_checks - len(check_runs)
        statuses, statuses_complete = self._fetch_commit_statuses(
            target,
            token,
            budget,
            limit=remaining_checks,
        )
        checks = tuple(check_runs + statuses)
        complete = checks_complete and statuses_complete
        return CiSnapshot(
            head_sha=target.head_sha,
            state=self._aggregate_ci(checks, complete),
            checks=checks,
            complete=complete,
            checked_at=self._now(),
        )

    def _fetch_check_runs(
        self,
        target: ReviewTarget,
        token: str,
        budget: _RequestBudget,
    ) -> tuple[list[CiCheckSnapshot], bool]:
        checks: list[CiCheckSnapshot] = []
        page = 1
        total_count: int | None = None
        raw_count = 0
        while raw_count < self._settings.max_checks:
            payload = self._request_json(
                f"/repos/{target.repository}/commits/{target.head_sha}/check-runs",
                token,
                budget,
                params={
                    "filter": "latest",
                    "per_page": self._settings.page_size,
                    "page": page,
                },
            )
            try:
                if not isinstance(payload, dict):
                    raise TypeError
                raw_total = payload["total_count"]
                raw_checks = payload["check_runs"]
                if not isinstance(raw_total, int) or raw_total < 0:
                    raise TypeError
                if not isinstance(raw_checks, list):
                    raise TypeError
                total_count = raw_total
                for raw in raw_checks:
                    raw_count += 1
                    if raw_count > self._settings.max_checks:
                        break
                    if not isinstance(raw, dict):
                        raise TypeError
                    app = raw.get("app")
                    app_id = app.get("id") if isinstance(app, dict) else None
                    if app_id == self._tokens.app_id:
                        continue
                    status = raw["status"]
                    conclusion = raw.get("conclusion")
                    self._validate_check_run_state(status, conclusion)
                    checks.append(
                        CiCheckSnapshot(
                            kind=CiCheckKind.CHECK_RUN,
                            external_key=str(raw["id"]),
                            name=raw["name"],
                            status=status,
                            conclusion=conclusion,
                            app_id=app_id,
                        )
                    )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                raise self._invalid_response("GitHub Check Run 格式无效") from exc
            if (
                len(raw_checks) < self._settings.page_size
                or raw_count >= self._settings.max_checks
            ):
                break
            page += 1
        complete = (
            total_count is not None
            and total_count <= self._settings.max_checks
            and raw_count == total_count
        )
        return checks, complete

    def _fetch_commit_statuses(
        self,
        target: ReviewTarget,
        token: str,
        budget: _RequestBudget,
        *,
        limit: int,
    ) -> tuple[list[CiCheckSnapshot], bool]:
        if limit <= 0:
            return [], False
        latest_by_context: dict[str, CiCheckSnapshot] = {}
        raw_count = 0
        page = 1
        complete = True
        last_page_size = 0
        while raw_count < limit:
            payload = self._request_json(
                f"/repos/{target.repository}/commits/{target.head_sha}/statuses",
                token,
                budget,
                params={"per_page": self._settings.page_size, "page": page},
            )
            if not isinstance(payload, list):
                raise self._invalid_response("GitHub Commit Status 响应格式无效")
            last_page_size = len(payload)
            for raw in payload:
                raw_count += 1
                if raw_count > limit:
                    complete = False
                    break
                try:
                    if not isinstance(raw, dict):
                        raise TypeError
                    context = raw["context"]
                    state = raw["state"]
                    self._validate_commit_status(state)
                    if context not in latest_by_context:
                        latest_by_context[context] = CiCheckSnapshot(
                            kind=CiCheckKind.COMMIT_STATUS,
                            external_key=context,
                            name=context,
                            status=state,
                            conclusion=None if state == "pending" else state,
                            app_id=None,
                        )
                except (KeyError, TypeError, ValueError, ValidationError) as exc:
                    raise self._invalid_response("GitHub Commit Status 格式无效") from exc
            if last_page_size < self._settings.page_size or not complete:
                break
            page += 1
        if (
            raw_count >= limit
            and last_page_size == self._settings.page_size
        ):
            complete = False
        return list(latest_by_context.values()), complete

    def _request_json(
        self,
        path: str,
        token: str,
        budget: _RequestBudget,
        *,
        params: dict[str, str | int] | None = None,
    ) -> object:
        budget.ensure_available()
        return self._api.request_json(
            "GET",
            path,
            bearer_token=token,
            params=params,
        ).payload

    @staticmethod
    def _validate_check_run_state(status: object, conclusion: object) -> None:
        allowed_statuses = {
            "queued",
            "in_progress",
            "completed",
            "waiting",
            "requested",
            "pending",
        }
        allowed_conclusions = {
            None,
            "action_required",
            "cancelled",
            "failure",
            "neutral",
            "skipped",
            "stale",
            "startup_failure",
            "success",
            "timed_out",
        }
        if status not in allowed_statuses or conclusion not in allowed_conclusions:
            raise ValueError("unsupported Check Run state")
        if status == "completed" and conclusion is None:
            raise ValueError("completed Check Run must have a conclusion")

    @staticmethod
    def _validate_commit_status(state: object) -> None:
        if state not in {"error", "failure", "pending", "success"}:
            raise ValueError("unsupported Commit Status state")

    @staticmethod
    def _aggregate_ci(
        checks: tuple[CiCheckSnapshot, ...],
        complete: bool,
    ) -> CiState:
        if not complete:
            return CiState.UNKNOWN
        if not checks:
            # 空集合与“无法证明完整”是两种不同状态：前者表示仓库没有
            # 配置 CI 门禁，后者仍需等待分页读取完成，避免无检查仓库白等一小时。
            return CiState.NOT_CONFIGURED
        failing = {
            "action_required",
            "cancelled",
            "error",
            "failure",
            "stale",
            "startup_failure",
            "timed_out",
        }
        if any(
            check.status in failing or check.conclusion in failing
            for check in checks
        ):
            return CiState.FAILURE
        if any(
            check.status in {"queued", "in_progress", "waiting", "pending"}
            or check.conclusion is None
            for check in checks
        ):
            return CiState.PENDING
        return CiState.SUCCESS

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _invalid_response(message: str) -> SafeApplicationError:
        return SafeApplicationError(
            SafeError(
                code=ErrorCode.GITHUB_INVALID_RESPONSE,
                safe_message=message,
                retryable=False,
            )
        )
