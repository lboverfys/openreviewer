"""基础索引先发布；显式请求的向量补全失败不撤销可用的源码证据。"""

import time
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from domain.retrieval import CodeChunk, IndexView, SourceFile, stable_key
from domain.security import SafeError
from persistence.retrieval import RetrievalRepository
from persistence.retrieval_runtime import RetrievalRuntimeRepository
from services.code_indexing import parse_cache_key, parse_sources
from services.egress import check_paths
from services.retrieval_providers import RequestBudget, RetrievalError


def embedding_batches(chunks: Sequence[CodeChunk], max_bytes: int = 64_000) -> Iterator[tuple[CodeChunk, ...]]:
    batch: list[CodeChunk] = []
    size = 0
    for chunk in chunks:
        amount = len(chunk.embedding_text.encode())
        if amount > max_bytes:
            raise RetrievalError(f"单个代码块超过单次向量请求文本上限 {max_bytes / 1000:g} KB，请调整配置")
        if batch and (len(batch) >= 20 or size + amount > max_bytes):
            yield tuple(batch)
            batch, size = [], 0
        batch.append(chunk)
        size += amount
    if batch:
        yield tuple(batch)


def build_index(repository: RetrievalRepository, settings_service: Any, client_factory: Callable[..., Any],
                source_loader: Callable[..., Sequence[SourceFile]] | None, claim: tuple[str, str, dict[str, Any], str], sources: Sequence[SourceFile] | None,
                on_progress: Callable[[], None] | None) -> IndexView:
    index_id, owner, target, configuration_key = claim
    started = time.monotonic()
    view, key = settings_service.runtime()
    client = None

    def heartbeat() -> None:
        if on_progress:
            on_progress()
        repository.renew(index_id, owner)

    try:
        if configuration_key != view.settings.embedding_fingerprint:
            raise RetrievalError("索引模型配置已变更，请创建新索引")
        heartbeat()
        existing = repository.get(index_id)
        if not existing.lexical_ready:
            if sources is None:
                if source_loader is None:
                    raise RetrievalError("GitHub 代码来源尚未配置")
                sources = source_loader(target, heartbeat)
            parse_keys = {source.file: parse_cache_key(source) for source in sources}
            cached = repository.cached_parses(tuple(parse_keys.values()))
            parsed = parse_sources(sources, cached)
            by_file: dict[str, list[CodeChunk]] = defaultdict(list)
            for chunk in parsed.chunks:
                by_file[chunk.file].append(chunk)
            missing_parses = [(parse_keys[source.file], by_file[source.file]) for source in sources if parse_keys[source.file] not in cached]
            for offset in range(0, len(missing_parses), 50):
                heartbeat()
                repository.store_parses(missing_parses[offset:offset + 50])
            repository.reset(index_id, owner)
            for offset in range(0, len(parsed.chunks), 200):
                heartbeat()
                repository.store_chunk_batch(index_id, owner, parsed.chunks[offset:offset + 200], configuration_key)
            repository.finish(index_id, owner, parsed.relations, file_count=len(sources),
                chunk_count=len(parsed.chunks), embedded_count=0, reused_count=0,
                duration_ms=round((time.monotonic() - started) * 1000),
                parse_errors=parsed.parse_error_files, parsed_files=len(missing_parses), reused_files=len(cached), release=False)
        # 已发布的基础快照不重置。续建只读取缺失向量对应的代码块。
        vector_count = repository.link_vectors(index_id, owner, configuration_key)
        index = repository.get(index_id)
        embedded = 0
        reused = vector_count
        continue_vectors = False
        retry_delay = 0.0
        failures = int(target.get("vector_failures", 0))
        vector_status, vector_error = "ready", None
        if vector_count < index.chunk_count:
            vector_status = "paused" if view.external_calls_paused else "pending"
            if target.get("include_vectors") and not view.external_calls_paused:
                try:
                    runtime = RetrievalRuntimeRepository(repository.sessions)
                    operation = stable_key(index_id, target.get("vector_operation_id", index_id))
                    budget = RequestBudget(view.settings.max_requests_per_operation,
                        used=runtime.request_usage(operation),
                        charge=lambda: runtime.charge_request(operation, view.settings.max_requests_per_operation))
                    client = client_factory(view, key, budget)
                    remaining = view.settings.max_new_vectors_per_index
                    # 分页取缺失片段，按服务商批量接口补全；缓存命中不重复调用。
                    priority_files = repository.vector_priority_files(index_id, tuple(target.get("priority_files", ())))
                    for page in repository.missing_chunk_pages(index_id, priority_files):
                        if remaining <= 0:
                            break
                        for batch in embedding_batches(page[:remaining], view.settings.embedding_batch_max_bytes):
                            heartbeat()
                            if time.monotonic() - started > 180:
                                remaining = 0
                                break
                            check_paths(tuple(chunk.file for chunk in batch))
                            client.embed(tuple(chunk.embedding_text for chunk in batch))
                            embedded += len(batch)
                            remaining -= len(batch)
                            vector_count = repository.link_vectors(index_id, owner, configuration_key, tuple(chunk.id for chunk in batch))
                            repository.progress(index_id, owner, index.file_count, index.chunk_count, embedded, reused)
                    failures = 0
                    if vector_count == index.chunk_count:
                        vector_status = "ready"
                    elif view.settings.max_new_vectors_per_index == 0:
                        vector_status, vector_error = "limited", "每轮新增向量上限 0，请在模型与审查设置中调整"
                    elif budget.used >= budget.limit:
                        vector_status, vector_error = "limited", f"达到本次补全累计请求上限 {budget.limit}，已覆盖 {vector_count}/{index.chunk_count}；调整额度后可继续补全"
                    else:
                        vector_status, continue_vectors = "pending", True
                        vector_error = f"本轮新增 {embedded} 个向量，等待索引 Worker 继续；已覆盖 {vector_count}/{index.chunk_count}"
                except RetrievalError as exc:
                    failures += 1
                    continue_vectors = exc.retryable and failures < 3
                    retry_delay = max(30.0, exc.retry_after) if continue_vectors else 0
                    vector_status = "pending" if continue_vectors else "limited" if budget.exhausted else "failed"
                    vector_error = str(exc)
        repository.complete(index_id, owner, vector_count=vector_count, vector_status=vector_status,
            vector_error=vector_error, embedded=embedded, reused=reused,
            duration_ms=round((time.monotonic() - started) * 1000),
            continue_vectors=continue_vectors, retry_delay=retry_delay, vector_failures=failures)
        return repository.get(index_id)
    except Exception as exc:
        repository.fail(index_id, owner, SafeError.from_exception(exc).safe_message)
        raise
    finally:
        if client is not None:
            client.close()
