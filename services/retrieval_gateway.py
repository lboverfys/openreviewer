"""用现有 PostgreSQL 协调模型请求，先复用缓存再申请全局单并发通道。"""

from hashlib import sha256
from typing import Any

from domain.retrieval import RetrievalSettings, stable_key
from persistence.retrieval import RetrievalRepository
from persistence.retrieval_runtime import RetrievalRuntimeRepository
from services.retrieval_providers import (
    AliyunRetrievalClient,
    EmbeddingResult,
    RequestBudget,
    RerankResult,
)


def provider_lane_key(settings: RetrievalSettings, key: str) -> str:
    return stable_key(settings.api_host, sha256(key.encode()).hexdigest())


class RetrievalGateway:
    def __init__(self, repository: RetrievalRepository, settings: RetrievalSettings,
                 key: str, client: Any, budget: RequestBudget) -> None:
        self.repository = repository
        self.runtime = RetrievalRuntimeRepository(repository.sessions)
        self.settings, self.client, self.budget = settings, client, budget
        self.lane = provider_lane_key(settings, key)
        if isinstance(client, AliyunRetrievalClient):
            client.budget = budget

    def close(self) -> None:
        self.client.close()

    def _count_fixture_call(self) -> None:
        if not isinstance(self.client, AliyunRetrievalClient):
            self.budget.consume()

    def embed(self, texts: tuple[str, ...], *, purpose: str = "code") -> EmbeddingResult:
        from services.egress import check_texts
        check_texts(texts)
        digests = [sha256(text.encode()).hexdigest() for text in texts]
        keys = [stable_key(self.settings.embedding_fingerprint, digest) for digest in digests]
        cached = self.runtime.vectors(keys)
        if len(cached) == len(set(keys)):
            return EmbeddingResult(tuple(cached[key] for key in keys), 0, None)
        with self.runtime.model_lane(self.lane):
            # 申请通道后再次读取，避免两个进程同时未命中导致重复生成。
            cached = self.runtime.vectors(keys)
            missing = {key: (digest, text) for key, digest, text in zip(keys, digests, texts, strict=True) if key not in cached}
            duration, tokens = 0, None
            if missing:
                self._count_fixture_call()
                result = self.client.embed(tuple(text for _, text in missing.values()))
                vectors = dict(zip(missing, result.vectors, strict=True))
                self.repository.store_embeddings(self.settings.embedding_fingerprint, [
                    (key, missing[key][0], vector) for key, vector in vectors.items()
                ], purpose=purpose)
                cached.update(vectors)
                duration, tokens = result.duration_ms, result.input_tokens
            return EmbeddingResult(tuple(cached[key] for key in keys), duration, tokens)

    def rerank(self, query: str, texts: tuple[str, ...]) -> RerankResult:
        from services.egress import check_texts
        check_texts((query, *texts))
        key = stable_key(self.lane, self.settings.rerank_model, query, texts)
        cached = self.runtime.rerank(key)
        if cached is not None:
            return RerankResult(cached, 0, None, cache_hit=True)
        with self.runtime.model_lane(self.lane):
            cached = self.runtime.rerank(key)
            if cached is not None:
                return RerankResult(cached, 0, None, cache_hit=True)
            self._count_fixture_call()
            result = self.client.rerank(query, texts)
            self.runtime.cache_rerank(key, result.ranking)
            return result

    def prefetch_queries(self, texts: tuple[str, ...]) -> tuple[int, int | None]:
        # 复用现有向量缓存与请求额度；四条一批兼容查询的UTF-8字节上限。
        unique = tuple(dict.fromkeys(texts))
        duration = 0
        tokens: int | None = None
        for offset in range(0, len(unique), 4):
            result = self.embed(unique[offset:offset + 4], purpose="query")
            duration += result.duration_ms
            if result.input_tokens is not None:
                tokens = (tokens or 0) + result.input_tokens
        return duration, tokens
