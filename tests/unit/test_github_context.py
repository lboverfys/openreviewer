import json
from datetime import UTC, datetime

import httpx

from domain.enums import CiState, PatchState
from domain.review_planning import RepositoryRulesSnapshot
from domain.security import ErrorCode, SafeApplicationError
from services.github import GitHubApiClient, GitHubClientSettings
from services.github_context import GitHubContextSettings, GitHubReviewContextLoader
from services.review_planning import DeterministicReviewPlanner
from services.task_queue import ReviewTarget

APP_ID = 4699977
INSTALLATION_ID = 156153422
FAKE_TOKEN = "ghs_FAKEINSTALLATIONTOKEN123456789"
HEAD_SHA = "c" * 40
BASE_SHA = "b" * 40


class StaticTokenProvider:
    """测试用 Token 提供器，只返回固定值且不访问磁盘。"""

    app_id = APP_ID

    def get_token(self, installation_id: int) -> str:
        assert installation_id == INSTALLATION_ID
        return FAKE_TOKEN


def _target(head_sha: str = HEAD_SHA) -> ReviewTarget:
    return ReviewTarget(
        installation_id=INSTALLATION_ID,
        repository_id=42,
        repository="lboverfys/NiuMa",
        pull_request_number=48,
        head_sha=head_sha,
        review_version_key=f"42:48:{head_sha}",
        context_fetched_at=None,
    )


def _pull_request_payload(head_sha: str = HEAD_SHA) -> dict[str, object]:
    return {
        "number": 48,
        "state": "open",
        "draft": False,
        "title": "验证 GitHub 上下文读取",
        "html_url": "https://github.com/lboverfys/NiuMa/pull/48",
        "user": {"login": "contributor"},
        "changed_files": 2,
        "updated_at": "2026-08-24T11:59:00Z",
        "base": {
            "sha": BASE_SHA,
            "ref": "main",
            "repo": {"id": 42, "full_name": "lboverfys/NiuMa"},
        },
        "head": {
            "sha": head_sha,
            "ref": "feature/review-context",
            "repo": {"full_name": "contributor/NiuMa"},
        },
    }


def test_loader_fetches_files_full_diff_and_terminal_ci_in_pages() -> None:
    """验证文件、二进制补丁和两类 CI 状态被一次上下文读取正确归一化。"""

    requested_paths: list[str] = []
    full_diff = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-old_value = 1
