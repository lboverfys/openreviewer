"""按确定提交读取 GitHub 代码树，并批量读取 Blob。"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

from domain.identifiers import normalize_sha
from domain.paths import normalize_repository_path
from domain.retrieval import MAX_INDEX_FILES, MAX_SOURCE_BYTES, SourceFile
from services.code_indexing import git_blob_sha
from services.github import GitHubApiClient
from services.github_context import InstallationTokenProvider
from services.retrieval_providers import RetrievalError

_EXCLUDED = {".git", "node_modules", "target", "build", "dist", "vendor", ".venv", "generated", "__pycache__"}
_SUFFIXES = {".java", ".xml", ".md"}
_MAX_TOTAL_BYTES = 32 * 1024 * 1024


class GitHubCodeSourceLoader:
    def __init__(self, api: GitHubApiClient, tokens: InstallationTokenProvider) -> None:
        self.api, self.tokens = api, tokens

    def __call__(self, target: dict[str, Any], heartbeat: Callable[[], None]) -> Sequence[SourceFile]:
        repository = target["repository"]
        parts = repository.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("仓库名称无效")
        owner, name = parts
        head_sha = normalize_sha(target["head_sha"])
        token = self.tokens.get_token(int(target["installation_id"]))
        prefix = "/repos/" + quote(owner, safe="") + "/" + quote(name, safe="")
        identity = self.api.request_json("GET", prefix, bearer_token=token, max_response_bytes=512 * 1024).payload
        if not isinstance(identity, dict) or identity.get("id") != target["repository_id"] or str(identity.get("full_name", "")).casefold() != repository.casefold():
            raise RetrievalError("GitHub 仓库身份与索引目标不一致")
        heartbeat()
        commit = self.api.request_json("GET", f"{prefix}/git/commits/{head_sha}", bearer_token=token, max_response_bytes=512 * 1024).payload
        heartbeat()
        if not isinstance(commit, dict) or commit.get("sha") != head_sha or not isinstance(commit.get("tree"), dict):
            raise RetrievalError("GitHub 提交身份无法确认")
        tree_sha = normalize_sha(commit["tree"]["sha"])
        tree = self.api.request_json("GET", f"{prefix}/git/trees/{tree_sha}", bearer_token=token, params={"recursive": 1}, max_response_bytes=8 * 1024 * 1024).payload
        heartbeat()
        if not isinstance(tree, dict) or tree.get("sha") != tree_sha or tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
            raise RetrievalError("GitHub 代码树不完整，不能建立完整索引")
        files: list[tuple[str, str]] = []
        total_bytes = 0
        for item in tree["tree"]:
            if not isinstance(item, dict) or item.get("type") != "blob" or item.get("mode") == "120000":
                continue
            path = normalize_repository_path(item.get("path", ""))
            pure = PurePosixPath(path)
            if pure.suffix.casefold() not in _SUFFIXES or _EXCLUDED.intersection(pure.parts):
                continue
            size = item.get("size")
            if not isinstance(size, int) or size > MAX_SOURCE_BYTES:
                raise RetrievalError("可审查源文件超过索引大小上限")
            total_bytes += size
            files.append((path, normalize_sha(item["sha"])))
            if len(files) > MAX_INDEX_FILES or total_bytes > _MAX_TOTAL_BYTES:
                raise RetrievalError("仓库超过当前索引容量上限")
        sources: list[SourceFile] = []
        actual_total = 0
        # Each request fetches up to 20 blobs, never one HTTP call per file.
        for offset in range(0, len(files), 20):
            heartbeat()
            batch = files[offset:offset + 20]
            fields = " ".join(
                f"b{index}:object(oid:{json.dumps(sha)}){{... on Blob{{oid text byteSize isBinary}}}}"
                for index, (_, sha) in enumerate(batch)
            )
            query = f"query{{repository(owner:{json.dumps(owner)},name:{json.dumps(name)}){{{fields}}}}}"
            result = self.api.request_json("POST", "/graphql", bearer_token=token, json_body={"query": query}, max_response_bytes=8 * 1024 * 1024).payload
            if not isinstance(result, dict) or result.get("errors"):
                raise RetrievalError("GitHub 批量读取代码失败")
            data = result.get("data")
            repo = data.get("repository") if isinstance(data, dict) else None
            if not isinstance(repo, dict):
                raise RetrievalError("GitHub 批量代码响应不完整")
            for index, (path, sha) in enumerate(batch):
                blob = repo.get(f"b{index}")
                if not isinstance(blob, dict) or blob.get("oid") != sha or blob.get("isBinary") is True or not isinstance(blob.get("text"), str):
                    raise RetrievalError("GitHub 源码 Blob 无法确认")
                content = blob["text"]
                actual_total += len(content.encode())
                if actual_total > _MAX_TOTAL_BYTES:
                    raise RetrievalError("实际源码总量超过索引上限")
                if len(content.encode()) > MAX_SOURCE_BYTES or git_blob_sha(content) != sha:
                    raise RetrievalError("GitHub 源码内容与 Blob SHA 不一致")
                sources.append(SourceFile(file=path, blob_sha=sha, content=content))
        return tuple(sources)
