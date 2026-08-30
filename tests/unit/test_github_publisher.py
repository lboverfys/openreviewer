import json
from types import SimpleNamespace

import httpx
import pytest

from domain.security import ErrorCode, SafeApplicationError
from services.github import GitHubApiClient, GitHubClientSettings
from services.github_publisher import GitHubReviewPublisher

HEAD_SHA = "a" * 40


class StaticTokens:
    def __init__(self) -> None:
        self.installations: list[int] = []

    def get_token(self, installation_id: int) -> str:
        self.installations.append(installation_id)
        return "test-installation-token"


def _gate(*, admitted: bool = True, category: str = "security"):
    return SimpleNamespace(category=category, admitted=admitted)


def _finding(**overrides):
    values = {
        "id": "finding-001",
        "fingerprint": "1" * 64,
        "head_sha": HEAD_SHA,
        "severity": "high",
        "category": "security",
        "title": "权限绕过",
        "evidence": "Authorization: Bearer secret-value",
        "impact": "普通用户可进入管理员流程",
        "suggestion": "增加统一鉴权",
        "required_test": "增加拒绝访问测试",
        "confidence": 0.95,
        "verification_status": "verified",
        "adjudication_status": "valid",
        "location_file": "src/auth.py",
        "location_start_line": 8,
        "location_end_line": 9,
        "location_side": "right",
        "location_in_diff": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _details(*, findings=(), gates=(), coverage_status="complete"):
    return SimpleNamespace(
        review_run_id="run-publish-001",
        review_version_key=f"42:19:{HEAD_SHA}",
        installation_id=77,
        repository_id=42,
        repository="owner/repository",
        pull_request_number=19,
        head_sha=HEAD_SHA,
        coverage_status=coverage_status,
        evaluation_gates=gates,
        findings=findings,
    )


def _api(handler):
    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    return (
        GitHubApiClient(
            GitHubClientSettings(api_base_url="https://api.github.test"),
            client=client,
        ),
        client,
    )


def _open_pr() -> dict[str, object]:
    return {
        "number": 19,
        "state": "open",
        "draft": False,
        "head": {"sha": HEAD_SHA},
    }


@pytest.mark.parametrize("max_pages", [0, 11, True, "10"])
def test_publisher_rejects_unsafe_page_limits(max_pages: object) -> None:
    with pytest.raises(ValueError):
        GitHubReviewPublisher(
            object(),
            object(),
            max_pages=max_pages,
        )  # type: ignore[arg-type]


def test_publisher_batches_inline_comments_and_creates_check_and_summary() -> None:
    requests: list[tuple[str, str, object | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        path = request.url.path
        if path.endswith("/pulls/19"):
            return httpx.Response(200, json=_open_pr())
        if request.method == "GET" and path.endswith("/pulls/19/comments"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and path.endswith("/pulls/19/reviews"):
            return httpx.Response(201, json={"id": 101})
        if request.method == "GET" and path.endswith(f"/{HEAD_SHA}/check-runs"):
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        if request.method == "POST" and path.endswith("/check-runs"):
            return httpx.Response(201, json={"id": 202})
        if request.method == "GET" and path.endswith("/issues/19/comments"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and path.endswith("/issues/19/comments"):
            return httpx.Response(201, json={"id": 303})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    api, client = _api(handler)
    tokens = StaticTokens()
    publisher = GitHubReviewPublisher(api, tokens)
    first = _finding()
    second = _finding(
        id="finding-002",
        fingerprint="2" * 64,
        title="第二个问题",
        location_start_line=15,
        location_end_line=15,
    )

    publisher(_details(findings=(first, second), gates=(_gate(),)))

    assert tokens.installations == [77]
    review_request = next(item for item in requests if item[1].endswith("/reviews"))
    review_body = review_request[2]
    assert isinstance(review_body, dict)
    assert len(review_body["comments"]) == 2
    assert review_body["comments"][0]["start_line"] == 8
    assert review_body["comments"][0]["line"] == 9
    assert review_body["comments"][1]["line"] == 15
    assert "start_line" not in review_body["comments"][1]
    assert "secret-value" not in review_body["comments"][0]["body"]

    check_request = next(
        item
        for item in requests
        if item[0] == "POST" and item[1].endswith("/check-runs")
    )
    check_body = check_request[2]
    assert isinstance(check_body, dict)
    assert check_body["head_sha"] == HEAD_SHA
    assert check_body["conclusion"] == "neutral"
    assert check_body["external_id"] == publisher.check_external_id_for(_details())

    summary_request = requests[-1]
    assert summary_request[:2] == (
        "POST",
        "/repos/owner/repository/issues/19/comments",
    )
    summary_body = summary_request[2]
    assert isinstance(summary_body, dict)
    assert "行内评论：2 条" in summary_body["body"]
    assert "secret-value" not in summary_body["body"]
    assert "<redacted>" in summary_body["body"]
    client.close()


def test_publisher_splits_large_inline_review_payloads() -> None:
    review_requests: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/19"):
            return httpx.Response(200, json=_open_pr())
        if request.method == "GET" and path.endswith("/pulls/19/comments"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and path.endswith("/pulls/19/reviews"):
            body = json.loads(request.content)
            assert isinstance(body, dict)
            comments = body.get("comments")
            assert isinstance(comments, list)
            review_requests.append((len(request.content), len(comments)))
            return httpx.Response(201, json={"id": len(review_requests)})
        if request.method == "GET" and path.endswith(f"/{HEAD_SHA}/check-runs"):
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        if request.method == "POST" and path.endswith("/check-runs"):
            return httpx.Response(201, json={"id": 202})
        if request.method == "GET" and path.endswith("/issues/19/comments"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and path.endswith("/issues/19/comments"):
            return httpx.Response(201, json={"id": 303})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    findings = tuple(
        _finding(
            id=f"finding-{index}",
            fingerprint=f"{index:064x}",
            evidence="证据" * 2_000,
            impact="影响" * 2_000,
            suggestion="建议" * 2_000,
            required_test="补测" * 2_000,
            location_start_line=index + 1,
            location_end_line=index + 1,
        )
        for index in range(50)
    )
    api, client = _api(handler)
    GitHubReviewPublisher(api, StaticTokens())(
        _details(findings=findings, gates=(_gate(),))
    )

    assert len(review_requests) >= 2
    assert sum(comment_count for _size, comment_count in review_requests) == 50
    assert all(size <= 900 * 1024 for size, _comment_count in review_requests)
    client.close()


def test_publisher_reconciles_and_updates_existing_check_and_summary() -> None:
    details = _details()
    marker = GitHubReviewPublisher.marker_for(details.review_run_id)
    external_id = GitHubReviewPublisher.check_external_id_for(details)
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/pulls/19"):
            return httpx.Response(200, json=_open_pr())
        if request.method == "GET" and path.endswith(f"/{HEAD_SHA}/check-runs"):
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "check_runs": [
                        {
                            "id": 22,
                            "name": "OpenReviewer",
                            "external_id": external_id,
                        }
                    ],
                },
            )
        if request.method == "PATCH" and path.endswith("/check-runs/22"):
            return httpx.Response(200, json={"id": 22})
        if request.method == "GET" and path.endswith("/issues/19/comments"):
            return httpx.Response(200, json=[{"id": 9, "body": f"{marker}\n旧内容"}])
        if request.method == "PATCH" and path.endswith("/issues/comments/9"):
            return httpx.Response(200, json={"id": 9})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    api, client = _api(handler)
    GitHubReviewPublisher(api, StaticTokens())(details)

    assert requests == [
        ("GET", "/repos/owner/repository/pulls/19"),
        ("GET", f"/repos/owner/repository/commits/{HEAD_SHA}/check-runs"),
        ("PATCH", "/repos/owner/repository/check-runs/22"),
        ("GET", "/repos/owner/repository/issues/19/comments"),
        ("PATCH", "/repos/owner/repository/issues/comments/9"),
    ]
    client.close()


def test_publisher_accepts_full_final_check_page_from_total_count() -> None:
    """总数正好等于整页时，不应误判为超过对账上限。"""

    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        pages.append(int(request.url.params["page"]))
        return httpx.Response(
            200,
            json={
                "total_count": 100,
                "check_runs": [{"id": index} for index in range(100)],
            },
        )

    api, client = _api(handler)
    publisher = GitHubReviewPublisher(api, StaticTokens(), max_pages=1)

    items = list(
        publisher._iter_pages(  # noqa: SLF001 - 直接覆盖分页边界
            "/repos/owner/repository/check-runs",
            "test-installation-token",
            max_response_bytes=2 * 1024 * 1024,
            list_key="check_runs",
            require_complete=True,
        )
    )

    assert pages == [1]
    assert len(items) == 100
    client.close()


def test_publisher_uses_check_total_count_when_first_page_is_full() -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        if page == 1:
            return httpx.Response(
                200,
                json={
                    "total_count": 101,
                    "check_runs": [{"id": index} for index in range(100)],
                },
            )
        return httpx.Response(
            200,
            json={"total_count": 101, "check_runs": [{"id": 100}]},
        )

    api, client = _api(handler)
    publisher = GitHubReviewPublisher(api, StaticTokens(), max_pages=2)

    items = list(
        publisher._iter_pages(  # noqa: SLF001 - 直接覆盖分页边界
            "/repos/owner/repository/check-runs",
            "test-installation-token",
            max_response_bytes=2 * 1024 * 1024,
            list_key="check_runs",
            require_complete=True,
        )
    )

    assert pages == [1, 2]
    assert len(items) == 101
    client.close()


def test_publisher_probes_after_full_page_when_link_header_is_unknown() -> None:
    """没有 Link 头时，整页结果要探测下一页，避免漏掉第 101 条。"""

    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        if page == 1:
            return httpx.Response(200, json=[{"id": index} for index in range(100)])
        return httpx.Response(200, json=[])

    api, client = _api(handler)
    publisher = GitHubReviewPublisher(api, StaticTokens(), max_pages=2)

    items = list(
        publisher._iter_pages(  # noqa: SLF001 - 直接覆盖分页边界
            "/repos/owner/repository/issues/19/comments",
            "test-installation-token",
            max_response_bytes=2 * 1024 * 1024,
            require_complete=True,
        )
    )

    assert pages == [1, 2]
    assert len(items) == 100
    client.close()


def test_publisher_accepts_short_page_at_configured_limit() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": index} for index in range(3)])

    api, client = _api(handler)
    publisher = GitHubReviewPublisher(api, StaticTokens(), max_pages=1)

    items = list(
        publisher._iter_pages(  # noqa: SLF001 - 直接覆盖分页边界
            "/repos/owner/repository/issues/19/comments",
            "test-installation-token",
            max_response_bytes=2 * 1024 * 1024,
            require_complete=True,
        )
    )

    assert len(items) == 3
    client.close()


def test_publisher_trusts_explicit_last_link_at_limit() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"id": index} for index in range(100)],
            headers={
                "Link": '<https://api.github.test/items?page=1>; rel="last"'
            },
        )

    api, client = _api(handler)
    publisher = GitHubReviewPublisher(api, StaticTokens(), max_pages=1)

    items = list(
        publisher._iter_pages(  # noqa: SLF001 - 直接覆盖 Link 末页边界
            "/repos/owner/repository/issues/19/comments",
            "test-installation-token",
            max_response_bytes=2 * 1024 * 1024,
            require_complete=True,
        )
    )

    assert len(items) == 100
    client.close()


def test_publisher_rejects_next_page_at_configured_limit() -> None:
    """服务端明确存在下一页时，达到上限必须失败而不能静默漏查。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"id": index} for index in range(100)],
            headers={
                "Link": (
                    '<https://api.github.test/repos/owner/repository/issues/19/'
                    'comments?page=2>; rel="next"'
                )
            },
        )

    api, client = _api(handler)
    publisher = GitHubReviewPublisher(api, StaticTokens(), max_pages=1)

    with pytest.raises(SafeApplicationError) as caught:
        list(
            publisher._iter_pages(  # noqa: SLF001 - 直接覆盖分页边界
                "/repos/owner/repository/issues/19/comments",
                "test-installation-token",
                max_response_bytes=2 * 1024 * 1024,
                require_complete=True,
            )
        )

    assert caught.value.error.code is ErrorCode.GITHUB_REQUEST_REJECTED
    assert "超过安全上限" in caught.value.error.safe_message
    client.close()


def test_publisher_can_find_marker_on_last_full_page() -> None:
    details = _details()
    marker = GitHubReviewPublisher.marker_for(details.review_run_id)
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(int(request.url.params["page"]))
        comments = [{"id": index, "body": "其他评论"} for index in range(99)]
        comments.append({"id": 999, "body": f"{marker}\n已发布"})
        return httpx.Response(200, json=comments)

    api, client = _api(handler)
    publisher = GitHubReviewPublisher(api, StaticTokens(), max_pages=1)

    comment_id = publisher._find_summary_comment(  # noqa: SLF001 - 覆盖末页命中
        details,
        "test-installation-token",
        marker,
    )

    assert comment_id == 999
    assert pages == [1]
    client.close()


def test_publisher_stops_inline_reconciliation_when_all_markers_are_found() -> None:
    details = _details(findings=(_finding(),))
    marker = GitHubReviewPublisher.inline_marker_for(details, details.findings[0])
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(int(request.url.params["page"]))
        comments = [{"id": index, "body": "其他行内评论"} for index in range(99)]
        comments.append({"id": 999, "body": f"{marker}\n已发布"})
        return httpx.Response(200, json=comments)

    api, client = _api(handler)
    publisher = GitHubReviewPublisher(api, StaticTokens(), max_pages=1)

    markers = publisher._load_inline_markers(  # noqa: SLF001 - 覆盖末页提前结束
        details,
        "test-installation-token",
        wanted_markers={marker},
    )

    assert markers == {marker}
    assert pages == [1]
    client.close()


def test_publisher_rejects_stale_head_before_any_write_or_reconciliation() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={**_open_pr(), "head": {"sha": "b" * 40}},
        )

    api, client = _api(handler)
    with pytest.raises(SafeApplicationError) as caught:
        GitHubReviewPublisher(api, StaticTokens())(_details())
    assert caught.value.error.code is ErrorCode.GITHUB_REQUEST_REJECTED
    assert paths == ["/repos/owner/repository/pulls/19"]
    client.close()


def test_invalid_inline_location_degrades_to_check_and_summary() -> None:
    posted_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if path.endswith("/pulls/19"):
            return httpx.Response(200, json=_open_pr())
        if request.method == "GET" and path.endswith("/pulls/19/comments"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and path.endswith("/pulls/19/reviews"):
            return httpx.Response(422, json={"message": "line must be part of diff"})
        if request.method == "GET" and path.endswith(f"/{HEAD_SHA}/check-runs"):
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        if request.method == "GET" and path.endswith("/issues/19/comments"):
            return httpx.Response(200, json=[])
        if request.method == "POST":
            assert isinstance(body, dict)
            posted_bodies.append(body)
            return httpx.Response(201, json={"id": len(posted_bodies)})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    api, client = _api(handler)
    GitHubReviewPublisher(api, StaticTokens())(
        _details(findings=(_finding(),), gates=(_gate(),))
    )

    assert len(posted_bodies) == 2
    assert "安全降级" in posted_bodies[0]["output"]["summary"]
    assert "自动降级" in posted_bodies[1]["body"]
    assert "行内评论：0 条" in posted_bodies[1]["body"]
    client.close()


def test_unadmitted_risk_domain_never_calls_inline_api() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        path = request.url.path
        if path.endswith("/pulls/19"):
            return httpx.Response(200, json=_open_pr())
        if request.method == "GET" and path.endswith(f"/{HEAD_SHA}/check-runs"):
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        if request.method == "GET" and path.endswith("/issues/19/comments"):
            return httpx.Response(200, json=[])
        if request.method == "POST":
            return httpx.Response(201, json={"id": 1})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    api, client = _api(handler)
    GitHubReviewPublisher(api, StaticTokens())(
        _details(findings=(_finding(),), gates=(_gate(admitted=False),))
    )

    assert not any(path.endswith("/pulls/19/comments") for path in paths)
    assert not any(path.endswith("/pulls/19/reviews") for path in paths)
    client.close()


def test_render_comment_excludes_non_valid_findings() -> None:
    rejected = _finding(adjudication_status="false_positive", title="已忽略问题")

    rendered = GitHubReviewPublisher.render_comment(
        _details(findings=(rejected,))
    )

    assert "已忽略问题" not in rendered
    assert "候选问题：0 条" in rendered
    assert "没有需要发布的问题" in rendered


def test_render_comment_neutralizes_untrusted_markdown_links_mentions_and_markers() -> None:
    forged_marker = "<!-- openreviewer-review:0123456789abcdef01234567 -->"
    hostile = _finding(
        title=f"[点击](https://attacker.example/x) @alice {forged_marker}",
        evidence="<script>alert(1)</script> `code` **bold** ![img](https://x.test/i)",
        impact="mailto:operator@example.test www.example.test",
        suggestion="# heading | table ~strike",
        location_file="src/`danger`.py",
    )

    rendered = GitHubReviewPublisher.render_comment(
        _details(findings=(hostile,))
    )
    inline = GitHubReviewPublisher.render_inline_comment(_details(), hostile)

    assert rendered.startswith(GitHubReviewPublisher.marker_for("run-publish-001"))
    assert forged_marker not in rendered
    assert "https://" not in rendered
    assert "mailto:" not in rendered
    assert "@alice" not in rendered
    assert "<script>" not in rendered
    assert "![img](" not in rendered
    assert "**bold**" not in rendered
    assert "`danger`" not in rendered
    assert forged_marker not in inline
    assert "https://" not in inline
    assert "@alice" not in inline


def test_render_comment_truncates_at_utf8_60_kib_boundary() -> None:
    finding = _finding(
        title="超长候选问题" * 400,
        evidence="证据内容" * 500,
        impact="影响内容" * 500,
        suggestion="修复建议" * 500,
        required_test="补充测试" * 500,
        location_file="src/very-long-path.py",
        location_start_line=1,
        location_end_line=2,
    )

    rendered = GitHubReviewPublisher.render_comment(
        _details(findings=(finding,) * 20)
    )

    assert len(rendered.encode("utf-8")) <= 60 * 1024
    assert rendered.startswith(GitHubReviewPublisher.marker_for("run-publish-001"))
    assert rendered.endswith("> 其余内容因 GitHub 评论大小限制已省略。\n")
    rendered.encode("utf-8").decode("utf-8")
