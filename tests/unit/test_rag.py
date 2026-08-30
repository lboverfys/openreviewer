from pathlib import Path

import pytest
from sqlalchemy import event

import services.rag as rag_module
from persistence.database import Database
from persistence.models import Base
from services.rag import (
    KnowledgeConflictError,
    KnowledgeValidationError,
    ManagedMarkdownKnowledgeBase,
    MarkdownKnowledgeBase,
)


def test_knowledge_search_matches_versioned_source_name() -> None:
    knowledge = MarkdownKnowledgeBase("knowledge")

    first_chunks = knowledge.chunks()
    citations = knowledge.search("security database", limit=8)

    assert first_chunks
    assert knowledge.chunks() is first_chunks
    assert {item.source for item in citations} >= {
        "security.md",
        "database.md",
    }
    assert all(len(item.version) == 16 for item in citations)
    assert all(item.excerpt for item in citations)


def test_knowledge_search_reuses_chunk_token_index(monkeypatch: pytest.MonkeyPatch) -> None:
    knowledge = MarkdownKnowledgeBase("knowledge")
    original_tokens = rag_module._tokens
    calls = 0

    def counted_tokens(value: str) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return original_tokens(value)

    monkeypatch.setattr(rag_module, "_tokens", counted_tokens)
    knowledge.search("security database")
    first_search_calls = calls
    knowledge.search("security database")

    # 第二次查询只需重新切分查询词，不应再次扫描每个 chunk 的正文。
    assert calls - first_search_calls == 1


def _managed_knowledge(tmp_path: Path) -> tuple[Database, ManagedMarkdownKnowledgeBase]:
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    (seed_root / "security.md").write_text(
        "# 安全规则\n\n检查鉴权和密钥泄漏。\n",
        encoding="utf-8",
    )
    database = Database.connect(f"sqlite:///{(tmp_path / 'rag.sqlite3').as_posix()}")
    Base.metadata.create_all(database.engine)
    return database, ManagedMarkdownKnowledgeBase(database.sessions, seed_root)


def test_managed_knowledge_versions_archive_restore_and_search(tmp_path: Path) -> None:
    database, knowledge = _managed_knowledge(tmp_path)
    try:
        seeded = knowledge.list_documents()
        assert seeded.revision == 1
        assert seeded.total == seeded.enabled_count == 1
        assert knowledge.search("鉴权 密钥")[0].source == "security.md"

        created = knowledge.create_document(
            source="database.md",
            content="# 数据库规则\n\n禁止循环查询和无界全表读取。",
            enabled=True,
            expected_revision=seeded.revision,
            actor="tester",
        )
        assert created.revision == 2
        assert created.document.current_version == 1

        updated = knowledge.update_document(
            created.document.id,
            source="database.md",
            content="# 数据库规则\n\n禁止 N+1，并要求分页和索引。",
            enabled=True,
            expected_revision=created.revision,
            expected_document_version=1,
            actor="tester",
        )
        assert updated.document.current_version == 2
        assert [item.version for item in updated.document.versions] == [2, 1]
        assert knowledge.search("N+1 分页")[0].source == "database.md"

        restored_version = knowledge.restore_version(
            updated.document.id,
            1,
            expected_revision=updated.revision,
            expected_document_version=2,
            actor="reviewer",
        )
        assert restored_version.document.current_version == 3
        assert "无界全表读取" in restored_version.document.content

        archived = knowledge.archive_document(
            restored_version.document.id,
            archived=True,
            expected_revision=restored_version.revision,
            expected_document_version=3,
            actor="reviewer",
        )
        assert archived.document.archived is True
        assert archived.document.enabled is False
        assert not knowledge.search("无界全表读取")

        restored = knowledge.archive_document(
            archived.document.id,
            archived=False,
            expected_revision=archived.revision,
            expected_document_version=3,
            actor="reviewer",
        )
        assert restored.document.archived is False
        assert restored.document.enabled is False
    finally:
        database.dispose()


def test_managed_search_invalidates_index_after_content_update(tmp_path: Path) -> None:
    database, knowledge = _managed_knowledge(tmp_path)
    try:
        seeded = knowledge.list_documents()
        assert knowledge.search("鉴权")[0].source == "security.md"
        updated = knowledge.update_document(
            seeded.items[0].id,
            source="security.md",
            content="# 安全规则\n\n只检查新的审查词。",
            enabled=True,
            expected_revision=seeded.revision,
            expected_document_version=1,
            actor="tester",
        )
        assert updated.document.current_version == 2
        assert not knowledge.search("鉴权")
        assert knowledge.search("新的审查词")
    finally:
        database.dispose()


def test_managed_knowledge_rejects_stale_and_unsafe_updates(tmp_path: Path) -> None:
    database, knowledge = _managed_knowledge(tmp_path)
    try:
        seeded = knowledge.list_documents()
        with pytest.raises(KnowledgeValidationError, match="相对路径"):
            knowledge.create_document(
                source="../outside.md",
                content="# bad",
                enabled=False,
                expected_revision=seeded.revision,
                actor="tester",
            )
        with pytest.raises(KnowledgeConflictError, match="其他管理员"):
            knowledge.create_document(
                source="logic.md",
                content="# 逻辑",
                enabled=False,
                expected_revision=0,
                actor="tester",
            )
        with pytest.raises(KnowledgeValidationError, match="512 KiB"):
            knowledge.create_document(
                source="large.md",
                content="x" * (512 * 1024 + 1),
                enabled=False,
                expected_revision=seeded.revision,
                actor="tester",
            )
    finally:
        database.dispose()


def test_managed_knowledge_list_query_count_is_bounded(tmp_path: Path) -> None:
    database, knowledge = _managed_knowledge(tmp_path)
    try:
        revision = knowledge.list_documents().revision
        for index in range(8):
            result = knowledge.create_document(
                source=f"rules/rule-{index}.md",
                content=f"# 规则 {index}\n\n内容 {index}",
                enabled=index % 2 == 0,
                expected_revision=revision,
                actor="tester",
            )
            revision = result.revision

        select_count = 0

        def count_selects(_conn, _cursor, statement, _parameters, _context, _many):
            nonlocal select_count
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        event.listen(database.engine, "before_cursor_execute", count_selects)
        try:
            listed = knowledge.list_documents(limit=128)
        finally:
            event.remove(database.engine, "before_cursor_execute", count_selects)
        assert listed.total == 9
        assert select_count == 4
    finally:
        database.dispose()
