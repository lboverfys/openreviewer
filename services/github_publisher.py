"""批准后人工发布审查结果到 GitHub Pull Request 评论。"""

from __future__ import annotations

from hashlib import sha256
import re
from typing import Final, Protocol

from domain.security import ErrorCode, SafeApplicationError, SafeError, redact_text
from services.github import GitHubApiClient
from services.review_management import StoredReviewDetails


class InstallationTokenProvider(Protocol):
    """提供短期 GitHub App installation token。"""

    def get_token(self, installation_id: int) -> str: ...


_COMMENTS_PAGE_SIZE: Final[int] = 100
_MAX_COMMENT_PAGES: Final[int] = 10
_MAX_COMMENT_BODY_BYTES: Final[int] = 60 * 1024
_MAX_FIELD_CHARS: Final[int] = 2_000
_MARKER_RE = re.compile(r"^<!-- openreviewer-review:[0-9a-f]{24} -->$")


class GitHubReviewPublisher:
    """把一条已批准审查发布为幂等 PR 评论。

    ``SqlAlchemyReviewManagementRepository.publish`` 会先提交
    ``publishing`` 状态，再调用此对象，因此本类永远不会在数据库事务中运行。
    评论首行包含由运行 ID 派生的固定标记；重试或进程恢复时先分页查找该标记，
    找到后直接视为成功。
    """

    def __init__(
        self,
        api: GitHubApiClient,
        tokens: InstallationTokenProvider,
        *,
        max_comment_pages: int = _MAX_COMMENT_PAGES,
    ) -> None:
        if not 1 <= max_comment_pages <= 10:
            raise ValueError("GitHub 评论查询页数必须在 1 到 10 之间")
        self._api = api
        self._tokens = tokens
        self._max_comment_pages = max_comment_pages

    def __call__(self, details: StoredReviewDetails) -> None:
        token = self._tokens.get_token(details.installation_id)
        comments_path = (
            f"/repos/{details.repository}/issues/{details.pull_request_number}/comments"
        )
        marker = self.marker_for(details.review_run_id)
        if self._has_marker(comments_path, token, marker):
            return
        self._validate_target(details, token)
        body = self.render_comment(details, marker=marker)
        result = self._api.request_json(
            "POST",
            comments_path,
            bearer_token=token,
            json_body={"body": body},
            max_response_bytes=256 * 1024,
        )
        payload = result.payload
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
            raise SafeApplicationError(
                SafeError(
                    code=ErrorCode.GITHUB_INVALID_RESPONSE,
                    safe_message="GitHub 评论发布返回了无效结果",
                    retryable=False,
                    details={
                        "status_code": result.audit.response_status,
                        "github_request_id": result.audit.github_request_id,
                    },
                )
            )

    @staticmethod
    def marker_for(review_run_id: str) -> str:
        digest = sha256(review_run_id.encode("utf-8")).hexdigest()[:24]
        return f"<!-- openreviewer-review:{digest} -->"

    def _has_marker(self, path: str, token: str, marker: str) -> bool:
        for page in range(1, self._max_comment_pages + 1):
            payload = self._api.request_json(
                "GET",
                path,
                bearer_token=token,
                params={"per_page": _COMMENTS_PAGE_SIZE, "page": page},
                max_response_bytes=2 * 1024 * 1024,
            ).payload
            if not isinstance(payload, list):
                raise SafeApplicationError(
                    SafeError(
                        code=ErrorCode.GITHUB_INVALID_RESPONSE,
                        safe_message="GitHub 评论列表格式无效",
                        retryable=False,
                    )
                )
            for item in payload:
                if not isinstance(item, dict):
                    continue
                body = item.get("body")
                if isinstance(body, str) and any(
                    line.strip() == marker for line in body.splitlines()[:2]
                ):
                    return True
            if len(payload) < _COMMENTS_PAGE_SIZE:
                break
        return False

    def _validate_target(self, details: StoredReviewDetails, token: str) -> None:
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

    @classmethod
    def render_comment(
        cls,
        details: StoredReviewDetails,
        *,
        marker: str | None = None,
    ) -> str:
        marker = marker or cls.marker_for(details.review_run_id)
        if not _MARKER_RE.fullmatch(marker):
            raise ValueError("GitHub 评论幂等标记格式无效")
        visible_findings = tuple(
            finding
            for finding in details.findings
            if getattr(finding, "verification_status", "unverified") != "rejected"
        )
        lines = [
            marker,
            "## OpenReviewer 审查结果",
            "",
            f"- 仓库：`{details.repository}`",
            f"- Pull Request：#{details.pull_request_number}",
            f"- 提交：`{details.head_sha[:12]}`",
            f"- 候选问题：{len(visible_findings)} 条",
            "",
        ]
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
    """清理模型文本中的常见凭据并限制评论大小。"""

    text = redact_text(str(value))
    text = " ".join(text.replace("\x00", "").split())
    return text[:limit] if text else "—"
