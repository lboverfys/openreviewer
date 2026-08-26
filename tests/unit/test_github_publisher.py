import json
from types import SimpleNamespace

import httpx
import pytest

from domain.security import ErrorCode, SafeApplicationError
from services.github import GitHubApiClient, GitHubClientSettings
from services.github_publisher import GitHubReviewPublisher


class StaticTokens:
    def __init__(self) -> None:
        self.installations: list[int] = []

    def get_token(self, installation_id: int) -> str:
        self.installations.append(installation_id)
        return "test-installation-token"


def _details(*, findings=()):
    return SimpleNamespace(
        review_run_id="run-publish-001",
        installation_id=77,
        repository="owner/repository",
        pull_request_number=19,
        head_sha="a" * 40,
        findings=findings,
    )


def test_publisher_posts_bounded_comment_with_stable_marker() -> None:
    requests: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            requests.append((request.method, None))
            if request.url.path.endswith("/pulls/19"):
                return httpx.Response(
                    200,
                    json={
                        "number": 19,
                        "state": "open",
                        "draft": False,
                        "head": {"sha": "a" * 40},
                    },
                )
            return httpx.Response(200, json=[])
        body = json.loads(request.content)
        requests.append((request.method, body))
        return httpx.Response(201, json={"id": 123})

    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=client,
    )
    tokens = StaticTokens()
    publisher = GitHubReviewPublisher(api, tokens)
    finding = SimpleNamespace(
        severity="high",
        category="security",
        title="权限绕过",
        evidence="Authorization: Bearer secret-value",
        impact="普通用户可进入管理员流程",
        suggestion="增加统一鉴权",
        required_test="增加拒绝访问测试",
        location_file="src/auth.py",
        location_start_line=8,
        location_end_line=9,
    )

    publisher(_details(findings=(finding,)))

    assert tokens.installations == [77]
    assert [method for method, _body in requests] == ["GET", "GET", "POST"]
    posted = requests[2][1]
    assert isinstance(posted, dict)
    comment = posted["body"]
    assert GitHubReviewPublisher.marker_for("run-publish-001") in comment
    assert "`src/auth.py`:8-9" in comment
    assert "权限绕过" in comment
    assert "secret-value" not in comment
    assert "<redacted>" in comment
    client.close()


def test_publisher_skips_post_when_marker_already_exists() -> None:
    marker = GitHubReviewPublisher.marker_for("run-publish-001")
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json=[{"id": 9, "body": f"{marker}\n已发布"}],
        )

    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=client,
    )
    publisher = GitHubReviewPublisher(api, StaticTokens())

    publisher(_details())

    assert methods == ["GET"]
    client.close()


def test_publisher_rejects_stale_head_before_post() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={
                "number": 19,
                "state": "open",
                "draft": False,
                "head": {"sha": "b" * 40},
            },
        )

    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=client,
    )

    with pytest.raises(SafeApplicationError) as caught:
        GitHubReviewPublisher(api, StaticTokens())(_details())
    assert caught.value.error.code is ErrorCode.GITHUB_REQUEST_REJECTED
    assert paths == [
        "/repos/owner/repository/issues/19/comments",
        "/repos/owner/repository/pulls/19",
    ]
    client.close()


def test_render_comment_excludes_rejected_findings() -> None:
    rejected = SimpleNamespace(
        severity="low",
        category="style",
        title="已忽略问题",
        evidence="无",
        impact="无",
        suggestion="无",
        required_test=None,
        location_file=None,
        location_start_line=None,
        location_end_line=None,
        verification_status="rejected",
    )

    rendered = GitHubReviewPublisher.render_comment(
        _details(findings=(rejected,))
    )

    assert "已忽略问题" not in rendered
    assert "候选问题：0 条" in rendered
    assert "没有需要发布的问题" in rendered


def test_render_comment_truncates_at_utf8_60_kib_boundary() -> None:
    finding = SimpleNamespace(
        severity="high",
        category="security",
        title="超长候选问题" * 400,
        evidence="证据内容" * 500,
        impact="影响内容" * 500,
        suggestion="修复建议" * 500,
        required_test="补充测试" * 500,
        location_file="src/very-long-path.py",
        location_start_line=1,
        location_end_line=2,
        verification_status="verified",
    )

    rendered = GitHubReviewPublisher.render_comment(
        _details(findings=(finding,) * 20)
    )

    assert len(rendered.encode("utf-8")) <= 60 * 1024
    assert rendered.startswith(GitHubReviewPublisher.marker_for("run-publish-001"))
    assert rendered.endswith("> 其余内容因 GitHub 评论大小限制已省略。\n")
    rendered.encode("utf-8").decode("utf-8")
