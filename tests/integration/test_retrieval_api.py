import asyncio

import httpx

from apps.api.main import create_app
from domain.models import ReviewRequest
from persistence.repositories import SqlAlchemyReviewRepository
from services.reviews import ReviewService
from tests.integration.test_hybrid_retrieval import TARGET, sources
from tests.integration.test_hybrid_retrieval import retrieval as retrieval
from tests.support import TEST_PASSWORD, TEST_USERNAME, make_auth_service


def test_retrieval_api_auth_configuration_and_scoped_index(retrieval, monkeypatch):
    service, sessions, _ = retrieval
    review = ReviewService(SqlAlchemyReviewRepository(sessions)).submit(
        ReviewRequest(**TARGET, pull_request_number=1), "retrieval-api-case",
    )
    app = create_app(auth_service=make_auth_service(), retrieval_service=service)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.get("/api/v1/retrieval/settings")).status_code == 401
            assert (await client.post("/api/v1/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})).status_code == 200
            current = await client.get("/api/v1/retrieval/settings")
            assert current.status_code == 200
            assert "test-key" not in current.text
            changed = await client.put("/api/v1/retrieval/settings", json={
                "settings": current.json()["settings"], "expected_revision": 999,
            })
            assert changed.status_code == 409
            settings = {**current.json()["settings"], "embedding_batch_max_bytes": 128_000, "context_max_bytes": 48_000}
            changed = await client.put("/api/v1/retrieval/settings", json={
                "settings": settings, "expected_revision": current.json()["revision"],
            })
            assert changed.status_code == 200, changed.text
            assert changed.json()["settings"]["embedding_batch_max_bytes"] == 128_000
            assert (await client.get("/api/v1/retrieval/settings")).json()["settings"]["context_max_bytes"] == 48_000
            invalid = await client.put("/api/v1/retrieval/settings", json={
                "settings": {**settings, "context_max_bytes": 256_001}, "expected_revision": changed.json()["revision"],
            })
            assert invalid.status_code == 422
            queued = await client.post("/api/v1/retrieval/indexes", json={"review_run_id": review.review_run_id})
            assert queued.status_code == 202
            index_id = queued.json()["id"]
            assert (await client.post("/api/v1/retrieval/indexes", json={"review_run_id": "not-a-review"})).status_code == 404
            service.index_sources(TARGET, sources())
            searched = await client.post(f"/api/v1/retrieval/indexes/{index_id}/search", json={"query": "find user SQL", "strategy": "reranked"})
            assert searched.status_code == 200, searched.text
            assert searched.json()["candidates"]
            assert searched.json()["context_budget"]["byte_limit"] == 48_000
            history = await client.get(f"/api/v1/retrieval/indexes/{index_id}/history")
            assert history.status_code == 200 and history.json()["items"][0]["id"] == searched.json()["id"]
            saved = await client.get("/api/v1/retrieval/history/" + searched.json()["id"])
            assert saved.json()["candidates"] == searched.json()["candidates"]
            bad = await client.post(f"/api/v1/retrieval/indexes/{index_id}/compare", json={"query": "user", "relevant_symbols": ["missing.symbol"]})
            assert bad.status_code == 422
            compared = await client.post(f"/api/v1/retrieval/indexes/{index_id}/compare", json={"query": "user", "relevant_symbols": ["sample.UserMapper.getById"], "strategies": ["bm25", "lexical_relations"]})
            assert compared.status_code == 200, compared.text
            assert compared.json()["annotation_source"] == "single_reviewer"
            assert compared.json()["strategies"][0]["cases"][0]["expected_symbols"] == ["sample.UserMapper.getById"]
            assert len((await client.get("/api/v1/retrieval/evaluations")).json()["items"]) == 1
            assert (await client.get(f"/api/v1/reviews/{review.review_run_id}/retrieval")).status_code == 200
            monkeypatch.setenv("OPENREVIEWER_RETRIEVAL_API_DISABLED", "true")
            assert (await client.get("/api/v1/retrieval/settings")).json()["external_calls_paused"] is True
            assert (await client.post("/api/v1/retrieval/indexes", json={"review_run_id": review.review_run_id})).status_code == 202
            assert (await client.post(f"/api/v1/retrieval/indexes/{index_id}/enrich")).status_code == 409
            targets = (await client.get("/api/v1/retrieval/targets")).json()
            assert targets["items"][0]["review_run_id"] == review.review_run_id
            assert (await client.get("/api/v1/retrieval/operations")).json()["available_indexes"] == 1
    asyncio.run(exercise())