+new_value = 2
diff --git a/assets/logo.png b/assets/logo.png
new file mode 100644
index 0000000..3333333
Binary files /dev/null and b/assets/logo.png differ
"""

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        path = request.url.path
        if path == "/repos/lboverfys/NiuMa/pulls/48":
            if request.headers["accept"] == "application/vnd.github.v3.diff":
                return httpx.Response(200, text=full_diff)
            return httpx.Response(200, json=_pull_request_payload())
        if path == "/repos/lboverfys/NiuMa/pulls/48/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "src/app.py",
                        "status": "modified",
                        "sha": "d" * 40,
                        "additions": 1,
                        "deletions": 1,
                        "changes": 2,
                    },
                    {
                        "filename": "assets/logo.png",
                        "status": "added",
                        "sha": "e" * 40,
                        "additions": 0,
                        "deletions": 0,
                        "changes": 0,
                    },
                ],
            )
        if path == f"/repos/lboverfys/NiuMa/commits/{HEAD_SHA}/check-runs":
            return httpx.Response(
                200,
                json={
                    "total_count": 2,
                    "check_runs": [
                        {
                            "id": 100,
                            "name": "OpenReviewer",
                            "status": "completed",
                            "conclusion": "success",
                            "app": {"id": APP_ID},
                        },
                        {
                            "id": 101,
                            "name": "Backend tests",
                            "status": "completed",
                            "conclusion": "success",
                            "app": {"id": 123},
                        },
                    ],
                },
            )
        if path == f"/repos/lboverfys/NiuMa/commits/{HEAD_SHA}/statuses":
            return httpx.Response(
                200,
                json=[
                    {
                        "context": "legacy-ci",
                        "state": "failure",
                    }
                ],
            )
        raise AssertionError(f"未预期的 GitHub 请求：{request.url}")

    api = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
    )
    context = GitHubReviewContextLoader(
        api,
        StaticTokenProvider(),
        clock=lambda: datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
    ).load(_target())

    assert context.files_complete is True
    assert context.diff_complete is True
    assert context.pull_request.author_login == "contributor"
    assert context.pull_request.head_repository == "contributor/NiuMa"
    assert context.pull_request.head_ref == "feature/review-context"
    assert context.pull_request.base_repository == "lboverfys/NiuMa"
    assert context.pull_request.base_ref == "main"
    assert context.files is not None
    assert [item.path for item in context.files] == [
        "src/app.py",
        "assets/logo.png",
    ]
    assert context.files[0].patch_state is PatchState.AVAILABLE
    assert "+new_value = 2" in (context.files[0].patch or "")
    assert context.files[1].patch_state is PatchState.BINARY
    assert context.ci is not None
    assert context.ci.state is CiState.FAILURE
    assert {check.name for check in context.ci.checks} == {
        "Backend tests",
        "legacy-ci",
    }
    assert requested_paths.count("/repos/lboverfys/NiuMa/pulls/48/files") == 1


def test_ci_aggregation_distinguishes_unconfigured_from_incomplete() -> None:
    assert GitHubReviewContextLoader._aggregate_ci((), True) is CiState.NOT_CONFIGURED
    assert GitHubReviewContextLoader._aggregate_ci((), False) is CiState.UNKNOWN


def test_loader_stops_after_pr_metadata_when_head_sha_is_stale() -> None:
    """验证旧任务不会继续下载文件或 CI，从源头阻止旧结果覆盖新提交。"""

    current_head_sha = "f" * 40
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json=_pull_request_payload(current_head_sha))

    api = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
    )
    context = GitHubReviewContextLoader(
        api,
        StaticTokenProvider(),
    ).load(_target())

    assert context.pull_request.head_sha == current_head_sha
    assert context.files is None
    assert context.ci is None
    assert requests == ["/repos/lboverfys/NiuMa/pulls/48"]


def test_identity_loader_fetches_only_pull_request_metadata() -> None:
    current_head_sha = "f" * 40
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json=_pull_request_payload(current_head_sha))

    api = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
    )

    snapshot = GitHubReviewContextLoader(
        api,
        StaticTokenProvider(),
    ).load_pull_request(_target())

    assert snapshot.head_sha == current_head_sha
    assert snapshot.author_login == "contributor"
    assert snapshot.head_repository == "contributor/NiuMa"
    assert snapshot.head_ref == "feature/review-context"
    assert snapshot.base_repository == "lboverfys/NiuMa"
    assert snapshot.base_ref == "main"
    assert requests == ["/repos/lboverfys/NiuMa/pulls/48"]


def test_loader_keeps_pr_metadata_when_author_and_source_fork_are_deleted() -> None:
    current_head_sha = "f" * 40
    payload = _pull_request_payload(current_head_sha)
    payload["user"] = None
    payload["head"] = {
        **payload["head"],
        "repo": None,
    }

    api = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=payload)
            ),
        ),
    )

    context = GitHubReviewContextLoader(
        api,
        StaticTokenProvider(),
    ).load(_target())

    assert context.pull_request.author_login is None
    assert context.pull_request.head_repository is None
    assert context.pull_request.head_ref == "feature/review-context"
    assert context.pull_request.base_repository == "lboverfys/NiuMa"
    assert context.files is None
    assert context.ci is None


def test_diff_larger_than_legacy_limit_reaches_review_planner() -> None:
    large_line = "x" * (600 * 1024)
    full_diff = (
        "diff --git a/src/large.py b/src/large.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/large.py\n"
        "+++ b/src/large.py\n"
        "@@ -0,0 +1 @@\n"
        f"+{large_line}\n"
    )
    pull_request = {**_pull_request_payload(), "changed_files": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/lboverfys/NiuMa/pulls/48":
            if request.headers["accept"] == "application/vnd.github.v3.diff":
                return httpx.Response(200, text=full_diff)
            return httpx.Response(200, json=pull_request)
        if path == "/repos/lboverfys/NiuMa/pulls/48/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "src/large.py",
                        "status": "added",
                        "sha": "d" * 40,
                        "additions": 1,
                        "deletions": 0,
                        "changes": 1,
                    }
                ],
            )
        if path == f"/repos/lboverfys/NiuMa/commits/{HEAD_SHA}/check-runs":
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        if path == f"/repos/lboverfys/NiuMa/commits/{HEAD_SHA}/statuses":
            return httpx.Response(200, json=[])
        raise AssertionError(f"未预期的 GitHub 请求：{request.url}")

    api = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
    )
    context = GitHubReviewContextLoader(api, StaticTokenProvider()).load(_target())

    assert context.files is not None
    assert context.files[0].patch_state is PatchState.AVAILABLE
    assert len((context.files[0].patch or "").encode("utf-8")) > 512 * 1024
    plan = DeterministicReviewPlanner().plan(
        _target(),
        context.files,
        RepositoryRulesSnapshot(
            repository_id=42,
            repository="lboverfys/NiuMa",
            head_sha=HEAD_SHA,
            rules=(),
            incomplete_files=(),
            issues=(),
            candidate_count=0,
            requested_candidate_count=0,
        ),
    )
    assert len(plan.units) == 1
    assert plan.units[0].patch == context.files[0].patch


def test_loader_rebuilds_missing_patches_with_two_batched_blob_queries() -> None:
    """完整 diff 不可用时，文本和二进制文件仍通过固定批量请求准确恢复。"""

    graphql_bodies: list[dict[str, object]] = []
    base_text = "value = 1\n"
    head_text = "value = 2\n"

    def blob(
        oid: str,
        text: str | None,
        *,
        binary: bool = False,
        include_text: bool = True,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "__typename": "Blob",
            "oid": oid,
            "byteSize": 4 if binary else len((text or "").encode("utf-8")),
            "isBinary": binary,
        }
        if text is not None and include_text:
            result["text"] = text
        return result

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/lboverfys/NiuMa/pulls/48":
            if request.headers["accept"] == "application/vnd.github.v3.diff":
                return httpx.Response(406, json={"message": "diff unavailable"})
            return httpx.Response(200, json=_pull_request_payload())
        if path == "/repos/lboverfys/NiuMa/pulls/48/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "src/app.py",
                        "status": "modified",
                        "sha": "d" * 40,
                        "additions": 1,
                        "deletions": 1,
                        "changes": 2,
                    },
                    {
                        "filename": "assets/logo.png",
                        "status": "added",
                        "sha": "e" * 40,
                        "additions": 0,
                        "deletions": 0,
                        "changes": 0,
                    },
                ],
            )
        if path == "/graphql":
            body = json.loads(request.content)
            assert isinstance(body, dict)
            graphql_bodies.append(body)
            query = body["query"]
            assert isinstance(query, str)
            if "isBinary text" not in query:
                repository = {
                    "databaseId": 42,
                    "nameWithOwner": "lboverfys/NiuMa",
                    "base0": blob("1" * 40, base_text, include_text=False),
                    "head0": blob("2" * 40, head_text, include_text=False),
                    "head1": blob("3" * 40, None, binary=True),
                }
            else:
                repository = {
                    "databaseId": 42,
                    "nameWithOwner": "lboverfys/NiuMa",
                    "base0": blob("1" * 40, base_text),
                    "head0": blob("2" * 40, head_text),
                }
            return httpx.Response(200, json={"data": {"repository": repository}})
        if path == f"/repos/lboverfys/NiuMa/commits/{HEAD_SHA}/check-runs":
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        if path == f"/repos/lboverfys/NiuMa/commits/{HEAD_SHA}/statuses":
            return httpx.Response(200, json=[])
        raise AssertionError(f"未预期的 GitHub 请求：{request.url}")

    api = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
    )
    context = GitHubReviewContextLoader(api, StaticTokenProvider()).load(_target())

    assert context.files_complete is True
    assert context.diff_complete is True
    assert context.files is not None
    assert context.files[0].patch_state is PatchState.AVAILABLE
    assert "-value = 1" in (context.files[0].patch or "")
    assert "+value = 2" in (context.files[0].patch or "")
    assert context.files[1].patch_state is PatchState.BINARY
    assert len(graphql_bodies) == 2
    metadata_variables = graphql_bodies[0]["variables"]
    assert isinstance(metadata_variables, dict)
    assert set(metadata_variables.values()) >= {
        f"{BASE_SHA}:src/app.py",
        f"{HEAD_SHA}:src/app.py",
        f"{HEAD_SHA}:assets/logo.png",
    }


def test_blob_fallback_marks_oversized_content_without_downloading_it() -> None:
    """Blob 元数据超限时不读取正文，并明确保留不完整状态。"""

    graphql_requests = 0
    pull_request = {**_pull_request_payload(), "changed_files": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal graphql_requests
        path = request.url.path
        if path == "/repos/lboverfys/NiuMa/pulls/48":
            if request.headers["accept"] == "application/vnd.github.v3.diff":
                return httpx.Response(422, json={"message": "diff too large"})
            return httpx.Response(200, json=pull_request)
        if path == "/repos/lboverfys/NiuMa/pulls/48/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "src/large.py",
                        "status": "added",
                        "sha": "d" * 40,
                        "additions": 1,
                        "deletions": 0,
                        "changes": 1,
                    }
                ],
            )
        if path == "/graphql":
            graphql_requests += 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "databaseId": 42,
                            "nameWithOwner": "lboverfys/NiuMa",
                            "head0": {
                                "__typename": "Blob",
                                "oid": "4" * 40,
                                "byteSize": 2048,
                                "isBinary": False,
                            },
                        }
                    }
                },
            )
        if path == f"/repos/lboverfys/NiuMa/commits/{HEAD_SHA}/check-runs":
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        if path == f"/repos/lboverfys/NiuMa/commits/{HEAD_SHA}/statuses":
            return httpx.Response(200, json=[])
        raise AssertionError(f"未预期的 GitHub 请求：{request.url}")

    api = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
    )
    context = GitHubReviewContextLoader(
        api,
        StaticTokenProvider(),
        GitHubContextSettings(
            max_blob_bytes=1024,
            max_blob_fallback_total_bytes=2048,
        ),
    ).load(_target())

    assert context.diff_complete is False
    assert context.files is not None
    assert context.files[0].patch_state is PatchState.TOO_LARGE
    assert graphql_requests == 1


def test_loader_enforces_total_time_budget_before_starting_next_request() -> None:
    """验证慢分页会在租约到期前转换成可重试超时，不继续扩大外部请求。"""

    requests: list[str] = []
    ticks = iter((0.0, 31.0))
    api = GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(
                lambda request: (
                    requests.append(request.url.path),
                    httpx.Response(200, json=_pull_request_payload()),
                )[1]
            ),
        ),
    )
    loader = GitHubReviewContextLoader(
        api,
        StaticTokenProvider(),
        GitHubContextSettings(max_load_seconds=30),
        monotonic=lambda: next(ticks),
    )

    try:
        loader.load(_target())
    except SafeApplicationError as error:
        assert error.error.code is ErrorCode.GITHUB_TIMEOUT
        assert error.error.retryable is True
    else:
        raise AssertionError("超过总时间预算时必须停止读取")
    assert requests == []
