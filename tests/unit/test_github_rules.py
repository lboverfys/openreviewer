import json

import httpx
import pytest

from domain.enums import (
    ChangedFileStatus,
    PatchState,
    RepositoryRuleIssueKind,
)
from domain.github import PullRequestFile
from domain.review_planning import repository_rule_candidate_paths
from domain.security import ErrorCode, SafeApplicationError
from services.github import GitHubApiClient, GitHubClientSettings
from services.github_rules import GitHubRepositoryRuleLoader, GitHubRuleSettings
from services.task_queue import ReviewTarget


INSTALLATION_ID = 156153422
FAKE_TOKEN = "ghs_FAKEINSTALLATIONTOKEN123456789"
HEAD_SHA = "c" * 40


class StaticTokenProvider:
    app_id = 4699977

    def get_token(self, installation_id: int) -> str:
        assert installation_id == INSTALLATION_ID
        return FAKE_TOKEN


def _target() -> ReviewTarget:
    return ReviewTarget(
        installation_id=INSTALLATION_ID,
        repository_id=42,
        repository="lboverfys/NiuMa",
        pull_request_number=48,
        head_sha=HEAD_SHA,
        review_version_key=f"42:48:{HEAD_SHA}",
        context_fetched_at=None,
    )


def _file(path: str) -> PullRequestFile:
    return PullRequestFile(
        path=path,
        status=ChangedFileStatus.MODIFIED,
        blob_sha="d" * 40,
        additions=1,
        deletions=1,
        changes=2,
        patch_state=PatchState.AVAILABLE,
        patch="@@ -1 +1 @@\n-old\n+new\n",
    )


def _blob(content: str, blob_sha: str) -> dict[str, object]:
    return {
        "__typename": "Blob",
        "oid": blob_sha,
        "byteSize": len(content.encode("utf-8")),
        "isBinary": False,
        "text": content,
    }


def _api(handler: httpx.MockTransport) -> GitHubApiClient:
    return GitHubApiClient(
        GitHubClientSettings(api_base_url="https://api.github.test"),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=handler,
        ),
    )


