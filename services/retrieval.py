"""混合检索编排：冻结配置、批量索引、RRF、精排和可复现评测。"""

from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ReviewAgent
from domain.retrieval import (
    AnnotationSource,
    ContextEvidence,
    IndexView,
    RetrievalEvaluationCase,
    RetrievalEvaluationReport,
    RetrievalRoute,
    RetrievalSettings,
    RetrievalSettingsView,
    RetrievalStrategy,
    RetrievalTrace,
    RouteMetric,
    SearchQuery,
    SourceFile,
    StrategyEvaluation,
    stable_key,
)
from domain.security import ErrorCode, SafeApplicationError, SafeError
from persistence.models import RetrievalSettingsRecord
from persistence.retrieval import RetrievalRepository
from persistence.retrieval_runtime import RetrievalRuntimeRepository
from services.ai_settings import AiSecretCipher
from services.rbac import ResourceScope
from services.retrieval_context import changed_symbols, merge_contexts, review_queries
from services.retrieval_gateway import RetrievalGateway
from services.retrieval_indexing import build_index
from services.retrieval_indexing import embedding_batches as _embedding_batches
from services.retrieval_lexical import LexicalIndex, LexicalSnapshotCache
from services.retrieval_providers import (
    AliyunRetrievalClient,
    RequestBudget,
    RetrievalError,
    external_retrieval_paused,
    normalize_aliyun_host,
)

_CIPHER_SCOPE = "retrieval_aliyun"
__all__ = ["HybridRetrievalService", "LexicalIndex", "RetrievalSettingsService", "reciprocal_rank_fusion", "_embedding_batches"]
_RRF_K = 60
_FUSION_LIMIT = 30


class RetrievalSettingsService:
    def __init__(self, sessions: sessionmaker[Session], cipher: AiSecretCipher) -> None:
        self.sessions, self.cipher = sessions, cipher

    def runtime(self) -> tuple[RetrievalSettingsView, str | None]:
        with self.sessions() as session:
            row = session.get(RetrievalSettingsRecord, 1)
            if row is None:
                return RetrievalSettingsView(revision=0, settings=RetrievalSettings(), key_configured=False, external_calls_paused=external_retrieval_paused()), None
            settings = RetrievalSettings.model_validate(row.settings)
            key = None
            if row.ciphertext is not None and row.nonce is not None and row.key_version is not None:
                key = self.cipher.decrypt(_CIPHER_SCOPE, row.ciphertext, row.nonce, row.key_version)
            return RetrievalSettingsView(
                revision=row.revision, settings=settings, key_configured=key is not None,
                external_calls_paused=external_retrieval_paused(),
                tested=row.tested_fingerprint == stable_key(row.revision, settings.model_dump()),
            ), key

    def get(self) -> RetrievalSettingsView:
        return self.runtime()[0]

    def update(self, settings: RetrievalSettings, expected_revision: int, actor: str, api_key: str | None = None) -> RetrievalSettingsView:
        settings = settings.model_copy(update={"api_host": normalize_aliyun_host(settings.api_host)})
        encrypted = self.cipher.encrypt(_CIPHER_SCOPE, api_key) if api_key is not None else None
        try:
            with self.sessions() as session, session.begin():
                row = session.scalar(select(RetrievalSettingsRecord).where(RetrievalSettingsRecord.id == 1).with_for_update())
                revision = row.revision if row is not None else 0
                if revision != expected_revision:
                    raise RetrievalError("检索配置已变化，请刷新后重试")
                if row is None:
                    row = RetrievalSettingsRecord(id=1, revision=0, settings={}, updated_by=actor)
                    session.add(row)
                previous = RetrievalSettings.model_validate(row.settings)
                if row.ciphertext is not None and previous.api_host != settings.api_host and encrypted is None:
                    raise ValueError("切换百炼地址时必须同时提供新的 API Key")
                if encrypted is not None:
                    row.ciphertext, row.nonce, row.key_version = encrypted.ciphertext, encrypted.nonce, encrypted.key_version
                row.settings, row.revision = settings.model_dump(mode="json"), revision + 1
                row.updated_by, row.updated_at = actor, datetime.now(UTC)
                row.tested_fingerprint = None
        except IntegrityError as exc:
            raise RetrievalError("检索配置已变化，请刷新后重试") from exc
        return self.get()

    def mark_tested(self, view: RetrievalSettingsView) -> None:
        with self.sessions() as session, session.begin():
            row = session.scalar(select(RetrievalSettingsRecord).where(RetrievalSettingsRecord.id == 1).with_for_update())
            if row is None or row.revision != view.revision:
                raise RetrievalError("连接测试期间配置已变化")
            row.tested_fingerprint = stable_key(view.revision, view.settings.model_dump())


