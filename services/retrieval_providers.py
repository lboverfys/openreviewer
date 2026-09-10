"""阿里云向量与精排接口；批量输入、固定公网连接和有界响应。"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from domain.retrieval import VECTOR_DIMENSIONS, RetrievalSettings
from domain.security import ErrorCode, SafeApplicationError, SafeError
from services.model_review import normalize_api_base_url
from services.pinned_http import PublicDnsPinnedHTTPTransport


def external_retrieval_paused() -> bool:
    return os.environ.get("OPENREVIEWER_RETRIEVAL_API_DISABLED", "").lower() in {"1", "true", "yes"}


class RetrievalError(SafeApplicationError):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float = 0) -> None:
        super().__init__(SafeError(code=ErrorCode.RETRIEVAL_UNAVAILABLE, safe_message=message, retryable=retryable))
        self.retryable = retryable
        self.retry_after = min(30.0, max(0.0, retry_after))


def normalize_aliyun_host(value: str) -> str:
    normalized = normalize_api_base_url(value)
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    host = parsed.hostname or ""
    allowed = host.endswith(".maas.aliyuncs.com") or host in {
        "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com",
    }
    if not allowed or parsed.port not in {None, 443}:
        raise ValueError("检索模型地址必须是阿里云百炼 HTTPS 接入域名")
    if parsed.path.rstrip("/") not in {"", "/api/v1", "/compatible-mode/v1", "/compatible-api/v1"}:
        raise ValueError("请填写百炼 API Host 或其标准接入地址")
    return f"https://{host}"


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    duration_ms: int
    input_tokens: int | None


@dataclass(frozen=True, slots=True)
class RerankResult:
    ranking: tuple[tuple[int, float], ...]
    duration_ms: int
    input_tokens: int | None


@dataclass(slots=True)
class AliyunRetrievalClient:
    settings: RetrievalSettings
    api_key: str = field(repr=False)
    client: httpx.Client | None = field(default=None, repr=False)
    _owns_client: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not self.api_key or any(char.isspace() for char in self.api_key):
            raise RetrievalError("检索模型密钥尚未配置或格式无效")
        origin = normalize_aliyun_host(self.settings.api_host)
        if not origin:
            raise RetrievalError("检索模型地址尚未配置")
        if self.client is None:
            self.client = httpx.Client(
                transport=PublicDnsPinnedHTTPTransport(),
                timeout=httpx.Timeout(self.settings.timeout_seconds, connect=10),
                follow_redirects=False, trust_env=False,
            )
            self._owns_client = True

    def close(self) -> None:
        if self._owns_client and self.client is not None:
            self.client.close()

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, Any]:
        if self._owns_client and external_retrieval_paused():
            raise RetrievalError("检索模型外部调用已暂停，需管理员明确开启")
        started = time.monotonic()
        for attempt in range(3):
            try:
                return self._post_once(path, payload)
            except RetrievalError as exc:
                if not exc.retryable or attempt == 2 or time.monotonic() - started > 200 - self.settings.timeout_seconds:
                    raise
                time.sleep(max(exc.retry_after, 2 ** attempt))
        raise AssertionError("unreachable retry state")

    def _post_once(self, path: str, payload: dict[str, object]) -> dict[str, Any]:
        assert self.client is not None
        try:
            with self.client.stream(
                "POST", normalize_aliyun_host(self.settings.api_host) + path,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            ) as response:
                if response.status_code != 200:
                    retry_header = response.headers.get("retry-after", "")
                    retry_after = float(retry_header) if retry_header.isascii() and retry_header.isdigit() else 0
                    error_bytes = bytearray()
                    for part in response.iter_bytes():
                        error_bytes.extend(part)
                        if len(error_bytes) > 16_384:
                            break
                    try:
                        error_body = json.loads(error_bytes)
                    except ValueError:
                        error_body = {}
                    error = error_body.get("error", error_body) if isinstance(error_body, dict) else {}
                    code = str(error.get("code", "")) if isinstance(error, dict) else ""
                    code = code if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code) and not code.startswith("sk-") else ""
                    description = f"百炼检索接口返回 HTTP {response.status_code}" + (f"（{code}）" if code else "")
                    raise RetrievalError(
                        description,
                        retryable=response.status_code == 429 or response.status_code >= 500 or code.startswith("Throttling"),
                        retry_after=retry_after,
                    )
                data = bytearray()
                for part in response.iter_bytes():
                    data.extend(part)
                    if len(data) > 4 * 1024 * 1024:
                        raise RetrievalError("百炼检索响应超过允许大小")
                body = json.loads(data)
        except httpx.HTTPError as exc:
            raise RetrievalError("百炼检索连接失败或超时", retryable=True) from exc
        except ValueError as exc:
            raise RetrievalError("百炼检索返回了无效 JSON") from exc
        if not isinstance(body, dict):
            raise RetrievalError("百炼检索返回结构无效")
        return body

    @staticmethod
    def _tokens(body: dict[str, Any]) -> int | None:
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return None
        value = usage.get("input_tokens", usage.get("prompt_tokens", usage.get("total_tokens")))
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
        # The indexer packs bounded batches before calling this method.
        if not 1 <= len(texts) <= 20 or any(not value.strip() for value in texts):
            raise ValueError("向量请求需要 1 到 20 段非空文本")
        if sum(len(value.encode()) for value in texts) > 64_000:
            raise ValueError("单批向量输入超过 64 KB")
        started = time.monotonic()
        body = self._post("/compatible-mode/v1/embeddings", {
            "model": self.settings.embedding_model,
            "input": list(texts), "dimensions": VECTOR_DIMENSIONS,
        })
        rows = body.get("data")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise RetrievalError("向量响应条数与请求不一致")
        vectors: dict[int, tuple[float, ...]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise RetrievalError("向量响应条目无效")
            index, vector = row.get("index"), row.get("embedding")
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(texts) or index in vectors:
                raise RetrievalError("向量响应索引无效或重复")
            if not isinstance(vector, list) or len(vector) != VECTOR_DIMENSIONS:
                raise RetrievalError("向量维度与索引配置不一致")
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in vector):
                raise RetrievalError("向量响应包含无效数值")
            if not any(vector):
                raise RetrievalError("向量响应为零向量")
            vectors[index] = tuple(float(v) for v in vector)
        return EmbeddingResult(tuple(vectors[i] for i in range(len(texts))), round((time.monotonic() - started) * 1000), self._tokens(body))

    def rerank(self, query: str, documents: tuple[str, ...]) -> RerankResult:
        if not query.strip() or not 1 <= len(documents) <= 30:
            raise ValueError("精排请求需要查询及 1 到 30 个候选")
        if sum(len(value.encode()) for value in (query, *documents)) > 180_000:
            raise ValueError("精排请求超过允许大小")
        started = time.monotonic()
        if self.settings.rerank_model == "qwen3-rerank":
            path = "/compatible-api/v1/reranks"
            payload: dict[str, object] = {
                "model": self.settings.rerank_model, "query": query,
                "documents": list(documents), "top_n": len(documents),
            }
        else:
            path = "/api/v1/services/rerank/text-rerank/text-rerank"
            payload = {
                "model": self.settings.rerank_model,
                "input": {"query": query, "documents": list(documents)},
                "parameters": {"top_n": len(documents), "instruct": "Retrieve code and rules that are relevant evidence for reviewing the described code change."},
            }
        body = self._post(path, payload)
        output = body.get("output", body)
        rows = output.get("results") if isinstance(output, dict) else None
        if not isinstance(rows, list) or len(rows) != len(documents):
            raise RetrievalError("精排响应条数与候选不一致")
        ranking: list[tuple[int, float]] = []
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise RetrievalError("精排响应条目无效")
            index, score = row.get("index"), row.get("relevance_score")
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(documents) or index in seen:
                raise RetrievalError("精排响应索引无效或重复")
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
                raise RetrievalError("精排响应分数无效")
            seen.add(index)
            ranking.append((index, float(score)))
        ranking.sort(key=lambda item: (-item[1], item[0]))
        return RerankResult(tuple(ranking), round((time.monotonic() - started) * 1000), self._tokens(body))
