from sqlalchemy import insert, text

from domain.retrieval import RetrievalSettings, SearchQuery
from persistence.retrieval import RetrievalRepository
from services.ai_settings import AiSecretCipher
from services.retrieval import HybridRetrievalService, RetrievalSettingsService
from tests.integration.test_hybrid_retrieval import TARGET, FakeModels, sources
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)


def test_postgres_vector_queries_are_snapshot_scoped(postgres_database, monkeypatch):
    monkeypatch.setenv("OPENREVIEWER_RETRIEVAL_API_DISABLED", "false")
    database = postgres_database
    settings = RetrievalSettingsService(database.sessions, AiSecretCipher(b"k" * 32))
    settings.update(RetrievalSettings(enabled=True, api_host="https://fake.cn-beijing.maas.aliyuncs.com"), settings.get().revision, "test", "fake-key")
    service = HybridRetrievalService(RetrievalRepository(database.sessions), settings, client_factory=FakeModels)
    first = service.index_sources(TARGET, sources())
    second = service.index_sources({**TARGET, "head_sha": "d" * 40}, sources("\n"))
    hits = service.search(first.id, SearchQuery(query="find user SQL", strategy="hybrid_relations", seed_files=("UserService.java",)))
    assert hits.candidates
    assert {item.head_sha for item in hits.candidates} == {TARGET["head_sha"]}
    assert all(item.index_id == first.id for item in hits.candidates)
    assert any(metric.route == "vector" and metric.candidate_count > 0 for metric in hits.routes)
    assert second.reused_count == first.chunk_count
    with database.engine.connect() as connection:
        names = connection.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'code_embeddings'")).scalars().all()
    assert "ix_code_embeddings_hnsw" in names


def test_ann_filters_snapshot_and_falls_back_when_global_candidates_do_not_cover_it(postgres_database, monkeypatch):
    from domain.retrieval import stable_key
    from persistence.models import CodeEmbeddingRecord
    monkeypatch.setenv("OPENREVIEWER_RETRIEVAL_API_DISABLED", "false")
    monkeypatch.setattr("persistence.vector_search.ANN_MIN_VECTORS", 0)
    database = postgres_database
    settings = RetrievalSettingsService(database.sessions, AiSecretCipher(b"k" * 32))
    settings.update(RetrievalSettings(enabled=True, api_host="https://fake.cn-beijing.maas.aliyuncs.com"), settings.get().revision, "test", "fake-key")
    repo = RetrievalRepository(database.sessions)
    service = HybridRetrievalService(repo, settings, client_factory=FakeModels)
    index = service.index_sources(TARGET, sources())
    vector = [1.0] + [0.0] * 1023
    with database.engine.begin() as connection:
        connection.execute(text("ANALYZE code_embeddings"))
    with database.engine.connect() as connection:
        before = connection.execute(text("SHOW hnsw.ef_search")).scalar_one()
    result = repo.vector_search_details(index.id, vector, 3)
    assert result.mode == "hnsw_snapshot"
    assert result.hits == repo.vector_search_details(index.id, vector, 3, exact=True).hits
    with database.engine.connect() as connection:
        assert connection.execute(text("SHOW hnsw.ef_search")).scalar_one() == before
    # 更近的全局向量均不属于本提交，不能泄漏进结果，也不能导致不足K条。
    noise = [0.0, 0.0, 1.0] + [0.0] * 1021
    configuration = settings.get().settings.embedding_fingerprint
    with database.sessions() as session, session.begin():
        session.execute(insert(CodeEmbeddingRecord), [
            {"id": stable_key("noise", n), "configuration_key": configuration, "input_hash": stable_key("noise-input", n), "embedding": noise}
            for n in range(120)
        ])
    with database.engine.begin() as connection:
        connection.execute(text("ANALYZE code_embeddings"))
    assert repo.vector_search_details(index.id, noise, 3).mode == "exact_snapshot"
    monkeypatch.setattr("persistence.vector_search.MIN_ANN_COVERAGE", 0)
    fallback = repo.vector_search_details(index.id, noise, 3)
    exact = repo.vector_search_details(index.id, noise, 3, exact=True)
    assert fallback.mode == "exact_fallback" and fallback.hits == exact.hits
    assert len(fallback.hits) == 3
    monkeypatch.setenv("OPENREVIEWER_VECTOR_SEARCH_MODE", "exact")
    assert repo.vector_search_details(index.id, vector, 3).mode == "exact_snapshot"