def test_rule_loader_fetches_all_scopes_in_one_graphql_request() -> None:
    requests: list[dict[str, object]] = []
    contents = {
        "AGENTS.md": ("根目录规则\n", "a" * 40),
        "src/AGENTS.md": ("源码规则\n", "b" * 40),
        "src/auth/AGENTS.md": ("鉴权规则\n", "e" * 40),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/graphql"
        assert request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
        body = json.loads(request.content)
        requests.append(body)
        variables = body["variables"]
        repository: dict[str, object] = {
            "databaseId": 42,
            "nameWithOwner": "lboverfys/NiuMa",
        }
        for key, expression in variables.items():
            if not key.startswith("expression"):
                continue
            index = int(key.removeprefix("expression"))
            path = expression.split(":", 1)[1]
            value = contents.get(path)
            repository[f"rule{index}"] = (
                _blob(value[0], value[1]) if value is not None else None
            )
        return httpx.Response(200, json={"data": {"repository": repository}})

    snapshot = GitHubRepositoryRuleLoader(
        _api(httpx.MockTransport(handler)),
        StaticTokenProvider(),
    ).load(
        _target(),
        (_file("src/auth/login.py"), _file("docs/README.md")),
    )

    assert len(requests) == 1
    assert "src/auth/AGENTS.md" not in requests[0]["query"]
    assert set(requests[0]["variables"].values()) >= {
        f"{HEAD_SHA}:AGENTS.md",
        f"{HEAD_SHA}:src/AGENTS.md",
        f"{HEAD_SHA}:src/auth/AGENTS.md",
        f"{HEAD_SHA}:docs/AGENTS.md",
    }
    assert [rule.path for rule in snapshot.rules] == [
        "AGENTS.md",
        "src/AGENTS.md",
        "src/auth/AGENTS.md",
    ]
    assert [rule.scope for rule in snapshot.rules] == [None, "src", "src/auth"]
    assert snapshot.candidate_count == 4
    assert snapshot.requested_candidate_count == 4
    assert snapshot.complete is True


def test_rule_loader_marks_only_files_affected_by_candidate_limit() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        body = json.loads(request.content)
        repository: dict[str, object] = {
            "databaseId": 42,
            "nameWithOwner": "lboverfys/NiuMa",
        }
        for key in body["variables"]:
            if key.startswith("expression"):
                repository[f"rule{key.removeprefix('expression')}"] = None
        return httpx.Response(200, json={"data": {"repository": repository}})

    snapshot = GitHubRepositoryRuleLoader(
        _api(httpx.MockTransport(handler)),
        StaticTokenProvider(),
        GitHubRuleSettings(max_candidate_paths=2),
    ).load(_target(), (_file("a/x.py"), _file("b/y.py")))

    assert request_count == 1
    assert snapshot.candidate_count == 3
    assert snapshot.requested_candidate_count == 2
    assert snapshot.incomplete_files == ("b/y.py",)
    assert snapshot.issues[0].kind is RepositoryRuleIssueKind.CANDIDATE_LIMIT
    assert snapshot.issues[0].affected_file_count == 1


def test_rule_loader_bounds_deep_scope_expansion() -> None:
    requested_expressions: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        variables = body["variables"]
        repository: dict[str, object] = {
            "databaseId": 42,
            "nameWithOwner": "lboverfys/NiuMa",
        }
        for key, expression in variables.items():
            if key.startswith("expression"):
                requested_expressions.add(expression)
                repository[f"rule{key.removeprefix('expression')}"] = None
        return httpx.Response(200, json={"data": {"repository": repository}})

    snapshot = GitHubRepositoryRuleLoader(
        _api(httpx.MockTransport(handler)),
        StaticTokenProvider(),
        GitHubRuleSettings(max_scope_depth=1),
    ).load(_target(), (_file("a/b/c/app.py"),))

    assert requested_expressions == {
        f"{HEAD_SHA}:AGENTS.md",
        f"{HEAD_SHA}:a/AGENTS.md",
    }
    assert snapshot.incomplete_files == ("a/b/c/app.py",)
    assert snapshot.issues[0].kind is RepositoryRuleIssueKind.SCOPE_DEPTH_LIMIT


def test_rule_loader_degrades_binary_and_oversized_rules_by_scope() -> None:
    oversized = "x" * 1025

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        variables = body["variables"]
        repository: dict[str, object] = {
            "databaseId": 42,
            "nameWithOwner": "lboverfys/NiuMa",
        }
        for key, expression in variables.items():
            if not key.startswith("expression"):
                continue
            index = key.removeprefix("expression")
            path = expression.split(":", 1)[1]
            if path == "src/AGENTS.md":
                repository[f"rule{index}"] = _blob(oversized, "a" * 40)
            elif path == "docs/AGENTS.md":
                repository[f"rule{index}"] = {
                    "__typename": "Blob",
                    "oid": "b" * 40,
                    "byteSize": 50,
                    "isBinary": True,
                    "text": None,
                }
            else:
                repository[f"rule{index}"] = None
        return httpx.Response(200, json={"data": {"repository": repository}})

    snapshot = GitHubRepositoryRuleLoader(
        _api(httpx.MockTransport(handler)),
        StaticTokenProvider(),
        GitHubRuleSettings(max_rule_bytes=1024, max_total_rule_bytes=1024),
    ).load(
        _target(),
        (_file("src/app.py"), _file("docs/guide.md"), _file("README.md")),
    )

    assert snapshot.rules == ()
    assert snapshot.incomplete_files == ("docs/guide.md", "src/app.py")
    assert {issue.kind for issue in snapshot.issues} == {
        RepositoryRuleIssueKind.BINARY,
        RepositoryRuleIssueKind.TOO_LARGE,
    }


def test_rule_loader_rejects_graphql_response_missing_requested_alias() -> None:
    api = _api(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "databaseId": 42,
                            "nameWithOwner": "lboverfys/NiuMa",
                        }
                    }
                },
            )
        )
    )

    with pytest.raises(SafeApplicationError) as captured:
        GitHubRepositoryRuleLoader(api, StaticTokenProvider()).load(
            _target(),
            (_file("src/app.py"),),
        )

    assert captured.value.error.code is ErrorCode.GITHUB_INVALID_RESPONSE


def test_rule_loader_degrades_oversized_graphql_response_for_all_files() -> None:
    api = GitHubApiClient(
        GitHubClientSettings(
            api_base_url="https://api.github.test",
            max_response_bytes=1024,
        ),
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"data": {"padding": "x" * 2048}},
                )
            ),
        ),
    )

    snapshot = GitHubRepositoryRuleLoader(
        api,
        StaticTokenProvider(),
        GitHubRuleSettings(max_response_bytes=1024),
    ).load(_target(), (_file("src/app.py"), _file("docs/guide.md")))

    assert snapshot.rules == ()
    assert snapshot.incomplete_files == ("docs/guide.md", "src/app.py")
    assert snapshot.issues[-1].kind is RepositoryRuleIssueKind.RESPONSE_TOO_LARGE


def test_rule_candidates_reject_unsafe_paths() -> None:
    assert repository_rule_candidate_paths("src/auth/login.py") == (
        "AGENTS.md",
        "src/AGENTS.md",
        "src/auth/AGENTS.md",
    )
    with pytest.raises(ValueError):
        repository_rule_candidate_paths("../AGENTS.md")
