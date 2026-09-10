"""混合检索编排：冻结配置、批量索引、RRF、精排和可复现评测。"""

from __future__ import annotations

import math
import re
import statistics
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import RLock
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ReviewAgent
from domain.retrieval import (
    AnnotationSource,
    CodeChunk,
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
from services.ai_settings import AiSecretCipher
from services.code_indexing import code_tokens, parse_cache_key, parse_sources
from services.rbac import ResourceScope
from services.retrieval_providers import (
    AliyunRetrievalClient,
    RetrievalError,
    external_retrieval_paused,
    normalize_aliyun_host,
)

_CIPHER_SCOPE = "retrieval_aliyun"
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
                if settings.enabled and not settings.api_host:
                    raise ValueError("启用检索前必须配置百炼地址")
                if settings.enabled and row.ciphertext is None and encrypted is None:
                    raise ValueError("启用检索前必须配置 API Key")
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


class LexicalIndex:
    def __init__(self, documents: Iterable[tuple[str, str, str, dict[str, int], int, int]]) -> None:
        self.documents: dict[str, tuple[str, str, dict[str, int], int, int, int]] = {}
        self.postings: dict[str, set[str]] = defaultdict(set)
        total_length = 0
        for chunk_id, file, symbol, tokens, start_line, end_line in documents:
            length = sum(tokens.values())
            self.documents[chunk_id] = file, symbol, tokens, length, start_line, end_line
            total_length += length
            for token in tokens:
                self.postings[token].add(chunk_id)
        self.average_length = max(1.0, total_length / max(1, len(self.documents)))

    def search(self, query: str, limit: int) -> list[tuple[str, float]]:
        terms = set(code_tokens(query))
        candidates: set[str] = set()
        for term in terms:
            candidates.update(self.postings.get(term, ()))
        scores = []
        count = len(self.documents)
        for chunk_id in candidates:
            _, _, tokens, length, _, _ = self.documents[chunk_id]
            score = 0.0
            for term in terms.intersection(tokens):
                frequency = tokens[term]
                inverse = math.log(1 + (count - len(self.postings[term]) + 0.5) / (len(self.postings[term]) + 0.5))
                score += inverse * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * length / self.average_length))
            scores.append((chunk_id, score))
        return sorted(scores, key=lambda item: (-item[1], item[0]))[:limit]

    def seeds(self, query: SearchQuery) -> list[str]:
        files, symbols = set(query.seed_files), set(query.symbols)
        return [chunk_id for chunk_id, (file, symbol, _, _, _, _) in self.documents.items() if (symbol in symbols if symbols else file in files)][:100]


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
        self._lexical_lock = RLock()
        self._lexical_id: str | None = None
        self._lexical_cache: LexicalIndex | None = None

    def _client(self, view: RetrievalSettingsView, key: str | None):
        if not key:
            raise RetrievalError("检索模型密钥尚未配置")
        return self.client_factory(view.settings, key)

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

    def enqueue(self, target: dict[str, Any]) -> str:
        view = self.settings.get()
        if view.external_calls_paused:
            raise RetrievalError("检索模型外部调用已暂停，不能新建索引任务")
        if not view.settings.enabled or not view.key_configured:
            raise RetrievalError("混合检索尚未启用或配置不完整")
        return self.repository.enqueue(target, view.settings)

    def retry_index(self, index_id: str, scope: ResourceScope | None) -> None:
        if self.settings.get().external_calls_paused:
            raise RetrievalError("检索模型外部调用已暂停，不能重试索引任务")
        self.repository.retry(index_id, scope)

    def index_sources(self, target: dict[str, Any], sources: Sequence[SourceFile], on_progress: Callable[[], None] | None = None) -> IndexView:
        index_id = self.enqueue(target)
        existing = self.repository.get(index_id)
        if existing.status == "ready":
            return existing
        claim = self.repository.claim(index_id)
        if claim is None:
            raise RetrievalError("该提交的索引正在构建", retryable=True)
        return self._build(claim, sources, on_progress)

    def process_next(self, on_progress: Callable[[], None] | None = None) -> bool:
        view = self.settings.get()
        if view.external_calls_paused or not view.settings.enabled or not view.key_configured or self.source_loader is None:
            return False
        claim = self.repository.claim()
        if claim is None:
            return False
        self._build(claim, None, on_progress)
        return True

    def _build(self, claim, sources: Sequence[SourceFile] | None, on_progress: Callable[[], None] | None) -> IndexView:
        index_id, owner, target, configuration_key = claim
        started = time.monotonic()
        view, key = self.settings.runtime()
        client = None

        def heartbeat() -> None:
            if on_progress:
                on_progress()
            self.repository.renew(index_id, owner)

        try:
            client = self._client(view, key)
            if configuration_key != view.settings.embedding_fingerprint:
                raise RetrievalError("索引模型配置已变更，请创建新索引")
            heartbeat()
            if sources is None:
                if self.source_loader is None:
                    raise RetrievalError("GitHub 代码来源尚未配置")
                sources = self.source_loader(target, heartbeat)
            parse_keys = {source.file: parse_cache_key(source) for source in sources}
            parse_cache = self.repository.cached_parses(tuple(parse_keys.values()))
            parsed = parse_sources(sources, parse_cache)
            by_file: dict[str, list[CodeChunk]] = defaultdict(list)
            for chunk in parsed.chunks:
                by_file[chunk.file].append(chunk)
            new_parses = [(parse_keys[source.file], by_file[source.file]) for source in sources if parse_keys[source.file] not in parse_cache]
            for offset in range(0, len(new_parses), 50):
                heartbeat()
                self.repository.store_parses(new_parses[offset:offset + 50])
            self.repository.reset(index_id, owner)
            unique = {chunk.embedding_hash: chunk for chunk in parsed.chunks}
            keys = {digest: stable_key(configuration_key, digest) for digest in unique}
            cached = self.repository.existing_embeddings(tuple(keys.values()))
            missing = [chunk for digest, chunk in unique.items() if keys[digest] not in cached]
            if len(missing) > view.settings.max_new_vectors_per_index:
                raise RetrievalError(f"本索引需要新增 {len(missing)} 条向量，超过已配置上限 {view.settings.max_new_vectors_per_index}；尚未发起向量请求")
            embedded_count = 0
            reused_count = len(unique) - len(missing)
            self.repository.progress(index_id, owner, len(sources), len(parsed.chunks), embedded_count, reused_count)
            # This is bounded model batching (up to 20 chunks / 64 KB per call),
            # rather than one request or SQL query for each source file.
            for batch in _embedding_batches(missing):
                heartbeat()
                response = client.embed(tuple(chunk.embedding_text for chunk in batch))
                heartbeat()
                self.repository.store_embeddings(configuration_key, [
                    (keys[chunk.embedding_hash], chunk.embedding_hash, vector)
                    for chunk, vector in zip(batch, response.vectors, strict=True)
                ])
                embedded_count += len(batch)
                self.repository.progress(index_id, owner, len(sources), len(parsed.chunks), embedded_count, reused_count)
            for offset in range(0, len(parsed.chunks), 200):
                heartbeat()
                self.repository.store_chunk_batch(index_id, owner, parsed.chunks[offset:offset + 200], configuration_key)
            self.repository.finish(
                index_id, owner, parsed.relations, file_count=len(sources),
                chunk_count=len(parsed.chunks), embedded_count=embedded_count,
                reused_count=reused_count, duration_ms=round((time.monotonic() - started) * 1000),
                parse_errors=parsed.parse_error_files, parsed_files=len(new_parses), reused_files=len(parse_cache),
            )
            return self.repository.get(index_id)
        except Exception as exc:
            self.repository.fail(index_id, owner, SafeError.from_exception(exc).safe_message)
            raise
        finally:
            if client is not None:
                client.close()

    def _lexical(self, index_id: str) -> LexicalIndex:
        with self._lexical_lock:
            if self._lexical_id != index_id or self._lexical_cache is None:
                self._lexical_cache = LexicalIndex(self.repository.lexical_documents(index_id))
                self._lexical_id = index_id
            return self._lexical_cache

    def search(
        self, index_id: str, query: SearchQuery, scope: ResourceScope | None = None, *,
        review_run_id: str | None = None, agent: str | None = None,
        plan_fingerprint: str | None = None, on_progress: Callable[[], None] | None = None,
    ) -> RetrievalTrace:
        index = self.repository.get(index_id, scope)
        view, key = self.settings.runtime()
        client = self._client(view, key) if query.strategy != "bm25" else None
        try:
            trace = self._search(index, query, view.settings, client)
            if on_progress:
                on_progress()
            trace = trace.model_copy(update={"agent": ReviewAgent(agent) if agent else None, "plan_fingerprint": plan_fingerprint})
            return self.repository.save_trace(trace, review_run_id, agent)
        finally:
            if client is not None:
                client.close()

    def _search(self, index: IndexView, query: SearchQuery, settings: RetrievalSettings, client) -> RetrievalTrace:
        if index.status != "ready":
            raise RetrievalError("索引尚未就绪", retryable=True)
        expected_id = stable_key(index.installation_id, index.repository_id, index.head_sha, settings.embedding_fingerprint)
        if query.strategy != "bm25" and index.id != expected_id:
            raise RetrievalError("索引使用的向量模型配置与当前配置不同，请重建索引")
        started = time.monotonic()
        lexical = self._lexical(index.id)
        begin = time.monotonic()
        routes = {"bm25": lexical.search(query.query, settings.candidate_k)}
        metrics = [RouteMetric(route="bm25", candidate_count=len(routes["bm25"]), duration_ms=round((time.monotonic() - begin) * 1000))]
        embedding_ms = rerank_ms = 0
        input_tokens = rerank_tokens = None
        cache_hit = False
        warnings: list[str] = []
        if query.strategy != "bm25":
            begin = time.monotonic()
            digest = sha256(query.query.encode()).hexdigest()
            vector_key = stable_key(settings.embedding_fingerprint, digest)
            vector = self.repository.cached_vector(vector_key)
            cache_hit = vector is not None
            if vector is None:
                response = client.embed((query.query,))
                vector, embedding_ms, input_tokens = response.vectors[0], response.duration_ms, response.input_tokens
                self.repository.store_embeddings(settings.embedding_fingerprint, [(vector_key, digest, vector)])
            routes["vector"] = self.repository.vector_search(index.id, vector, settings.candidate_k)
            metrics.append(RouteMetric(route="vector", candidate_count=len(routes["vector"]), duration_ms=round((time.monotonic() - begin) * 1000)))
        if query.strategy in {"hybrid_relations", "reranked"}:
            begin = time.monotonic()
            seeds = lexical.seeds(query)
            routes["relation"] = self.repository.relation_search(index.id, seeds, settings.candidate_k)
            metrics.append(RouteMetric(route="relation", candidate_count=len(routes["relation"]), duration_ms=round((time.monotonic() - begin) * 1000)))
        fused = reciprocal_rank_fusion(routes)
        chunks = self.repository.chunks(index.id, [key for key, _, _ in fused])
        fused = [item for item in fused if item[0] in chunks]
        order = list(range(len(fused)))
        scores: dict[int, float] = {}
        if query.strategy == "reranked" and fused:
            # Keep query repetition and document bodies below the provider limit.
            per_document = min(6000, max(0, 110_000 // len(fused) - len(query.query.encode())))
            texts = tuple(chunks[item[0]].embedding_text.encode()[:per_document].decode("utf-8", errors="ignore") for item in fused)
            total_bytes = sum(len(text.encode()) for text in texts) + len(query.query.encode()) * len(texts)
            if total_bytes <= 110_000 and per_document >= 200:
                result = client.rerank(query.query, texts)
                order = [number for number, _ in result.ranking]
                scores = dict(result.ranking)
                rerank_ms, rerank_tokens = result.duration_ms, result.input_tokens
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
        return RetrievalTrace(
            id=str(uuid4()), index_id=index.id, query=query.query, strategy=query.strategy,
            candidates=tuple(candidates), routes=tuple(metrics),
            duration_ms=round((time.monotonic() - started) * 1000),
            embedding_ms=embedding_ms, rerank_ms=rerank_ms, input_tokens=input_tokens,
            rerank_tokens=rerank_tokens, query_cache_hit=cache_hit, warnings=tuple(warnings),
        )


    def review_context(self, model_input, on_progress: Callable[[], None]):
        view = self.settings.get()
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
        index_id = self.enqueue(target)
        index = self.repository.get(index_id)
        if index.status == "failed":
            raise RetrievalError("代码索引构建失败，请先重试索引")
        if index.status != "ready":
            claim = self.repository.claim(index_id)
            if claim is None:
                raise SafeApplicationError(SafeError(
                    code=ErrorCode.RETRIEVAL_INDEX_PENDING,
                    safe_message="等待同一提交的代码索引完成",
                    retryable=True,
                    details={"batch_retry_managed": True, "retry_at": (datetime.now(UTC) + timedelta(seconds=20)).isoformat()},
                ))
            index = self._build(claim, None, on_progress)
        purposes = {
            ReviewAgent.SECURITY: "authorization, authentication, sensitive data and security boundaries",
            ReviewAgent.CONVENTION: "repository conventions, interface contracts and maintainability",
            ReviewAgent.LOGIC: "business logic, transactions, database queries and concurrency",
        }
        contexts: list[ContextEvidence] = []
        for agent, purpose in purposes.items():
            units = tuple(unit for unit in model_input.units if agent in unit.review_domains)
            if not units:
                continue
            trace = cached.get(agent.value)
            if trace is None:
                ranges = {}
                for unit in units[:100]:
                    ranges[unit.file] = [(int(match.group(1)), int(match.group(1)) + max(1, int(match.group(2) or 1)) - 1) for match in re.finditer(r"^@@ -[0-9]+(?:,[0-9]+)? [+]([0-9]+)(?:,([0-9]+))? @@", unit.patch, re.M)]
                changed_symbols = tuple(dict.fromkeys(
                    symbol for file, symbol, _, _, first, last in self._lexical(index.id).documents.values()
                    if file in ranges and any(first <= end and last >= begin for begin, end in ranges[file])
                ))[:100]
                query_text = (
                    f"Find implementation and SQL evidence for reviewing {purpose}.\n"
                    + "\n".join(unit.file + "\n" + unit.patch[:500] for unit in units[:4])
                ).encode()[:1200].decode("utf-8", errors="ignore")
                trace = self.search(
                    index.id, SearchQuery(
                        query=query_text, seed_files=tuple(unit.file for unit in units[:100]), symbols=changed_symbols,
                        strategy=view.settings.strategy, limit=view.settings.context_k,
                    ), review_run_id=model_input.review_run_id, agent=agent.value,
                    plan_fingerprint=model_input.plan_fingerprint, on_progress=on_progress,
                )
            if trace.index_id != index.id:
                raise RetrievalError("部分审查已保存旧配置的上下文，请恢复原配置后重试")
            contexts.extend(item.model_copy(update={"agent": agent}) for item in trace.selected)
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
        client = self._client(view, key) if any(value != "bm25" for value in strategies) else None
        reports: list[StrategyEvaluation] = []
        try:
            # Warm query vectors once for every strategy. Timings then compare
            # retrieval/reranking rather than mixing cold and warm cache states.
            if client is not None:
                query_hashes = {sha256(case.query.encode()).hexdigest(): case.query for case in cases}
                keys = {digest: stable_key(view.settings.embedding_fingerprint, digest) for digest in query_hashes}
                existing = self.repository.existing_embeddings(tuple(keys.values()))
                pending = [(digest, text) for digest, text in query_hashes.items() if keys[digest] not in existing]
                for offset in range(0, len(pending), 4):
                    batch = pending[offset:offset + 4]
                    response = client.embed(tuple(text for _, text in batch))
                    self.repository.store_embeddings(view.settings.embedding_fingerprint, [
                        (keys[digest], digest, vector) for (digest, _), vector in zip(batch, response.vectors, strict=True)
                    ])
            for strategy in strategies:
                items: list[dict[str, object]] = []
                recalls, ranks, durations = [], [], []
                for case in cases:
                    trace = self._search(index, SearchQuery(query=case.query, seed_files=case.seed_files, symbols=case.symbols, strategy=strategy, limit=k), view.settings, client)
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
            vector_search_mode="exact_snapshot" if client is not None else "not_used",
        )
        self.repository.save_evaluation(report)
        return report


def _embedding_batches(chunks: Sequence[CodeChunk]) -> Iterator[tuple[CodeChunk, ...]]:
    batch: list[CodeChunk] = []
    size = 0
    for chunk in chunks:
        amount = len(chunk.embedding_text.encode())
        if batch and (len(batch) >= 20 or size + amount > 64_000):
            yield tuple(batch)
            batch = []
            size = 0
        batch.append(chunk)
        size += amount
    if batch:
        yield tuple(batch)
