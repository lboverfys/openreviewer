"""验证分页边界、资源隔离和查询次数，不请求外部模型或服务器。"""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import event, insert

from persistence.dashboard import SqlAlchemyDashboardRepository
from persistence.models import (
    CodeIndexRecord,
    ConfigurationAuditRecord,
    KnowledgeDocumentRecord,
    KnowledgeDocumentVersionRecord,
    OutboxEventRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.retrieval import RetrievalRepository
from persistence.review_management import SqlAlchemyReviewManagementRepository
from services.ai_settings import AiSecretCipher, AiSettingsService
from services.dashboard import DashboardService
from services.rag import ManagedMarkdownKnowledgeBase
from services.rbac import ResourceScope
from tests.integration.test_finding_pagination import (
    _application,
    _login,
    _seed_review,
)
from tests.integration.test_management_api import database as database

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def seed_runs(database, count=23):
    runs = [{
        "id": f"run-{number:04}", "review_version_key": f"42:{number + 1}:{'a' * 40}",
        "installation_id": 10, "repository_id": 42,
        "repository": "lboverfys/NiuMa", "repository_key": "lboverfys/niuma",
        "pull_request_number": number + 1, "head_sha": "a" * 40,
        "execution_status": "queued" if number % 2 else "completed",
        "workflow_status": "queued" if number % 2 else "completed",
        "coverage_status": "complete", "idempotency_key": f"seed-{number}",
        "request_fingerprint": "b" * 64, "created_at": NOW, "updated_at": NOW,
    } for number in range(count)]
    with database.sessions() as session, session.begin():
        session.execute(insert(ReviewRunRecord), runs)
        session.execute(insert(ReviewTaskRecord), [{
            "id": f"task-{row['id']}", "review_run_id": row["id"],
            "execution_status": row["execution_status"],
        } for row in runs])
    return runs


def test_review_pages_have_stable_boundaries_and_global_filters(database):
    seed_runs(database)
    service = DashboardService(SqlAlchemyDashboardRepository(database.sessions))
    first = service.snapshot(10, include_overview=False)
    second = service.snapshot(10, first.next_cursor, include_overview=False)
    third = service.snapshot(10, second.next_cursor, include_overview=False)
    assert [len(page.recent_reviews) for page in (first, second, third)] == [10, 10, 3]
    assert len({item.review_run_id for page in (first, second, third) for item in page.recent_reviews}) == 23
    assert first.next_cursor and second.next_cursor and third.next_cursor is None
    assert first.total_reviews == 23
    assert not first.workers
    # PR #1 在第三页，搜索仍能直接找到，不能仅筛选已加载的第一页。
    found = service.snapshot(10, query="1", include_overview=False)
    assert found.total_reviews == 1
    assert found.recent_reviews[0].pull_request_number == 1
    assert service.snapshot(10, query="niuma", include_overview=False).total_reviews == 23
    denied = service.snapshot(10, scope=ResourceScope(repositories=frozenset({"other/repo"})), include_overview=False)
    assert denied.total_reviews == 0 and not denied.recent_reviews
    with pytest.raises(ValueError):
        service.snapshot(10, cursor="broken", include_overview=False)


def test_review_page_queries_are_constant_and_skip_worker_overview(database):
    seed_runs(database)
    repository = SqlAlchemyDashboardRepository(database.sessions)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        repository.load(1, include_overview=False)
        small_count = len(statements)
        statements.clear()
        repository.load(20, include_overview=False)
        assert len(statements) == small_count == 3
        assert not any("worker_heartbeats" in statement for statement in statements)
        assert any("LIMIT" in statement for statement in statements)
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)


