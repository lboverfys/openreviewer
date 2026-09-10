from sqlalchemy import text

from domain.retrieval import RetrievalSettings, SearchQuery
from persistence.retrieval import RetrievalRepository
from services.ai_settings import AiSecretCipher
from services.retrieval import HybridRetrievalService, RetrievalSettingsService
from tests.integration.test_hybrid_retrieval import TARGET, FakeModels, sources
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)


def test_postgres_vector_queries_are_snapshot_scoped(postgres_database):
    database = postgres_database
    settings = RetrievalSettingsService(database.sessions, AiSecretCipher(b"k" * 32))
    settings.update(RetrievalSettings(enabled=True, api_host="https://fake.cn-beijing.maas.aliyuncs.com"), 0, "test", "fake-key")
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
