import httpx
import pytest

from services.code_indexing import git_blob_sha
from services.github import GitHubApiClient
from services.github_code_sources import GitHubCodeSourceLoader
from services.retrieval_providers import RetrievalError


class Tokens:
    def get_token(self, installation_id):
        assert installation_id == 10
        return "test-token"


def test_github_snapshot_uses_one_blob_batch_for_multiple_files():
    content = ["class A {}", "class B {}"]
    shas = [git_blob_sha(value) for value in content]
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == "/repos/owner/repo":
            return httpx.Response(200, json={"id": 42, "full_name": "owner/repo"})
        if "/git/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": "a" * 40, "tree": {"sha": "b" * 40}})
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json={"sha": "b" * 40, "truncated": False, "tree": [
                {"path": f"{name}.java", "sha": sha, "type": "blob", "mode": "100644", "size": len(value)}
                for name, sha, value in zip(("A", "B"), shas, content, strict=True)
            ]})
        assert request.url.path == "/graphql"
        return httpx.Response(200, json={"data": {"repository": {
            f"b{i}": {"oid": sha, "text": value, "byteSize": len(value), "isBinary": False}
            for i, (sha, value) in enumerate(zip(shas, content, strict=True))
        }}})
    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    loader = GitHubCodeSourceLoader(GitHubApiClient(client=client), Tokens())
    files = loader({"installation_id": 10, "repository_id": 42, "repository": "owner/repo", "head_sha": "a" * 40}, lambda: None)
    assert len(files) == 2
    assert len([request for request in calls if request.url.path == "/graphql"]) == 1
    assert len(calls) == 4


def test_github_truncated_tree_cannot_become_complete_index():
    def handler(request):
        if request.url.path == "/repos/owner/repo":
            return httpx.Response(200, json={"id": 42, "full_name": "owner/repo"})
        if "/git/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": "a" * 40, "tree": {"sha": "b" * 40}})
        return httpx.Response(200, json={"sha": "b" * 40, "truncated": True, "tree": []})
    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    loader = GitHubCodeSourceLoader(GitHubApiClient(client=client), Tokens())
    with pytest.raises(RetrievalError, match="不完整"):
        loader({"installation_id": 10, "repository_id": 42, "repository": "owner/repo", "head_sha": "a" * 40}, lambda: None)