def reciprocal_rank_fusion(routes: dict[str, list[tuple[str, float]]], limit: int = _FUSION_LIMIT) -> list[tuple[str, float, tuple[str, ...]]]:
    scores: dict[str, float] = defaultdict(float)
    origins: dict[str, list[str]] = defaultdict(list)
    for route, hits in routes.items():
        seen: set[str] = set()
        for rank, (chunk_id, _) in enumerate(hits, 1):
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            scores[chunk_id] += 1 / (_RRF_K + rank)
            origins[chunk_id].append(route)
    return [(key, score, tuple(origins[key])) for key, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]]


class HybridRetrievalService:
    def __init__(
        self, repository: RetrievalRepository, settings: RetrievalSettingsService, *,
        client_factory: Callable[[RetrievalSettings, str], Any] = AliyunRetrievalClient,
        source_loader: Callable[[dict[str, Any], Callable[[], None]], Sequence[SourceFile]] | None = None,
    ) -> None:
        self.repository, self.settings = repository, settings
        self.client_factory, self.source_loader = client_factory, source_loader
        self._lexical_cache = LexicalSnapshotCache()

    def _client(self, view: RetrievalSettingsView, key: str | None, budget: RequestBudget | None = None):
        if not key:
            raise RetrievalError("检索模型密钥尚未配置")
        return RetrievalGateway(self.repository, view.settings, key, self.client_factory(view.settings, key),
            budget or RequestBudget(view.settings.max_requests_per_operation))

    def test_connection(self) -> RetrievalSettingsView:
        view, key = self.settings.runtime()
        client = self._client(view, key)
        try:
            client.embed(("database transaction", "format a date"))
            client.rerank("database transaction", ("commit an order transaction", "format a date"))
        finally:
            client.close()
        self.settings.mark_tested(view)
        return self.settings.get()

    def enqueue(self, target: dict[str, Any], *, include_vectors: bool = False) -> str:
        view = self.settings.get()
        if include_vectors and view.external_calls_paused:
            raise RetrievalError("检索模型外部调用已暂停，不能补全向量")
        if include_vectors and not view.key_configured:
            raise RetrievalError("混合检索尚未启用或配置不完整")
        return self.repository.enqueue({**target, "include_vectors": include_vectors}, view.settings)

    def retry_index(self, index_id: str, scope: ResourceScope | None, *, include_vectors: bool = False) -> None:
        if include_vectors and self.settings.get().external_calls_paused:
            raise RetrievalError("检索模型外部调用已暂停，不能补全向量")
        self.repository.retry(index_id, scope, include_vectors=include_vectors)

    def index_sources(self, target: dict[str, Any], sources: Sequence[SourceFile], on_progress: Callable[[], None] | None = None, *, include_vectors: bool = True) -> IndexView:
        index_id = self.enqueue(target, include_vectors=include_vectors)
        existing = self.repository.get(index_id)
        if existing.status == "ready":
            if not include_vectors or existing.vector_status == "ready":
                return existing
            self.repository.retry(index_id, None, include_vectors=True)
        claim = self.repository.claim(index_id)
        if claim is None:
            raise RetrievalError("该提交的索引正在构建", retryable=True)
        return self._build(claim, sources, on_progress)

    def process_next(self, on_progress: Callable[[], None] | None = None) -> bool:
        if self.source_loader is None:
            return False
        claim = self.repository.claim()
        if claim is None:
            return False
        self._build(claim, None, on_progress)
        return True

    def _build(self, claim, sources: Sequence[SourceFile] | None, on_progress: Callable[[], None] | None) -> IndexView:
        return build_index(self.repository, self.settings, self._client, self.source_loader, claim, sources, on_progress)
    def _lexical(self, index_id: str) -> LexicalIndex:
        return self._lexical_cache.get(index_id, lambda: LexicalIndex(self.repository.lexical_documents(index_id)))
    def search(
        self, index_id: str, query: SearchQuery, scope: ResourceScope | None = None, *,
        review_run_id: str | None = None, agent: str | None = None,
        plan_fingerprint: str | None = None, on_progress: Callable[[], None] | None = None,
    ) -> RetrievalTrace:
        index = self.repository.get(index_id, scope)
        view, key = self.settings.runtime()
        client = self._client(view, key) if query.strategy not in {"bm25", "lexical_relations"} and key and not view.external_calls_paused else None
        try:
            trace = self._search(index, query, view.settings, client)
            if client is not None:
                trace = trace.model_copy(update={"model_requests": client.budget.used})
            if on_progress:
                on_progress()
            trace = trace.model_copy(update={"agent": ReviewAgent(agent) if agent else None, "plan_fingerprint": plan_fingerprint})
            # 工作台的临时搜索直接返回；只有审查上下文需要持久化证据快照。
            return self.repository.save_trace(trace, review_run_id, agent) if review_run_id is not None else trace
        finally:
            if client is not None:
                client.close()

    def _search(self, index: IndexView, query: SearchQuery, settings: RetrievalSettings, client) -> RetrievalTrace:
        if not index.lexical_ready:
            raise RetrievalError("索引尚未就绪", retryable=True)
        expected_id = stable_key(index.installation_id, index.repository_id, index.head_sha, settings.embedding_fingerprint)
        if query.strategy not in {"bm25", "lexical_relations"} and index.id != expected_id:
            raise RetrievalError("索引使用的向量模型配置与当前配置不同，请重建索引")
        started = time.monotonic()
        lexical = self._lexical(index.id)
        begin = time.monotonic()
        routes = {"bm25": lexical.search(query.query, settings.candidate_k)}
        metrics = [RouteMetric(route="bm25", candidate_count=len(routes["bm25"]), duration_ms=round((time.monotonic() - begin) * 1000))]
        embedding_ms = rerank_ms = 0
        input_tokens = rerank_tokens = None
        cache_hit = False
        vector_search_mode = "unused"
        warnings: list[str] = []
        if query.strategy not in {"bm25", "lexical_relations"} and client is not None and index.vector_count:
            begin = time.monotonic()
            digest = sha256(query.query.encode()).hexdigest()
            vector_key = stable_key(settings.embedding_fingerprint, digest)
            vector = self.repository.cached_vector(vector_key)
            cache_hit = vector is not None
            try:
                if vector is None:
                    response = client.embed((query.query,), purpose="query")
                    vector, embedding_ms, input_tokens = response.vectors[0], response.duration_ms, response.input_tokens
                vector_result = self.repository.vector_search_details(index.id, vector, settings.candidate_k)
                routes["vector"] = list(vector_result.hits)
                vector_search_mode = vector_result.mode
                metrics.append(RouteMetric(route="vector", candidate_count=len(routes["vector"]), duration_ms=round((time.monotonic() - begin) * 1000)))
                if index.vector_count < index.chunk_count:
                    warnings.append(f"向量覆盖 {index.vector_count}/{index.chunk_count} 个代码块，关键词与关系召回覆盖完整基础索引")
            except RetrievalError as exc:
                warnings.append(str(exc))
        elif query.strategy not in {"bm25", "lexical_relations"}:
            warnings.append("向量服务暂停或尚未就绪，本次使用基础检索")
        if query.strategy in {"lexical_relations", "hybrid_relations", "reranked"}:
            begin = time.monotonic()
            seeds = lexical.seeds(query)
            routes["relation"] = self.repository.relation_search(index.id, seeds, settings.candidate_k)
            metrics.append(RouteMetric(route="relation", candidate_count=len(routes["relation"]), duration_ms=round((time.monotonic() - begin) * 1000)))
        fused = reciprocal_rank_fusion(routes)
        chunks = self.repository.chunks(index.id, [key for key, _, _ in fused])
        fused = [item for item in fused if item[0] in chunks]
        order = list(range(len(fused)))
        scores: dict[int, float] = {}
        rerank_cache_hit = False
        if query.strategy == "reranked" and fused and client is not None:
            # Keep query repetition and document bodies below the provider limit.
            per_document = min(6000, max(0, 110_000 // len(fused) - len(query.query.encode())))
            texts = tuple(chunks[item[0]].embedding_text.encode()[:per_document].decode("utf-8", errors="ignore") for item in fused)
            total_bytes = sum(len(text.encode()) for text in texts) + len(query.query.encode()) * len(texts)
            if total_bytes <= 110_000 and per_document >= 200:
                try:
                    result = client.rerank(query.query, texts)
                    order = [number for number, _ in result.ranking]
                    scores = dict(result.ranking)
                    rerank_ms, rerank_tokens, rerank_cache_hit = result.duration_ms, result.input_tokens, result.cache_hit
                except RetrievalError as exc:
                    warnings.append("精排未完成，使用 RRF 结果：" + str(exc))
            else:
                warnings.append("精排输入超过本批上限，本次使用 RRF 排名")
        route_details = {route: {chunk_id: (rank, score) for rank, (chunk_id, score) in enumerate(hits, 1)} for route, hits in routes.items()}
        candidates = []
        selected_bytes = selected_count = 0
        for rank, position in enumerate(order, 1):
            chunk_id, score, origins = fused[position]
            chunk = chunks[chunk_id]
            selected = selected_count < query.limit and selected_bytes + len(chunk.content.encode()) <= 24_000
            if selected:
                selected_count += 1
                selected_bytes += len(chunk.content.encode())
            candidates.append(ContextEvidence(
                reference_id=stable_key(index.id, chunk.id), chunk_id=chunk.id,
                index_id=index.id, head_sha=index.head_sha, file=chunk.file,
                blob_sha=chunk.blob_sha, symbol=chunk.symbol, start_line=chunk.start_line,
                end_line=chunk.end_line, content=chunk.content, content_hash=chunk.content_hash,
                routes=cast(tuple[RetrievalRoute, ...], origins),
                route_scores={route: route_details[route][chunk_id][1] for route in origins},
                route_ranks={route: route_details[route][chunk_id][0] for route in origins}, rank=rank, fused_rank=position + 1, fusion_score=score,
                rerank_score=scores.get(position), selected=selected,
            ))
        if index.parse_error_files:
            warnings.append("部分文件存在语法解析错误，请结合原始代码核对")
        actual: RetrievalStrategy = "reranked" if scores else "hybrid_relations" if "vector" in routes and "relation" in routes else "hybrid" if "vector" in routes else "lexical_relations" if "relation" in routes else "bm25"
        return RetrievalTrace(
            id=str(uuid4()), index_id=index.id, query=query.query, strategy=actual, requested_strategy=query.strategy,
            rerank_cache_hit=rerank_cache_hit,
            vector_search_mode=vector_search_mode,
            candidates=tuple(candidates), routes=tuple(metrics),
            duration_ms=round((time.monotonic() - started) * 1000),
            embedding_ms=embedding_ms, rerank_ms=rerank_ms, input_tokens=input_tokens,
            rerank_tokens=rerank_tokens, query_cache_hit=cache_hit, warnings=tuple(warnings),
        )


    def review_context(self, model_input, on_progress: Callable[[], None], *,
                       frozen_runtime: tuple[RetrievalSettingsView, str | None] | None = None):
        view = frozen_runtime[0] if frozen_runtime is not None else self.settings.get()
        if not model_input.units:
            return model_input
        target = self.repository.target_for_review(model_input.review_run_id, None)
        if target["head_sha"] != model_input.head_sha:
            raise RetrievalError("审查与索引提交版本不一致")
        cached = self.repository.review_contexts(model_input.review_run_id, model_input.plan_fingerprint)
        required_agents = {agent.value for agent in (ReviewAgent.SECURITY, ReviewAgent.CONVENTION, ReviewAgent.LOGIC) if any(agent in unit.review_domains for unit in model_input.units)}
        if required_agents <= cached.keys():
            stored_contexts = tuple(item.model_copy(update={"agent": ReviewAgent(agent)}) for agent in ("security", "convention", "logic") if agent in required_agents for item in cached[agent].selected)
            if any(item.head_sha != model_input.head_sha for item in stored_contexts):
                raise RetrievalError("已保存检索上下文与当前提交不一致")
            on_progress()
            return model_input.model_copy(update={"context_evidence": stored_contexts})
        if not view.settings.enabled:
            if cached:
                raise RetrievalError("已有部分检索上下文，请恢复检索配置后重试")
            return model_input
        index_id = self.repository.enqueue({**target, "include_vectors": False}, view.settings)
        index = self.repository.get(index_id)
        if index.status == "failed" and not index.lexical_ready:
            raise RetrievalError("代码索引构建失败，请先重试索引")
        if not index.lexical_ready:
            raise SafeApplicationError(SafeError(
                    code=ErrorCode.RETRIEVAL_INDEX_PENDING,
                    safe_message="等待同一提交的代码索引完成",
                    retryable=True,
                    details={"batch_retry_managed": True, "retry_at": (datetime.now(UTC) + timedelta(seconds=20)).isoformat()},
            ))
        purposes = (ReviewAgent.SECURITY, ReviewAgent.CONVENTION, ReviewAgent.LOGIC)
        contexts: list[ContextEvidence] = []
        governance = RetrievalRuntimeRepository(self.repository.sessions)
        budget_key = stable_key(model_input.review_run_id, model_input.plan_fingerprint)
        budget = RequestBudget(view.settings.max_requests_per_operation, charge=lambda: governance.charge_request(budget_key, view.settings.max_requests_per_operation))
        runtime, key = frozen_runtime if frozen_runtime is not None else self.settings.runtime()
        client = self._client(runtime, key, budget) if key and not runtime.external_calls_paused else None
        units_by_agent = {agent: tuple(unit for unit in model_input.units if agent in unit.review_domains)
                          for agent in purposes}
        grouped_by_agent = {agent: review_queries(units, view.settings.strategy, view.settings.context_k)
                            for agent, units in units_by_agent.items() if units and agent.value not in cached}
        traces_by_agent: dict[ReviewAgent, list[tuple[RetrievalTrace, tuple[str, ...]]]] = defaultdict(list)
        calls_by_agent: dict[ReviewAgent, int] = defaultdict(int)
        prefetch_ms = 0
        prefetch_tokens: int | None = None
        try:
            # 先生成有界查询计划；各角色轮流取一组，同样的检索只执行一次。
            scheduled = [(agent, grouped[number])
                for number in range(max((len(items) for items in grouped_by_agent.values()), default=0))
                for agent, grouped in grouped_by_agent.items() if number < len(grouped)]
            if client is not None and index.vector_count and view.settings.strategy not in {"bm25", "lexical_relations"}:
                before = budget.used
                try:
                    prefetch_ms, prefetch_tokens = client.prefetch_queries(tuple(query.query for _, (query, _) in scheduled))
                except RetrievalError:
                    # 仍由每次检索报告基础召回和额度降级，已生成的向量继续复用。
                    pass
                if scheduled:
                    calls_by_agent[scheduled[0][0]] += budget.used - before
            by_key = {unit.unit_key: unit for unit in model_input.units}
            lexical = self._lexical(index.id)
            shared_searches: dict[str, RetrievalTrace] = {}
            for agent, (query, keys) in scheduled:
                selected_units = tuple(by_key[key] for key in keys)
                query = query.model_copy(update={"symbols": changed_symbols(
                    lexical.documents_for_files(tuple(unit.file for unit in selected_units)), selected_units)})
                search_key = stable_key(index.id, query.model_dump(mode="json"))
                if search_key not in shared_searches:
                    before = budget.used
                    shared_searches[search_key] = self._search(index, query, view.settings, client)
                    calls_by_agent[agent] += budget.used - before
                traces_by_agent[agent].append((shared_searches[search_key], keys))
                on_progress()
            for agent in purposes:
                units = units_by_agent[agent]
                if not units:
                    continue
                trace = cached.get(agent.value)
                if trace is None:
                    grouped = grouped_by_agent[agent]
                    traces = traces_by_agent[agent]
                    first = traces[0][0]
                    candidates = merge_contexts(traces, view.settings.context_k)
                    trace = first.model_copy(update={
                        "id": str(uuid4()), "agent": agent, "plan_fingerprint": model_input.plan_fingerprint,
                        "queries": tuple(query.query for query, _ in grouped), "query": f"{len(grouped)} 组变更上下文检索",
                        "candidates": candidates, "total_units": len(units),
                        "strategies_used": tuple(dict.fromkeys(item.strategy for item, _ in traces)),
                        "vector_search_mode": ",".join(sorted({item.vector_search_mode for item, _ in traces} - {"unused"})) or "unused",
                        "query_cache_hit": all(item.query_cache_hit for item, _ in traces),
                        "rerank_cache_hit": all(item.rerank_cache_hit for item, _ in traces),
                        "covered_units": len({key for item in candidates if item.selected for key in item.unit_keys}),
                        "duration_ms": sum(item.duration_ms for item, _ in traces) + prefetch_ms,
                        "warnings": tuple(dict.fromkeys(warning for item, _ in traces for warning in item.warnings)),
                        "model_requests": calls_by_agent[agent],
                        "embedding_ms": sum(item.embedding_ms for item, _ in traces) + prefetch_ms,
                        "rerank_ms": sum(item.rerank_ms for item, _ in traces),
                        "input_tokens": (sum(item.input_tokens or 0 for item, _ in traces) + (prefetch_tokens or 0)) or None,
                        "rerank_tokens": sum(item.rerank_tokens or 0 for item, _ in traces) or None,
                        "routes": tuple(RouteMetric(route=cast(RetrievalRoute, route), candidate_count=sum(metric.candidate_count for item, _ in traces for metric in item.routes if metric.route == route), duration_ms=sum(metric.duration_ms for item, _ in traces for metric in item.routes if metric.route == route)) for route in ("bm25", "vector", "relation") if any(metric.route == route for item, _ in traces for metric in item.routes)),
                    })
                    trace = self.repository.save_trace(trace, model_input.review_run_id, agent.value)
                    prefetch_ms, prefetch_tokens = 0, None
                if trace.index_id != index.id:
                    raise RetrievalError("部分审查已保存旧配置的上下文，请恢复原配置后重试")
                contexts.extend(item.model_copy(update={"agent": agent}) for item in trace.selected)
        finally:
            if client is not None:
                client.close()
        on_progress()
        return model_input.model_copy(update={"context_evidence": tuple(contexts)})
    def evaluate(
        self, index_id: str, cases: Sequence[RetrievalEvaluationCase], *,
        dataset_version: str, annotation_source: AnnotationSource,
        strategies: tuple[RetrievalStrategy, ...] = ("bm25", "hybrid", "hybrid_relations", "reranked"),
        k: int = 8, scope: ResourceScope | None = None,
    ) -> RetrievalEvaluationReport:
        if not 1 <= k <= 20 or not strategies or len(set(strategies)) != len(strategies):
            raise ValueError("评测 K 值或策略列表无效")
        if not 1 <= len(cases) <= 100 or len({case.id for case in cases}) != len(cases):
            raise ValueError("评测需要 1 到 100 个不重复样本")
        index = self.repository.get(index_id, scope)
        view, key = self.settings.runtime()
        client = self._client(view, key) if any(value not in {"bm25", "lexical_relations"} for value in strategies) else None
        reports: list[StrategyEvaluation] = []
        vector_modes: set[str] = set()
        try:
            # 每个策略使用相同的词法热缓存，避免后运行的策略白占缓存优势。
            lexical = self._lexical(index_id)
            for case in cases:
                lexical.search(case.query, view.settings.candidate_k)
            # Warm query vectors once for every strategy. Timings then compare
            # retrieval/reranking rather than mixing cold and warm cache states.
            if client is not None:
                query_hashes = {sha256(case.query.encode()).hexdigest(): case.query for case in cases}
                keys = {digest: stable_key(view.settings.embedding_fingerprint, digest) for digest in query_hashes}
                existing = self.repository.existing_embeddings(tuple(keys.values()))
                pending = [(digest, text) for digest, text in query_hashes.items() if keys[digest] not in existing]
                for offset in range(0, len(pending), 4):
                    batch = pending[offset:offset + 4]
                    response = client.embed(tuple(text for _, text in batch), purpose="query")
                    self.repository.store_embeddings(view.settings.embedding_fingerprint, [
                        (keys[digest], digest, vector) for (digest, _), vector in zip(batch, response.vectors, strict=True)
                    ])
            for strategy in strategies:
                items: list[dict[str, object]] = []
                recalls, ranks, durations = [], [], []
                for case in cases:
                    trace = self._search(index, SearchQuery(query=case.query, seed_files=case.seed_files, symbols=case.symbols, strategy=strategy, limit=k), view.settings, client)
                    if trace.strategy != strategy:
                        raise RetrievalError("评测中发生策略降级，未生成效果对比报告；已有缓存已保留，请核对调用上限和服务状态")
                    vector_modes.add(trace.vector_search_mode)
                    relevant = set(case.relevant_symbols)
                    symbols = [item.symbol for item in trace.selected]
                    recall = len(relevant.intersection(symbols)) / len(relevant)
                    rank = next((position for position, symbol in enumerate(symbols, 1) if symbol in relevant), None)
                    reciprocal = 1 / rank if rank else 0
                    recalls.append(recall)
                    ranks.append(reciprocal)
                    durations.append(trace.duration_ms)
                    items.append({"case_id": case.id, "split": case.split, "query_cache_hit": trace.query_cache_hit, "recall_at_k": recall, "mrr": reciprocal, "symbols": symbols, "duration_ms": trace.duration_ms, "warnings": list(trace.warnings)})
                ordered = sorted(durations)
                reports.append(StrategyEvaluation(
                    strategy=strategy, sample_count=len(cases), recall_at_k=statistics.mean(recalls),
                    mrr=statistics.mean(ranks), k=k, median_duration_ms=statistics.median(durations),
                    p95_duration_ms=ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], cases=tuple(items),
                ))
        finally:
            if client is not None:
                client.close()
        report = RetrievalEvaluationReport(
            id=str(uuid4()), index_id=index_id, dataset_version=dataset_version,
            annotation_source=annotation_source, embedding_model=view.settings.embedding_model,
            rerank_model=view.settings.rerank_model, generated_at=datetime.now(UTC).isoformat(),
            strategies=tuple(reports),
            query_cache_mode="shared_warm" if client is not None else "not_used",
            lexical_cache_mode="shared_warm",
            vector_search_mode=",".join(sorted(vector_modes - {"unused"})) or "not_used",
        )
        self.repository.save_evaluation(report)
        return report