def test_findings_and_event_endpoints_page_without_loading_full_details(database, monkeypatch):
    run_id = _seed_review(database, finding_count=25)
    with database.sessions() as session, session.begin():
        session.execute(insert(OutboxEventRecord), [{
            "id": f"event-{number:04}", "event_key": f"event-{number}",
            "aggregate_type": "review_run", "aggregate_id": run_id,
            "event_type": "review.model.batch_failed" if number % 2 else "review.workflow.advance",
            "payload": {"message": "example"}, "occurred_at": NOW,
        } for number in range(23)])

    def unexpected_details(*_args, **_kwargs):
        raise AssertionError("翻页不能重新读取完整详情")

    monkeypatch.setattr(SqlAlchemyReviewManagementRepository, "get", unexpected_details)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_application(database)), base_url="http://testserver") as client:
            await _login(client)
            finding_url = f"/api/v1/reviews/{run_id}/findings"
            first = (await client.get(finding_url)).json()
            second = (await client.get(finding_url, params={"cursor": first["next_cursor"]})).json()
            third = (await client.get(finding_url, params={"cursor": second["next_cursor"]})).json()
            assert [len(page["items"]) for page in (first, second, third)] == [10, 10, 5]
            assert len({item["id"] for page in (first, second, third) for item in page["items"]}) == 25
            assert third["next_cursor"] is None
            event_url = f"/api/v1/reviews/{run_id}/events"
            first_events = (await client.get(event_url)).json()
            second_events = (await client.get(event_url, params={"cursor": first_events["next_cursor"]})).json()
            assert len(first_events["items"]) == len(second_events["items"]) == 10
            assert not ({item["id"] for item in first_events["items"]} & {item["id"] for item in second_events["items"]})
            errors = (await client.get(event_url, params={"event_filter": "errors"})).json()
            assert all(item["event_type"].endswith("failed") for item in errors["items"])
            assert (await client.get(event_url, params={"cursor": "broken"})).status_code == 422
            assert (await client.get("/api/v1/reviews/missing/events")).status_code == 404

    asyncio.run(exercise())


def test_index_pages_use_stable_keys_and_preserve_scope(database):
    seed_runs(database)
    with database.sessions() as session, session.begin():
        session.execute(insert(CodeIndexRecord), [{
            "id": f"index-{number:04}", "installation_id": 10, "repository_id": 42,
            "repository": "lboverfys/niuma", "head_sha": "a" * 40,
            "configuration_key": "test", "embedding_model": "test", "created_at": NOW,
        } for number in range(23)])
    repository = RetrievalRepository(database.sessions)
    first = repository.index_page(None)
    second = repository.index_page(None, cursor=first.next_cursor)
    third = repository.index_page(None, cursor=second.next_cursor)
    assert [len(page.items) for page in (first, second, third)] == [10, 10, 3]
    assert len({item.id for page in (first, second, third) for item in page.items}) == 23
    targets = repository.target_page(None)
    assert len(targets.items) == 10 and targets.next_cursor
    assert len(repository.target_page(None, cursor=targets.next_cursor).items) == 10
    assert not repository.index_page(ResourceScope(repositories=frozenset({"other/repo"}))).items


def test_knowledge_and_history_are_paged_and_history_does_not_read_bodies(database, tmp_path):
    seed_root = tmp_path / "empty-knowledge"
    seed_root.mkdir()
    library = ManagedMarkdownKnowledgeBase(database.sessions, seed_root)
    library.list_documents()
    with database.sessions() as session, session.begin():
        session.execute(insert(KnowledgeDocumentRecord), [{
            "id": f"document-{number:04}", "source": f"rule-{number:04}.md", "enabled": False,
            "current_version": 23 if number == 0 else 1, "created_by": "test", "updated_by": "test",
        } for number in range(23)])
        session.execute(insert(KnowledgeDocumentVersionRecord), [{
            "id": f"version-{number:04}", "document_id": f"document-{number:04}",
            "version": 23 if number == 0 else 1, "content": f"# Rule {number}\n\nTest.",
            "content_sha256": "a" * 64, "byte_size": 20, "created_by": "test",
        } for number in range(23)] + [{
            "id": f"history-{version:04}", "document_id": "document-0000", "version": version,
            "content": "# old\n" + "x" * 16000, "content_sha256": "b" * 64,
            "byte_size": 16006, "created_by": "test",
        } for version in range(1, 23)])
    assert library.list_documents().has_more
    assert len(library.list_documents(offset=20).items) == 3
    assert library.list_documents(query="rule-0022").items[0].id == "document-0022"
    first = library.get_document("document-0000")
    second = library.get_document("document-0000", version_cursor=int(first.version_next_cursor))
    third = library.get_document("document-0000", version_cursor=int(second.version_next_cursor))
    assert [len(page.versions) for page in (first, second, third)] == [10, 10, 3]
    assert third.version_next_cursor is None


def test_configuration_audits_use_revision_cursor(database):
    with database.sessions() as session, session.begin():
        session.execute(insert(ConfigurationAuditRecord), [{
            "id": f"audit-{revision}", "revision": revision, "actor": "test", "action": "update", "changed_fields": ["model"],
        } for revision in range(1, 24)])
    service = AiSettingsService(database.sessions, AiSecretCipher(b"k" * 32))
    first = service.audits(11)
    second = service.audits(11, first[9].revision)
    assert [item.revision for item in first[:10]] == list(range(23, 13, -1))
    assert [item.revision for item in second[:10]] == list(range(13, 3, -1))
