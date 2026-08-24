"""批量读取并规范化 GitHub PR、完整 diff 与 CI 上下文。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import time
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


@dataclass(frozen=True, slots=True)
class GitHubContextSettings:
    """限制一次 PR 上下文读取的页数、条数与单文件补丁大小。"""

    page_size: int = 100
    max_files: int = 3000
    max_checks: int = 1000
    max_patch_bytes: int = 512 * 1024
    max_load_seconds: float = 8 * 60

    def __post_init__(self) -> None:
        if not 1 <= self.page_size <= 100:
            raise ValueError("GitHub page size must be between 1 and 100")
        if not 1 <= self.max_files <= 3000:
            raise ValueError("GitHub changed file limit must be between 1 and 3000")
        if not 1 <= self.max_checks <= 1000:
            raise ValueError("GitHub CI check limit must be between 1 and 1000")
        if not 1024 <= self.max_patch_bytes <= 512 * 1024:
            raise ValueError("GitHub per-file patch limit must be between 1 KiB and 512 KiB")
        if not 30 <= self.max_load_seconds <= 30 * 60:
            raise ValueError("GitHub context time budget must be between 30 and 1800 seconds")


@dataclass(frozen=True, slots=True)
class _DiffEntry:
    text: str | None
    binary: bool


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
            diff_entries, raw_diff_complete = self._fetch_diff(
                target,
                token,
                budget,
            )
            files, patches_complete = self._merge_files(raw_files, diff_entries)
            diff_complete = raw_diff_complete and patches_complete

        ci = self._fetch_ci(target, token, budget)
        return GitHubReviewContext(
            pull_request=pull_request,
            files=files,
            files_complete=files_complete,
            diff_complete=diff_complete,
            ci=ci,
        )

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
            snapshot = PullRequestSnapshot(
                repository_id=repository["id"],
                repository=repository["full_name"],
                pull_request_number=payload["number"],
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
                        previous_path=raw.get("previous_filename"),
                        status=ChangedFileStatus(raw["status"]),
                        blob_sha=raw["sha"],
                        additions=raw["additions"],
                        deletions=raw["deletions"],
                        changes=raw["changes"],
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
        while raw_count < limit:
            payload = self._request_json(
                f"/repos/{target.repository}/commits/{target.head_sha}/statuses",
                token,
                budget,
                params={"per_page": self._settings.page_size, "page": page},
            )
            if not isinstance(payload, list):
                raise self._invalid_response("GitHub Commit Status 响应格式无效")
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
            if len(payload) < self._settings.page_size or not complete:
                break
            page += 1
        if (
            raw_count >= limit
            and len(payload) == self._settings.page_size
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
            return CiState.UNKNOWN
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
