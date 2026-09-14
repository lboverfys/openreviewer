from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from domain.retrieval import (
    RetrievalEvaluationCase,
    RetrievalSettings,
    SearchQuery,
    SourceFile,
)
from persistence.models import Base, CodeIndexRecord
from persistence.retrieval import RetrievalRepository
from services.ai_settings import AiSecretCipher
from services.code_indexing import git_blob_sha
from services.retrieval import (
    HybridRetrievalService,
    RetrievalSettingsService,
    reciprocal_rank_fusion,
)
from services.retrieval_providers import EmbeddingResult, RerankResult, RetrievalError

TARGET = {"installation_id": 10, "repository_id": 123, "repository": "sample/repo", "head_sha": "a" * 40}


class FakeModels:
    embeddings = 0

    def __init__(self, settings, key):
        self.settings = settings

    def close(self):
        pass

    def embed(self, texts):
        FakeModels.embeddings += len(texts)
        vectors = []
        for text in texts:
            vector = [0.0] * 1024
            vector[0 if "user" in text.casefold() else 1] = 1.0
            vectors.append(tuple(vector))
        return EmbeddingResult(tuple(vectors), 1, 10)

    def rerank(self, query, documents):
        scores = [(i, 1.0 if "select" in value.casefold() else 0.1) for i, value in enumerate(documents)]
        return RerankResult(tuple(sorted(scores, key=lambda item: (-item[1], item[0]))), 1, 10)


