import asyncio
from pathlib import Path

import httpx

from apps.api.main import create_app
from persistence.database import Database
from persistence.models import Base
from services.rag import ManagedMarkdownKnowledgeBase
from tests.support import TEST_PASSWORD, TEST_USERNAME, make_auth_service


def test_authenticated_knowledge_management_api(tmp_path: Path) -> None:
    seed_root = tmp_path / "knowledge"
    seed_root.mkdir()
    (seed_root / "security.md").write_text(
        "# 安全规则\n\n检查鉴权与敏感数据。\n",
        encoding="utf-8",
    )
    database = Database.connect(
        f"sqlite:///{(tmp_path / 'knowledge-api.sqlite3').as_posix()}"
    )
    Base.metadata.create_all(database.engine)
    knowledge = ManagedMarkdownKnowledgeBase(database.sessions, seed_root)
    application = create_app(
        auth_service=make_auth_service(),
        knowledge_base=knowledge,
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            assert (await client.get("/api/v1/knowledge/documents")).status_code == 401
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
            )
            assert login.status_code == 200

            listed = await client.get("/api/v1/knowledge/documents")
            assert listed.status_code == 200
            assert listed.json()["revision"] == 1
            assert listed.json()["items"][0]["source"] == "security.md"

            created = await client.post(
                "/api/v1/knowledge/documents",
                json={
                    "expected_revision": 1,
                    "source": "database.md",
                    "content": "# 数据库规则\n\n禁止 N+1 查询。",
                    "enabled": True,
                },
            )
            assert created.status_code == 201
            body = created.json()
            document_id = body["document"]["id"]
            assert body["revision"] == 2
            assert body["document"]["current_version"] == 1

            stale = await client.put(
                f"/api/v1/knowledge/documents/{document_id}",
                json={
                    "expected_revision": 1,
                    "expected_document_version": 1,
                    "source": "database.md",
                    "content": "# 数据库规则\n\n新的内容。",
                    "enabled": True,
                },
            )
            assert stale.status_code == 409

            updated = await client.put(
                f"/api/v1/knowledge/documents/{document_id}",
                json={
                    "expected_revision": 2,
                    "expected_document_version": 1,
                    "source": "database.md",
                    "content": "# 数据库规则\n\n禁止 N+1 查询并要求分页。",
                    "enabled": True,
                },
            )
            assert updated.status_code == 200
            assert updated.json()["document"]["current_version"] == 2

            searched = await client.get(
                "/api/v1/knowledge/search",
                params={"q": "N+1 分页", "limit": 8},
            )
            assert searched.status_code == 200
            assert searched.json()["items"][0]["source"] == "database.md"

            archived = await client.post(
                f"/api/v1/knowledge/documents/{document_id}/archive",
                json={
                    "expected_revision": 3,
                    "expected_document_version": 2,
                },
            )
            assert archived.status_code == 200
            assert archived.json()["document"]["archived"] is True

            with_archived = await client.get(
                "/api/v1/knowledge/documents",
                params={"include_archived": "true"},
            )
            assert with_archived.status_code == 200
            assert with_archived.json()["total"] == 2

            restored = await client.post(
                f"/api/v1/knowledge/documents/{document_id}/restore",
                json={
                    "expected_revision": 4,
                    "expected_document_version": 2,
                },
            )
            assert restored.status_code == 200
            assert restored.json()["document"]["enabled"] is False

            restored_version = await client.post(
                f"/api/v1/knowledge/documents/{document_id}/versions/1/restore",
                json={
                    "expected_revision": 5,
                    "expected_document_version": 2,
                },
            )
            assert restored_version.status_code == 200
            assert restored_version.json()["document"]["current_version"] == 3

    try:
        asyncio.run(exercise())
    finally:
        database.dispose()


def test_knowledge_mutations_reject_cross_origin_requests(tmp_path: Path) -> None:
    seed_root = tmp_path / "knowledge-origin"
    seed_root.mkdir()
    (seed_root / "security.md").write_text(
        "# 安全规则\n\n检查鉴权。\n",
        encoding="utf-8",
    )
    database = Database.connect(
        f"sqlite:///{(tmp_path / 'knowledge-origin.sqlite3').as_posix()}"
    )
    Base.metadata.create_all(database.engine)
    application = create_app(
        auth_service=make_auth_service(),
        knowledge_base=ManagedMarkdownKnowledgeBase(database.sessions, seed_root),
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
            )
            assert login.status_code == 200

            rejected = await client.post(
                "/api/v1/knowledge/documents",
                headers={"Origin": "https://attacker.example"},
                json={
                    "expected_revision": 1,
                    "source": "database.md",
                    "content": "# 数据库规则\n\n禁止 N+1 查询。",
                    "enabled": True,
                },
            )
            assert rejected.status_code == 403
            assert rejected.json() == {"detail": "cross-origin request rejected"}

            listed = await client.get("/api/v1/knowledge/documents")
            assert listed.status_code == 200
            assert listed.json()["revision"] == 1
            assert listed.json()["total"] == 1

            accepted = await client.post(
                "/api/v1/knowledge/documents",
                headers={"Origin": "http://testserver"},
                json={
                    "expected_revision": 1,
                    "source": "database.md",
                    "content": "# 数据库规则\n\n禁止 N+1 查询。",
                    "enabled": True,
                },
            )
            assert accepted.status_code == 201

    try:
        asyncio.run(exercise())
    finally:
        database.dispose()
