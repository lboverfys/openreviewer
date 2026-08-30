"""批准后把审查结果可靠发布到 GitHub。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from hashlib import sha256
from typing import Final, Protocol
from uuid import uuid4

from domain.security import ErrorCode, SafeApplicationError, SafeError, redact_text
from services.external_actions import ExternalActionStore, remote_id_from_result
from services.github import GitHubApiClient
from services.review_management import StoredFinding, StoredReviewDetails


class InstallationTokenProvider(Protocol):
    """提供短期 GitHub App installation token。"""

    def get_token(self, installation_id: int) -> str: ...


_PAGE_SIZE: Final[int] = 100
# 评论与 Check 对账最多读取 10 页（每页 100 条），与发布契约保持一致。
_MAX_PAGES: Final[int] = 10
_MAX_INLINE_COMMENTS: Final[int] = 50
# GitHubApiClient 的 JSON 上限是 1 MiB；给请求头和服务端解析留出余量，
# 过大的行内 Review 会在本地拆成多个有界请求。
_MAX_INLINE_REVIEW_REQUEST_BYTES: Final[int] = 900 * 1024
_MAX_INLINE_REVIEW_REQUESTS: Final[int] = 10
_MAX_COMMENT_BODY_BYTES: Final[int] = 60 * 1024
_MAX_FIELD_CHARS: Final[int] = 2_000
_EXTERNAL_ACTION_LEASE_SECONDS: Final[int] = 300
_CHECK_NAME: Final[str] = "OpenReviewer"
_MARKER_RE = re.compile(r"^<!-- openreviewer-review:[0-9a-f]{24} -->$")
_INLINE_MARKER_RE = re.compile(r"^<!-- openreviewer-inline:[0-9a-f]{24} -->$")
# 模型字段会被嵌入平台生成的 Markdown；先移除可执行链接/提及，再转义
# Markdown 和 HTML 控制字符。幂等标记只允许由本模块生成，不能来自模型文本。
_UNTRUSTED_URL_RE = re.compile(
    r"(?ix)(?<![\w])(?:https?|ftp)://[^\s<>()]+"
    r"|(?<![\w])mailto:[^\s<>()]+"
    r"|(?<![\w])www\.[^\s<>()]+"
)
_UNTRUSTED_MENTION_RE = re.compile(r"(?<![\w])@[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_MARKDOWN_CONTROL_RE = re.compile(r"([\\*_{}\[\]()#+\-.!|>~])")
_SAFE_REDACTION_TOKEN_RE = re.compile(r"<(?:redacted|truncated)>")


class _InlineReviewTooLarge(ValueError):
    """聚合行内 Review 超过安全边界，应降级到 Check/汇总。"""


class GitHubReviewPublisher:
    """发布幂等 Check、批量行内评论和 PR 汇总评论。

    调用方在进入本类前已经提交数据库发布状态，所以这里的全部 GitHub HTTP
    请求都发生在数据库事务外。每种远端对象都带稳定身份，外部成功而本地回写
    失败时，重试会先对账再补缺失动作。
    """

    def __init__(
        self,
        api: GitHubApiClient,
        tokens: InstallationTokenProvider,
        *,
        max_pages: int = _MAX_PAGES,
        action_store: ExternalActionStore | None = None,
    ) -> None:
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= _MAX_PAGES
        ):
            raise ValueError(f"GitHub 对账页数必须在 1 到 {_MAX_PAGES} 之间")
        self._api = api
        self._tokens = tokens
        self._max_pages = max_pages
        self._action_store = action_store

    def __call__(self, details: StoredReviewDetails) -> None:
        token = self._tokens.get_token(details.installation_id)
        owner = f"publisher:{uuid4()}"
        current_sha = self._validate_target(details, token)
        visible_findings = self._visible_findings(details)
        inline_candidates = tuple(
            finding
            for finding in visible_findings
            if self._can_publish_inline(finding, details, current_sha)
        )[:_MAX_INLINE_COMMENTS]
        candidate_markers = {
            self.inline_marker_for(details, finding) for finding in inline_candidates
        }

        existing_inline_markers = (
            self._load_inline_markers(
                details,
                token,
                wanted_markers=candidate_markers,
            )
            if candidate_markers
            else set()
        )
        existing_inline_markers.intersection_update(candidate_markers)
        missing_inline = tuple(
            finding
            for finding in inline_candidates
            if self.inline_marker_for(details, finding)
            not in existing_inline_markers
        )
        inline_degraded = False
        if missing_inline:
            try:
                self._post_inline_review(details, token, missing_inline, owner=owner)
            except _InlineReviewTooLarge:
                inline_degraded = True
            except SafeApplicationError as exc:
                if not self._is_invalid_inline_location(exc):
                    raise
                inline_degraded = True

        published_inline_count = len(existing_inline_markers)
        if not inline_degraded:
            published_inline_count += len(missing_inline)

        check_id = self._find_check_run(details, token)
        self._upsert_check_run(
            details,
            token,
            check_id=check_id,
            visible_findings=visible_findings,
            inline_count=published_inline_count,
            inline_degraded=inline_degraded,
            owner=owner,
        )

        marker = self.marker_for(details.review_run_id)
        comment_id = self._find_summary_comment(details, token, marker)
        body = self.render_comment(
            details,
            marker=marker,
            inline_count=published_inline_count,
            inline_degraded=inline_degraded,
        )
        self._upsert_summary_comment(
            details,
            token,
            comment_id=comment_id,
            body=body,
            owner=owner,
        )

    @staticmethod
    def marker_for(review_run_id: str) -> str:
        digest = sha256(review_run_id.encode("utf-8")).hexdigest()[:24]
        return f"<!-- openreviewer-review:{digest} -->"

    @staticmethod
    def inline_marker_for(
        details: StoredReviewDetails,
        finding: StoredFinding,
    ) -> str:
        identity = ":".join(
            (
                str(details.repository_id),
                str(details.pull_request_number),
                details.head_sha.casefold(),
                finding.fingerprint,
            )
        )
        digest = sha256(identity.encode("utf-8")).hexdigest()[:24]
        return f"<!-- openreviewer-inline:{digest} -->"

    @staticmethod
    def check_external_id_for(details: StoredReviewDetails) -> str:
        identity = getattr(details, "review_version_key", "") or ":".join(
            (
                str(details.repository_id),
                str(details.pull_request_number),
                details.head_sha.casefold(),
            )
        )
        return "openreviewer-" + sha256(identity.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _visible_findings(details: StoredReviewDetails) -> tuple[StoredFinding, ...]:
        return tuple(
            finding
            for finding in details.findings
            if getattr(finding, "adjudication_status", "unreviewed") == "valid"
        )

    @staticmethod
    def _can_publish_inline(
        finding: StoredFinding,
        details: StoredReviewDetails,
        current_sha: str,
    ) -> bool:
        admitted_categories = {
            gate.category
            for gate in getattr(details, "evaluation_gates", ())
            if gate.admitted
        }
        start_line = getattr(finding, "location_start_line", None)
        end_line = getattr(finding, "location_end_line", None)
        adjudication = getattr(finding, "adjudication_status", None)
        # 兼容迁移前的测试/调用方对象；真实数据库记录必须带独立字段。
        evidence_status = getattr(finding, "evidence_verification_status", None)
        if evidence_status is None:
            evidence_status = "verified" if adjudication == "valid" else "unverified"
        return (
            getattr(finding, "verification_status", None) == "verified"
            and evidence_status == "verified"
            and adjudication == "valid"
            and getattr(finding, "location_file", None) is not None
            and isinstance(start_line, int)
            and isinstance(end_line, int)
            and 0 < start_line <= end_line
            and getattr(finding, "location_in_diff", False) is True
            and getattr(finding, "location_side", None) == "right"
            and getattr(finding, "head_sha", "").casefold()
            == current_sha.casefold()
            and getattr(finding, "confidence", 0.0) >= 0.90
            and getattr(finding, "category", None) != "test_gap"
            and getattr(finding, "category", None) in admitted_categories
        )

    def _validate_target(self, details: StoredReviewDetails, token: str) -> str:
        payload = self._api.request_json(
            "GET",
            f"/repos/{details.repository}/pulls/{details.pull_request_number}",
            bearer_token=token,
            max_response_bytes=512 * 1024,
        ).payload
        head = payload.get("head") if isinstance(payload, dict) else None
        current_sha = head.get("sha") if isinstance(head, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("number") != details.pull_request_number
            or payload.get("state") != "open"
            or payload.get("draft") is not False
            or not isinstance(current_sha, str)
            or current_sha.casefold() != details.head_sha.casefold()
        ):
            raise SafeApplicationError(
                SafeError(
                    code=ErrorCode.GITHUB_REQUEST_REJECTED,
                    safe_message="Pull Request 已更新或不再可发布",
                    retryable=False,
                )
            )
        return current_sha

    def _find_summary_comment(
        self,
        details: StoredReviewDetails,
        token: str,
        marker: str,
    ) -> int | None:
        path = (
            f"/repos/{details.repository}/issues/"
            f"{details.pull_request_number}/comments"
        )
        for item in self._iter_pages(
            path,
            token,
            max_response_bytes=2 * 1024 * 1024,
            require_complete=True,
        ):
            body = item.get("body")
            comment_id = item.get("id")
            if (
                isinstance(comment_id, int)
                and isinstance(body, str)
                and marker in {line.strip() for line in body.splitlines()[:2]}
            ):
                return comment_id
        return None

    def _load_inline_markers(
        self,
        details: StoredReviewDetails,
        token: str,
        *,
        wanted_markers: set[str] | None = None,
    ) -> set[str]:
        path = (
            f"/repos/{details.repository}/pulls/"
            f"{details.pull_request_number}/comments"
        )
        markers: set[str] = set()
        for item in self._iter_pages(
            path,
            token,
            max_response_bytes=2 * 1024 * 1024,
            require_complete=True,
        ):
            body = item.get("body")
            if not isinstance(body, str):
                continue
            for line in body.splitlines()[:2]:
                candidate = line.strip()
                if _INLINE_MARKER_RE.fullmatch(candidate):
                    markers.add(candidate)
                    if wanted_markers is not None and wanted_markers.issubset(markers):
                        return markers
        return markers

    def _find_check_run(
        self,
        details: StoredReviewDetails,
        token: str,
    ) -> int | None:
        path = f"/repos/{details.repository}/commits/{details.head_sha}/check-runs"
        external_id = self.check_external_id_for(details)
        for item in self._iter_pages(
            path,
            token,
            extra_params={"check_name": _CHECK_NAME, "filter": "all"},
            max_response_bytes=2 * 1024 * 1024,
            list_key="check_runs",
            require_complete=True,
        ):
            check_id = item.get("id")
            if (
                isinstance(check_id, int)
                and item.get("name") == _CHECK_NAME
                and item.get("external_id") == external_id
            ):
                return check_id
        return None

    def _iter_pages(
        self,
        path: str,
        token: str,
        *,
        extra_params: dict[str, str] | None = None,
        max_response_bytes: int,
        list_key: str | None = None,
        require_complete: bool = False,
    ) -> Iterator[dict[str, object]]:
        seen_count = 0
        expected_total: int | None = None
        for page in range(1, self._max_pages + 1):
            params: dict[str, str | int] = {
                "per_page": _PAGE_SIZE,
                "page": page,
            }
            if extra_params:
                params.update(extra_params)
            result = self._api.request_json(
                "GET",
                path,
                bearer_token=token,
                params=params,
                max_response_bytes=max_response_bytes,
            )
            payload = result.payload
            if list_key:
                items = (
                    payload.get(list_key)
                    if isinstance(payload, dict)
                    else None
                )
                if isinstance(payload, dict) and "total_count" in payload:
                    raw_total = payload.get("total_count")
                    if (
                        not isinstance(raw_total, int)
                        or isinstance(raw_total, bool)
                        or raw_total < 0
                    ):
                        raise SafeApplicationError(
                            SafeError(
                                code=ErrorCode.GITHUB_INVALID_RESPONSE,
                                safe_message="GitHub 发布对账总数格式无效",
                                retryable=False,
                            )
                        )
                    if expected_total is None:
                        expected_total = raw_total
                    elif expected_total != raw_total:
                        raise SafeApplicationError(
                            SafeError(
                                code=ErrorCode.GITHUB_INVALID_RESPONSE,
                                safe_message="GitHub 发布对账总数前后不一致",
                                retryable=False,
                            )
                        )
            else:
                items = payload
            if not isinstance(items, list):
                raise SafeApplicationError(
                    SafeError(
                        code=ErrorCode.GITHUB_INVALID_RESPONSE,
                        safe_message="GitHub 发布对账列表格式无效",
                        retryable=False,
                    )
                )
            seen_count += len(items)
            audit = getattr(result, "audit", None)
            has_next = getattr(audit, "has_next_page", None)
            if has_next is not None and not isinstance(has_next, bool):
                raise SafeApplicationError(
                    SafeError(
                        code=ErrorCode.GITHUB_INVALID_RESPONSE,
                        safe_message="GitHub 发布分页标记格式无效",
                        retryable=False,
                    )
                )

            # Check Runs 返回 total_count；它比“本页是否刚好 100 条”更可靠。
            if expected_total is not None and seen_count > expected_total:
                raise SafeApplicationError(
                    SafeError(
                        code=ErrorCode.GITHUB_INVALID_RESPONSE,
                        safe_message="GitHub 发布对账条目数超过声明总数",
                        retryable=False,
                    )
                )
            total_complete = (
                expected_total is not None and seen_count == expected_total
            )
            if (
                expected_total is not None
                and not total_complete
                and not items
            ):
                raise SafeApplicationError(
                    SafeError(
                        code=ErrorCode.GITHUB_INVALID_RESPONSE,
                        safe_message="GitHub 发布对账总数与分页内容不一致",
                        retryable=False,
                    )
                )

            should_continue = True
            # 先校验本页的计数和分页元数据，再把条目交给查找方；这样即使
            # 查找方在首个命中项处提前返回，也不会绕过明显的响应完整性错误。
            if has_next is False:
                if expected_total is not None and not total_complete:
                    raise SafeApplicationError(
                        SafeError(
                            code=ErrorCode.GITHUB_INVALID_RESPONSE,
                            safe_message="GitHub 发布分页标记与总数不一致",
                            retryable=False,
                        )
                    )
                should_continue = False
            elif total_complete:
                if has_next is True:
                    raise SafeApplicationError(
                        SafeError(
                            code=ErrorCode.GITHUB_INVALID_RESPONSE,
                            safe_message="GitHub 发布分页标记与总数不一致",
                            retryable=False,
                        )
                    )
                should_continue = False
            elif has_next is None and expected_total is None:
                should_continue = len(items) >= _PAGE_SIZE

            at_limit = should_continue and page == self._max_pages

            for item in items:
                if isinstance(item, dict):
                    yield item
            # 先让调用方检查本页条目；如果它在最后一页找到目标并提前返回，
            # 不需要为了证明其余页不存在而失败。只有完整消费到页尾时才报上限。
            if at_limit and require_complete:
                raise SafeApplicationError(
                    SafeError(
                        code=ErrorCode.GITHUB_REQUEST_REJECTED,
                        safe_message="GitHub 对账结果超过安全上限，请先人工核对",
                        retryable=False,
                        details={
                            "max_pages": self._max_pages,
                            "page_size": _PAGE_SIZE,
                        },
                    )
                )
            if not should_continue or at_limit:
                break

    def _post_inline_review(
        self,
        details: StoredReviewDetails,
        token: str,
        findings: tuple[StoredFinding, ...],
        *,
        owner: str,
    ) -> None:
        comments: list[dict[str, object]] = []
        for finding in findings:
            start_line = finding.location_start_line
            end_line = finding.location_end_line
            if (
                finding.location_file is None
                or start_line is None
                or end_line is None
            ):
                continue
            comment: dict[str, object] = {
                "path": finding.location_file,
                "line": end_line,
                "side": "RIGHT",
                "body": self.render_inline_comment(details, finding),
            }
            if start_line < end_line:
                comment["start_line"] = start_line
                comment["start_side"] = "RIGHT"
            comments.append(comment)
        if not comments:
            return
        path = (
            f"/repos/{details.repository}/pulls/"
            f"{details.pull_request_number}/reviews"
        )
        base_body = {
            "commit_id": details.head_sha,
            "event": "COMMENT",
            "body": "OpenReviewer 已发布通过评测准入的高置信问题。",
        }

        def request_size(items: list[dict[str, object]]) -> int:
            payload = {**base_body, "comments": items}
            return len(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )

        batches: list[list[dict[str, object]]] = []
        current: list[dict[str, object]] = []
        for comment in comments:
            candidate = [*current, comment]
            if request_size(candidate) <= _MAX_INLINE_REVIEW_REQUEST_BYTES:
                current.append(comment)
                continue
            if not current or request_size([comment]) > _MAX_INLINE_REVIEW_REQUEST_BYTES:
                raise _InlineReviewTooLarge()
            batches.append(current)
            current = [comment]
        if current:
            batches.append(current)
        if len(batches) > _MAX_INLINE_REVIEW_REQUESTS:
            raise _InlineReviewTooLarge()

        for index, batch in enumerate(batches):
            marker_key = "|".join(
                str(comment.get("body", "")).splitlines()[0]
                for comment in batch
            )
            self._execute_external_action(
                details,
                token,
                owner=owner,
                action_type="inline_review",
                discriminator=f"{index}:{marker_key}",
                method="POST",
                path=path,
                json_body={**base_body, "comments": batch},
                max_response_bytes=512 * 1024,
                target="行内评论",
            )

    def _upsert_check_run(
        self,
        details: StoredReviewDetails,
        token: str,
        *,
        check_id: int | None,
        visible_findings: tuple[StoredFinding, ...],
        inline_count: int,
        inline_degraded: bool,
        owner: str,
    ) -> None:
        conclusion = (
            "success"
            if not visible_findings
            and getattr(details, "coverage_status", "unknown") == "complete"
            else "neutral"
        )
        gate_count = sum(
            gate.admitted for gate in getattr(details, "evaluation_gates", ())
        )
        summary = (
            f"发现 {len(visible_findings)} 条候选问题；"
            f"已发布 {inline_count} 条行内评论；"
            f"{gate_count} 个风险域通过历史评测准入。"
        )
        if inline_degraded:
            summary += "行内定位已失效，本轮已安全降级为汇总展示。"
        output = {
            "title": "OpenReviewer 审查完成",
            "summary": summary,
            "text": self.render_comment(
                details,
                marker=self.marker_for(details.review_run_id),
                inline_count=inline_count,
                inline_degraded=inline_degraded,
            ),
        }
        body: dict[str, object] = {
            "name": _CHECK_NAME,
            "external_id": self.check_external_id_for(details),
            "status": "completed",
            "conclusion": conclusion,
            "output": output,
        }
        if check_id is None:
            body["head_sha"] = details.head_sha
            method = "POST"
            path = f"/repos/{details.repository}/check-runs"
        else:
            method = "PATCH"
            path = f"/repos/{details.repository}/check-runs/{check_id}"
        self._execute_external_action(
            details,
            token,
            owner=owner,
            action_type="check_run",
            discriminator="check",
            method=method,
            path=path,
            json_body=body,
            max_response_bytes=512 * 1024,
            target="Check Run",
        )

    def _upsert_summary_comment(
        self,
        details: StoredReviewDetails,
        token: str,
        *,
        comment_id: int | None,
        body: str,
        owner: str,
    ) -> None:
        if comment_id is None:
            method = "POST"
            path = (
                f"/repos/{details.repository}/issues/"
                f"{details.pull_request_number}/comments"
            )
        else:
            method = "PATCH"
            path = f"/repos/{details.repository}/issues/comments/{comment_id}"
        self._execute_external_action(
            details,
            token,
            owner=owner,
            action_type="summary_comment",
            discriminator="summary",
            method=method,
            path=path,
            json_body={"body": body},
            max_response_bytes=256 * 1024,
            target="汇总评论",
        )

    def _execute_external_action(
        self,
        details: StoredReviewDetails,
        token: str,
        *,
        owner: str,
        action_type: str,
        discriminator: str,
        method: str,
        path: str,
        json_body: dict[str, object],
        max_response_bytes: int,
        target: str,
    ) -> str:
        """执行一个有审计租约的 GitHub 写请求。"""

        action_key = self._action_key(details, action_type, discriminator)
        store = self._action_store
        if store is not None:
            existing_remote_id = store.acquire(
                action_key=action_key,
                review_run_id=details.review_run_id,
                action_type=action_type,
                owner=owner,
                request_method=method,
                request_path=path,
            )
            if existing_remote_id is not None:
                return existing_remote_id
        try:
            result = self._api.request_json(
                method,
                path,
                bearer_token=token,
                json_body=json_body,
                max_response_bytes=max_response_bytes,
            )
            remote_id = remote_id_from_result(result, target)
        except SafeApplicationError as exc:
            if store is not None:
                store.fail(
                    action_key=action_key,
                    owner=owner,
                    error_code=exc.error.code.value,
                    error_message=exc.error.safe_message,
                    retryable=exc.error.retryable,
                    details=exc.error.details,
                )
            raise
        except Exception as exc:
            safe_error = SafeError.from_exception(exc)
            if store is not None:
                store.fail(
                    action_key=action_key,
                    owner=owner,
                    error_code=safe_error.code.value,
                    error_message=safe_error.safe_message,
                    retryable=safe_error.retryable,
                    details=safe_error.details,
                )
            raise
        if store is not None:
            store.succeed(
                action_key=action_key,
                owner=owner,
                remote_id=remote_id,
                audit=result.audit,
            )
        return remote_id

    @staticmethod
    def _action_key(
        details: StoredReviewDetails,
        action_type: str,
        discriminator: str,
    ) -> str:
        identity = "|".join(
            (
                details.review_run_id,
                getattr(details, "review_version_key", ""),
                action_type,
                discriminator,
            )
        )
        return (
            f"openreviewer:{details.review_run_id}:{action_type}:"
            f"{sha256(identity.encode('utf-8')).hexdigest()[:32]}"
        )[:300]

    @staticmethod
    def _require_remote_id(
        payload: object,
        response_status: int | None,
        target: str,
    ) -> None:
        if isinstance(payload, dict) and isinstance(payload.get("id"), int):
            return
        raise SafeApplicationError(
            SafeError(
                code=ErrorCode.GITHUB_INVALID_RESPONSE,
                safe_message=f"GitHub {target}发布返回了无效结果",
                retryable=False,
                details={"status_code": response_status},
            )
        )

    @staticmethod
    def _is_invalid_inline_location(exc: SafeApplicationError) -> bool:
        return (
            exc.error.code is ErrorCode.GITHUB_REQUEST_REJECTED
            and exc.error.details.get("status_code") == 422
        )

    @classmethod
    def render_inline_comment(
        cls,
        details: StoredReviewDetails,
        finding: StoredFinding,
    ) -> str:
        marker = cls.inline_marker_for(details, finding)
        lines = [
            marker,
            f"**[{_safe_field(finding.severity, 32)}] {_safe_field(finding.title, 300)}**",
            "",
            f"证据：{_safe_field(finding.evidence, _MAX_FIELD_CHARS)}",
            "",
            f"影响：{_safe_field(finding.impact, _MAX_FIELD_CHARS)}",
            "",
            f"建议：{_safe_field(finding.suggestion, _MAX_FIELD_CHARS)}",
        ]
        if finding.required_test:
            lines.extend(
                ["", f"建议补测：{_safe_field(finding.required_test, _MAX_FIELD_CHARS)}"]
            )
        return "\n".join(lines)

    @classmethod
    def render_comment(
        cls,
        details: StoredReviewDetails,
        *,
        marker: str | None = None,
        inline_count: int = 0,
        inline_degraded: bool = False,
    ) -> str:
        marker = marker or cls.marker_for(details.review_run_id)
        if not _MARKER_RE.fullmatch(marker):
            raise ValueError("GitHub 评论幂等标记格式无效")
        visible_findings = cls._visible_findings(details)
        gates = tuple(getattr(details, "evaluation_gates", ()))
        admitted_gate_count = sum(gate.admitted for gate in gates)
        lines = [
            marker,
            "## OpenReviewer 审查结果",
            "",
            f"- 仓库：`{details.repository}`",
            f"- Pull Request：#{details.pull_request_number}",
            f"- 提交：`{details.head_sha[:12]}`",
            f"- 候选问题：{len(visible_findings)} 条",
            f"- 行内评论：{inline_count} 条",
            f"- 评测准入：{admitted_gate_count}/{len(gates)} 个风险域",
            "",
        ]
        if inline_degraded:
            lines.extend(
                [
                    "> 本轮行内评论无法安全发布，相关问题已自动降级到此汇总。",
                    "",
                ]
            )
        if not visible_findings:
            lines.append("审查已完成，没有需要发布的问题。")
        else:
            lines.append("### 候选问题")
            for index, finding in enumerate(visible_findings, start=1):
                severity = _safe_field(finding.severity, 32)
                title = _safe_field(finding.title, _MAX_FIELD_CHARS)
                lines.extend(
                    [
                        f"#### {index}. [{severity}] {title}",
                        f"- 类别：{_safe_field(finding.category, 64)}",
                    ]
                )
                if finding.location_file:
                    location = f"`{_safe_field(finding.location_file, 1024)}`"
                    if finding.location_start_line is not None:
                        end = finding.location_end_line or finding.location_start_line
                        location += f":{finding.location_start_line}-{end}"
                    lines.append(f"- 位置：{location}")
                lines.extend(
                    [
                        f"- 证据：{_safe_field(finding.evidence, _MAX_FIELD_CHARS)}",
                        f"- 影响：{_safe_field(finding.impact, _MAX_FIELD_CHARS)}",
                        f"- 建议：{_safe_field(finding.suggestion, _MAX_FIELD_CHARS)}",
                    ]
                )
                if finding.required_test:
                    lines.append(
                        f"- 建议补测：{_safe_field(finding.required_test, _MAX_FIELD_CHARS)}"
                    )
                lines.append("")
        rendered = "\n".join(lines).rstrip() + "\n"
        encoded = rendered.encode("utf-8")
        if len(encoded) <= _MAX_COMMENT_BODY_BYTES:
            return rendered
        suffix = "\n\n> 其余内容因 GitHub 评论大小限制已省略。\n"
        available = _MAX_COMMENT_BODY_BYTES - len(suffix.encode("utf-8"))
        truncated = encoded[:available].decode("utf-8", errors="ignore")
        return truncated.rstrip() + suffix


def _safe_field(value: object, limit: int) -> str:
    """清理模型文本并阻止它改变 GitHub 评论的 Markdown 语义。

    评论主体中的标题、证据和建议都来自模型或仓库内容，不能直接当作
    Markdown/HTML 片段拼接。这里保留 ``<redacted>`` 等平台生成的脱敏标记，
    其余控制字符、链接、提及和 Markdown 分隔符全部转成无执行语义的文本。
    """

    try:
        text = redact_text(str(value))
    except Exception:
        text = ""
    # 控制字符可能造成日志/评论解析差异；空白统一成单个空格也可避免
    # 模型文本伪造新的 Markdown 行或幂等标记行。
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = " ".join(text.split())
    if not text:
        return "—"

    # 先保留脱敏组件生成的安全占位符，之后再恢复；模型字段本身不能生成
    # HTML 标签。链接和 @ 提及直接替换，避免 GitHub 自动生成可点击/通知行为。
    redaction_tokens: list[str] = []

    def hold_redaction(match: re.Match[str]) -> str:
        # 仅使用字母/数字，避免后续 Markdown 转义改变占位符本身。
        token = f"OPENREVIEWERREDACTION{len(redaction_tokens)}X"
        redaction_tokens.append(match.group(0))
        return token

    text = _SAFE_REDACTION_TOKEN_RE.sub(hold_redaction, text)
    text = _UNTRUSTED_URL_RE.sub("[链接已隐藏]", text)
    text = _UNTRUSTED_MENTION_RE.sub("[提及已隐藏]", text)
    # 反引号不能仅靠反斜杠转义：当字段位于代码 span 中时，反引号仍可能
    # 闭合外层 span。因此改成普通撇号，再处理其余 Markdown 控制字符。
    text = text.replace("`", "'")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _MARKDOWN_CONTROL_RE.sub(r"\\\1", text)
    for index, token in enumerate(redaction_tokens):
        text = text.replace(f"OPENREVIEWERREDACTION{index}X", token)
    return text[:limit] if text else "—"