@pytest.fixture
def retrieval(tmp_path, monkeypatch):
    # This fixture injects FakeModels; it never constructs a live model client.
    monkeypatch.setenv("OPENREVIEWER_RETRIEVAL_API_DISABLED", "false")
    engine = create_engine("sqlite:///" + str(tmp_path / "retrieval.sqlite3"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = RetrievalSettingsService(sessions, AiSecretCipher(b"a" * 32))
    settings.update(RetrievalSettings(enabled=True, api_host="https://sample.cn-beijing.maas.aliyuncs.com"), 0, "tester", "test-key")
    service = HybridRetrievalService(RetrievalRepository(sessions), settings, client_factory=FakeModels)
    try:
        yield service, sessions, engine
    finally:
        engine.dispose()


def sources(prefix=""):
    values = [
        ("UserMapper.java", "package sample; interface UserMapper { User getById(long id); }"),
        ("UserService.java", "package sample; class UserService { UserMapper mapper; User load(long id) { return mapper.getById(id); } }"),
        ("UserMapper.xml", '<mapper namespace="sample.UserMapper"><select id="getById">SELECT id FROM users WHERE id = #{id}</select></mapper>'),
    ]
    return tuple(SourceFile(file=path, content=prefix + text, blob_sha=git_blob_sha(prefix + text)) for path, text in values)


def test_rrf_uses_ranks_and_deduplicates_within_a_route():
    fused = reciprocal_rank_fusion({"bm25": [("a", 999), ("b", 1), ("a", 999)], "vector": [("b", 0.9), ("c", 0.8)]})
    assert fused[0][0] == "b"
    assert len(fused) == 3
    assert fused[1][1] == pytest.approx(1 / 61)


def test_index_search_cache_and_snapshot_reuse(retrieval):
    service, _, _ = retrieval
    FakeModels.embeddings = 0
    index = service.index_sources(TARGET, sources())
    assert index.status == "ready"
    assert index.chunk_count >= 5
    assert index.relation_count >= 3
    first_count = FakeModels.embeddings
    assert service.index_sources(TARGET, sources()).id == index.id
    assert FakeModels.embeddings == first_count
    query = SearchQuery(query="find the user query SQL", seed_files=("UserService.java",), strategy="reranked", limit=3)
    first = service.search(index.id, query)
    second = service.search(index.id, query)
    assert not first.query_cache_hit
    assert second.query_cache_hit
    assert first.candidates[0].symbol == "sample.UserMapper.getById"
    changed = service.index_sources({**TARGET, "head_sha": "b" * 40}, sources("\n"))
    assert changed.id != index.id
    assert changed.reused_count > 0
    assert changed.embedded_count == 0


def test_scope_prevents_cross_repository_access(retrieval):
    from services.rbac import ResourceScope
    service, _, _ = retrieval
    index = service.index_sources(TARGET, sources())
    denied = ResourceScope(installation_ids=frozenset({10}), repositories=frozenset({"other/repo"}))
    assert service.repository.list_indexes(denied) == ()
    with pytest.raises(LookupError):
        service.search(index.id, SearchQuery(query="user"), denied)


def test_expired_index_lease_rejects_old_owner(retrieval):
    service, sessions, _ = retrieval
    index_id = service.enqueue(TARGET)
    claim = service.repository.claim(index_id)
    assert claim is not None
    _, owner, _, _ = claim
    with sessions() as session, session.begin():
        row = session.get(CodeIndexRecord, index_id)
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    newer = service.repository.claim(index_id)
    assert newer is not None and newer[1] != owner
    with pytest.raises(RetrievalError, match="租约"):
        service.repository.progress(index_id, owner, 1, 1, 0, 0)
    service.repository.fail(index_id, owner, "old failure")
    assert service.repository.get(index_id).status == "building"


def test_embedding_configuration_change_requires_new_index(retrieval):
    service, _, _ = retrieval
    index = service.index_sources(TARGET, sources())
    view = service.settings.get()
    service.settings.update(view.settings.model_copy(update={"embedding_model": "another-model"}), view.revision, "tester")
    with pytest.raises(RetrievalError, match="配置不同"):
        service.search(index.id, SearchQuery(query="user"))
    assert service.search(index.id, SearchQuery(query="user", strategy="bm25")).candidates


def test_metrics_are_computed_from_labels_and_preserve_annotation_origin(retrieval):
    service, _, _ = retrieval
    index = service.index_sources(TARGET, sources())
    report = service.evaluate(index.id, [
        RetrievalEvaluationCase(id="sql", query="find user SQL", relevant_symbols=("sample.UserMapper.getById",), seed_files=("UserService.java",)),
    ], dataset_version="controlled-v1", annotation_source="synthetic_contract", k=8)
    assert len(report.strategies) == 4
    assert all(item.recall_at_k == 1 for item in report.strategies)
    assert report.real_review_accuracy is None
    assert report.annotation_source == "synthetic_contract"


def test_candidate_loading_is_one_query_for_multiple_ids(retrieval):
    service, _, engine = retrieval
    index = service.index_sources(TARGET, sources())
    ids = [row[0] for row in service.repository.lexical_documents(index.id)]
    statements = []
    def collect(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    event.listen(engine, "before_cursor_execute", collect)
    try:
        result = service.repository.chunks(index.id, ids)
    finally:
        event.remove(engine, "before_cursor_execute", collect)
    assert len(result) == len(ids)
    assert len(statements) == 1


def test_configuration_secret_is_encrypted_and_not_returned(retrieval):
    from persistence.models import RetrievalSettingsRecord
    service, sessions, _ = retrieval
    assert "test-key" not in service.settings.get().model_dump_json()
    with sessions() as session:
        row = session.scalar(select(RetrievalSettingsRecord))
        assert row.ciphertext != b"test-key"
    view = service.settings.get()
    with pytest.raises(ValueError, match="新的 API Key"):
        service.settings.update(view.settings.model_copy(update={"api_host": "https://another.cn-beijing.maas.aliyuncs.com"}), view.revision, "tester")

def test_unchanged_files_reuse_parse_cache(retrieval, monkeypatch):
    service, _, _ = retrieval
    first = service.index_sources(TARGET, sources())
    assert first.parsed_files == len(sources())
    def fail_if_parsed(source):
        raise AssertionError("unchanged source parsed again")
    monkeypatch.setattr("services.code_indexing._java", fail_if_parsed)
    monkeypatch.setattr("services.code_indexing._xml", fail_if_parsed)
    second = service.index_sources({**TARGET, "head_sha": "c" * 40}, sources())
    assert second.parsed_files == 0
    assert second.reused_files == len(sources())
    assert second.embedded_count == 0


def test_retrieval_secret_rotation_preserves_runtime_key(retrieval):
    from services.ai_secret_rotation import AiSecretRotationService
    service, sessions, _ = retrieval
    cipher = AiSecretCipher(b"b" * 32, key_version=2, previous_keys=((1, b"a" * 32),))
    result = AiSecretRotationService(sessions, cipher).rotate_batch()
    assert result.retrieval_secrets == 1
    assert RetrievalSettingsService(sessions, cipher).runtime()[1] == "test-key"

def test_index_limit_is_checked_before_model_requests(retrieval):
    service, _, _ = retrieval
    view = service.settings.get()
    service.settings.update(view.settings.model_copy(update={"max_new_vectors_per_index": 0}), view.revision, "tester")
    FakeModels.embeddings = 0
    index = service.index_sources(TARGET, sources())
    assert index.lexical_ready and index.vector_status == "limited"
    assert "上限 0" in index.vector_error
    assert FakeModels.embeddings == 0

def test_review_retries_reuse_frozen_context_after_configuration_changes(retrieval):
    from domain.models import ReviewRequest
    from persistence.repositories import SqlAlchemyReviewRepository
    from services.reviews import ReviewService
    from tests.unit.test_model_review import make_model_input

    service, sessions, _ = retrieval
    original = make_model_input()
    target = {"installation_id": 10, "repository_id": original.repository_id, "repository": original.repository, "head_sha": original.head_sha}
    review = ReviewService(SqlAlchemyReviewRepository(sessions)).submit(
        ReviewRequest(**target, pull_request_number=original.pull_request_number), "context-freeze",
    )
    service.index_sources(target, sources())
    review_input = original.model_copy(update={"review_run_id": review.review_run_id})
    first = service.review_context(review_input, lambda: None)
    assert first.context_evidence
    view = service.settings.get()
    service.settings.update(view.settings.model_copy(update={"embedding_model": "changed", "enabled": False}), view.revision, "tester")
    second = service.review_context(review_input, lambda: None)
    assert second.context_evidence == first.context_evidence


def test_identical_agent_queries_share_search_and_batch_vectors(retrieval, monkeypatch):
    from domain.enums import ReviewAgent
    from domain.models import ReviewRequest
    from persistence.repositories import SqlAlchemyReviewRepository
    from services.reviews import ReviewService
    from tests.unit.test_model_review import make_model_input

    service, sessions, _ = retrieval
    original = make_model_input()
    target = {"installation_id": 10, "repository_id": original.repository_id,
              "repository": original.repository, "head_sha": original.head_sha}
    run = ReviewService(SqlAlchemyReviewRepository(sessions)).submit(
        ReviewRequest(**target, pull_request_number=original.pull_request_number), "dedup-agents")
    service.index_sources(target, sources())
    unit = original.units[0].model_copy(update={"review_domains": (ReviewAgent.SECURITY, ReviewAgent.CONVENTION, ReviewAgent.LOGIC)})
    model_input = original.model_copy(update={"review_run_id": run.review_run_id, "units": (unit,)})
    searches = []
    search = service._search
    def capture(*args):
        searches.append(args[1].query)
        return search(*args)
    monkeypatch.setattr(service, "_search", capture)
    result = service.review_context(model_input, lambda: None)
    assert result.context_evidence
    assert len(searches) == 1
    assert {item.agent for item in result.context_evidence} == {ReviewAgent.SECURITY, ReviewAgent.CONVENTION, ReviewAgent.LOGIC}


def test_pause_allows_basic_index_but_blocks_explicit_vector_enrichment(retrieval, monkeypatch):
    service, _, _ = retrieval
    ready = service.index_sources(TARGET, sources())
    queued = service.enqueue({**TARGET, "head_sha": "b" * 40})
    failed = service.enqueue({**TARGET, "head_sha": "c" * 40})
    claim = service.repository.claim(failed)
    service.repository.fail(failed, claim[1], "fixture failure")
    requests_before = FakeModels.embeddings
    def forbidden_loader(*args):
        raise AssertionError("paused worker must not load source files")
    service.source_loader = forbidden_loader
    monkeypatch.setenv("OPENREVIEWER_RETRIEVAL_API_DISABLED", "true")
    assert service.enqueue({**TARGET, "head_sha": "d" * 40})
    with pytest.raises(RetrievalError, match="暂停"):
        service.retry_index(failed, None, include_vectors=True)
    assert service.repository.get(queued).status == "queued"
    assert service.repository.get(failed).status == "failed"
    assert service.search(ready.id, SearchQuery(query="user", strategy="bm25")).candidates
    assert FakeModels.embeddings == requests_before


def test_offline_index_and_fallback_search_never_construct_model_client(retrieval, monkeypatch):
    service, _, _ = retrieval
    monkeypatch.setenv("OPENREVIEWER_RETRIEVAL_API_DISABLED", "true")
    def forbidden(*args):
        raise AssertionError("offline indexing must not construct a model client")
    service.client_factory = forbidden
    index = service.index_sources(TARGET, sources(), include_vectors=False)
    assert index.status == "ready" and index.lexical_ready and index.vector_status == "paused"
    trace = service.search(index.id, SearchQuery(query="getById", strategy="reranked", seed_files=("UserService.java",)))
    assert trace.strategy == "lexical_relations" and trace.requested_strategy == "reranked"
    assert trace.candidates and trace.model_requests == 0


def test_provider_failure_keeps_base_index_and_requests_are_bounded(retrieval):
    service, _, _ = retrieval
    current = service.settings.get()
    service.settings.update(current.settings.model_copy(update={"max_requests_per_operation": 0}), current.revision, "tester")
    count = FakeModels.embeddings
    index = service.index_sources(TARGET, sources())
    assert index.lexical_ready and index.vector_status == "failed"
    assert "请求上限" in index.vector_error
    assert service.search(index.id, SearchQuery(query="user", strategy="bm25")).candidates
    assert FakeModels.embeddings == count

    service.repository.retry(index.id, None, include_vectors=True)
    queued = service.repository.get(index.id)
    assert queued.status == "queued" and queued.vector_status == "pending"
    assert queued.vector_error is None


def test_repeated_search_reuses_embedding_and_rerank(retrieval):
    service, _, _ = retrieval
    index = service.index_sources(TARGET, sources())
    query = SearchQuery(query="find user SQL", strategy="reranked")
    first, second = service.search(index.id, query), service.search(index.id, query)
    assert first.model_requests == 2
    assert second.model_requests == 0 and second.rerank_cache_hit
    assert [(item.chunk_id, item.rank) for item in first.candidates] == [(item.chunk_id, item.rank) for item in second.candidates]


def test_shared_provider_lane_and_circuit_breaker(retrieval):
    from persistence.retrieval_runtime import RetrievalRuntimeRepository
    service, sessions, _ = retrieval
    first, second = RetrievalRuntimeRepository(sessions), RetrievalRuntimeRepository(sessions)
    with first.model_lane("test-lane"):
        with pytest.raises(RetrievalError, match="其他请求"):
            with second.model_lane("test-lane"):
                raise AssertionError("a second process entered the same provider lane")
    for _ in range(3):
        with pytest.raises(RetrievalError, match="fixture failure"):
            with first.model_lane("test-lane"):
                raise RetrievalError("fixture failure")
    assert second.circuit_state("test-lane") == (False, True)
    with pytest.raises(RetrievalError, match="熔断"):
        with second.model_lane("test-lane"):
            raise AssertionError("open circuit must not enter the provider")


def test_operation_budget_survives_recreating_the_gateway(retrieval):
    from persistence.retrieval_runtime import RetrievalRuntimeRepository
    from services.retrieval_providers import RequestBudget
    _, sessions, _ = retrieval
    runtime = RetrievalRuntimeRepository(sessions)
    first = RequestBudget(1, charge=lambda: runtime.charge_request("same-review-operation", 1))
    first.consume()
    retry = RequestBudget(1, charge=lambda: runtime.charge_request("same-review-operation", 1))
    with pytest.raises(RetrievalError, match="累计"):
        retry.consume()


def test_database_source_cache_handles_misses_and_streamed_rows(retrieval):
    from persistence.retrieval_runtime import RetrievalRuntimeRepository
    _, sessions, engine = retrieval
    runtime = RetrievalRuntimeRepository(sessions)
    assert runtime.source_blobs(("missing",)) == {}
    runtime.cache_blobs((("blob-a", "class A {}"), ("blob-b", "class B {}")))
    statements = []
    def collect(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    event.listen(engine, "before_cursor_execute", collect)
    try:
        assert runtime.source_blobs(("blob-a", "missing", "blob-b")) == {"blob-a": "class A {}", "blob-b": "class B {}"}
    finally:
        event.remove(engine, "before_cursor_execute", collect)
    assert len(statements) == 1


def test_retention_preserves_referenced_indexes_and_paid_vectors(retrieval):
    from sqlalchemy import update

    from domain.models import ReviewRequest
    from persistence.models import CodeEmbeddingRecord, CodeSourceCacheRecord
    from persistence.repositories import SqlAlchemyReviewRepository
    from persistence.retrieval_runtime import RetrievalRuntimeRepository
    from services.reviews import ReviewService
    service, sessions, _ = retrieval
    index = service.index_sources(TARGET, sources())
    review = ReviewService(SqlAlchemyReviewRepository(sessions)).submit(ReviewRequest(**TARGET, pull_request_number=1), "retained-context")
    trace = service.search(index.id, SearchQuery(query="user", strategy="bm25"), review_run_id=review.review_run_id)
    view = service.settings.get()
    service.repository.store_embeddings(view.settings.embedding_fingerprint, [("expired-query", "query-hash", (1.0,) * 1024)], purpose="query")
    service.index_sources({**TARGET, "head_sha": "c" * 40}, sources())
    old = datetime.now(UTC) - timedelta(days=120)
    with sessions() as session, session.begin():
        session.execute(update(CodeIndexRecord).where(CodeIndexRecord.id == index.id).values(created_at=old))
        session.execute(update(CodeEmbeddingRecord).values(created_at=old))
        session.add(CodeSourceCacheRecord(id="expired-source", content="class Old {}", created_at=old))
    runtime = RetrievalRuntimeRepository(sessions)
    assert runtime.cleanup() >= 1
    assert service.repository.get(trace.index_id).lexical_ready
    with sessions() as session:
        assert session.get(CodeSourceCacheRecord, "expired-source") is None
        assert session.get(CodeEmbeddingRecord, "expired-query") is None
        assert session.scalar(select(CodeEmbeddingRecord.id).limit(1)) is not None


def test_explicit_enrichment_reuses_the_published_base_snapshot(retrieval):
    service, _, _ = retrieval
    base = service.index_sources(TARGET, sources(), include_vectors=False)
    assert base.lexical_ready and base.vector_count == 0
    enriched = service.index_sources(TARGET, (), include_vectors=True)
    assert enriched.id == base.id and enriched.lexical_ready
    assert enriched.vector_status == "ready" and enriched.vector_count == base.chunk_count


def test_manual_search_persists_candidate_bodies(retrieval):
    from sqlalchemy import func

    from persistence.models import RetrievalTraceRecord
    service, sessions, _ = retrieval
    index = service.index_sources(TARGET, sources(), include_vectors=False)
    first = service.search(index.id, SearchQuery(query="user", strategy="bm25"))
    second = service.search(index.id, SearchQuery(query="user", strategy="bm25"))
    assert first.candidates and second.candidates
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(RetrievalTraceRecord)) == 2


def test_base_index_links_once_and_unchanged_links_are_not_rewritten(retrieval, monkeypatch):
    from domain.retrieval import stable_key
    service, _, engine = retrieval
    linked = []
    original = service.repository.link_vectors
    def link(*args):
        linked.append(True)
        return original(*args)
    monkeypatch.setattr(service.repository, "link_vectors", link)
    index = service.index_sources(TARGET, sources(), include_vectors=False)
    assert len(linked) == 1
    key = next(iter(service.repository.lexical_documents(index.id)))[0]
    chunk = service.repository.chunks(index.id, (key,))[key]
    config = service.settings.get().settings.embedding_fingerprint
    service.repository.store_embeddings(config, [(stable_key(config, chunk.embedding_hash), chunk.embedding_hash, (1.0,) * 1024)])
    service.repository.retry(index.id, None)
    claim = service.repository.claim(index.id)
    assert claim is not None
    updates = []
    def after(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE code_index_chunks"):
            updates.append(cursor.rowcount)
    event.listen(engine, "after_cursor_execute", after)
    try:
        assert original(index.id, claim[1], config) == 1
        assert original(index.id, claim[1], config) == 1
    finally:
        event.remove(engine, "after_cursor_execute", after)
    assert updates == [1, 0]


def test_saved_manual_history_survives_temporary_cache_cleanup(retrieval):
    from sqlalchemy import update

    from persistence.models import RetrievalTraceRecord
    from persistence.retrieval_runtime import RetrievalRuntimeRepository
    service, sessions, _ = retrieval
    index = service.index_sources(TARGET, sources(), include_vectors=False)
    trace = service.search(index.id, SearchQuery(query="user", strategy="bm25"))
    service.repository.save_trace(trace)  # 模拟升级前已保存的临时搜索。
    with sessions() as session, session.begin():
        session.execute(update(RetrievalTraceRecord).where(RetrievalTraceRecord.id == trace.id).values(created_at=datetime.now(UTC) - timedelta(days=15)))
    RetrievalRuntimeRepository(sessions).cleanup()
    with sessions() as session:
        assert session.get(RetrievalTraceRecord, trace.id) is not None
    assert service.repository.get(index.id).lexical_ready


def test_bm25_evaluation_does_not_claim_vector_execution(retrieval):
    service, _, _ = retrieval
    index = service.index_sources(TARGET, sources())
    requests_before = FakeModels.embeddings
    report = service.evaluate(index.id, (RetrievalEvaluationCase(id="lexical-only", query="getById SQL", relevant_symbols=("sample.UserMapper.getById",)),), dataset_version="fixture-bm25-only", annotation_source="synthetic_contract", strategies=("bm25",))
    assert report.query_cache_mode == "not_used"
    assert report.vector_search_mode == "not_used"
    assert FakeModels.embeddings == requests_before
