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
            queued = await client.post("/api/v1/retrieval/indexes", json={"review_run_id": review.review_run_id})
            assert queued.status_code == 202
            index_id = queued.json()["id"]
            assert (await client.post("/api/v1/retrieval/indexes", json={"review_run_id": "not-a-review"})).status_code == 404
            service.index_sources(TARGET, sources())
            searched = await client.post(f"/api/v1/retrieval/indexes/{index_id}/search", json={"query": "find user SQL", "strategy": "reranked"})
            assert searched.status_code == 200, searched.text
            assert searched.json()["candidates"]
            assert (await client.get("/api/v1/retrieval/evaluations")).json() == []
            assert (await client.get(f"/api/v1/reviews/{review.review_run_id}/retrieval")).status_code == 200
            monkeypatch.setenv("OPENREVIEWER_RETRIEVAL_API_DISABLED", "true")
            assert (await client.get("/api/v1/retrieval/settings")).json()["external_calls_paused"] is True
            assert (await client.post("/api/v1/retrieval/indexes", json={"review_run_id": review.review_run_id})).status_code == 409
            assert (await client.post(f"/api/v1/retrieval/indexes/{index_id}/retry")).status_code == 409
    asyncio.run(exercise())
