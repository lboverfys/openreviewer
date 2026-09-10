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
from services.retrieval_providers import RequestBudget, RetrievalError


def embedding_batches(chunks: Sequence[CodeChunk]) -> Iterator[tuple[CodeChunk, ...]]:
    batch: list[CodeChunk] = []
    size = 0
    for chunk in chunks:
        amount = len(chunk.embedding_text.encode())
        if batch and (len(batch) >= 20 or size + amount > 64_000):
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
        vector_status, vector_error = "ready", None
        if vector_count < index.chunk_count:
            vector_status = "paused" if view.external_calls_paused else "pending"
            if target.get("include_vectors") and not view.external_calls_paused:
                missing_count = repository.missing_vector_count(index_id)
                if missing_count > view.settings.max_new_vectors_per_index:
                    vector_status = "limited"
                    vector_error = f"需要新增 {missing_count} 条向量，超过上限 {view.settings.max_new_vectors_per_index}；尚未发起向量请求"
                else:
                    try:
                        runtime = RetrievalRuntimeRepository(repository.sessions)
                        operation = stable_key(index_id, target.get("vector_operation_id", index_id))
                        budget = RequestBudget(view.settings.max_requests_per_operation, charge=lambda: runtime.charge_request(operation, view.settings.max_requests_per_operation))
                        client = client_factory(view, key, budget)
                        # 按主键游标取200块，HTTP每批20块；不在内存累计整个仓库正文。
                        for page in repository.missing_chunk_pages(index_id):
                            for batch in embedding_batches(page):
                                heartbeat()
                                client.embed(tuple(chunk.embedding_text for chunk in batch))
                                embedded += len(batch)
                                repository.progress(index_id, owner, index.file_count, index.chunk_count, embedded, reused)
                        vector_status = "ready"
                    except RetrievalError as exc:
                        vector_status, vector_error = "failed", str(exc)
            if embedded:
                vector_count = repository.link_vectors(index_id, owner, configuration_key)
        repository.complete(index_id, owner, vector_count=vector_count, vector_status=vector_status,
            vector_error=vector_error, embedded=embedded, reused=reused,
            duration_ms=round((time.monotonic() - started) * 1000))
        return repository.get(index_id)
    except Exception as exc:
        repository.fail(index_id, owner, SafeError.from_exception(exc).safe_message)
        raise
    finally:
        if client is not None:
            client.close()
