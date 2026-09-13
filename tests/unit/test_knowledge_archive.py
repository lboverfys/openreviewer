from datetime import UTC, datetime, timedelta

import pytest

from persistence.database import Database
from persistence.models import Base
from services.rag import KnowledgeValidationError, ManagedMarkdownKnowledgeBase


@pytest.fixture
def archive_library(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "a.md").write_text("# A\n" + "a" * 600 + "\n", encoding="utf-8")
    (root / "b.md").write_text("# B\n" + "b" * 300 + "\n", encoding="utf-8")
    db = Database.connect(f"sqlite:///{(tmp_path / 'knowledge.sqlite3').as_posix()}")
    Base.metadata.create_all(db.engine)
    now = [datetime(2026, 9, 14, tzinfo=UTC)]
    library = ManagedMarkdownKnowledgeBase(db.sessions, root, max_file_bytes=1024, max_total_bytes=1024, clock=lambda: now[0])
    try:
        yield library, now
    finally:
        db.dispose()


def test_removed_documents_are_separately_paged_newest_first(archive_library):
    library, now = archive_library
    before = library.list_documents()
    first, second = before.items
    one = library.archive_document(first.id, archived=True, expected_revision=before.revision,
        expected_document_version=1, actor="owner")
    now[0] += timedelta(seconds=1)
    library.archive_document(second.id, archived=True, expected_revision=one.revision,
        expected_document_version=1, actor="owner")
    page = library.list_documents(archived_only=True, limit=1)
    assert page.total == 2 and page.has_more
    assert page.items[0].id == second.id
    assert library.list_documents(archived_only=True, limit=1, offset=1).items[0].id == first.id
    assert library.list_documents().total == 0


def test_restore_and_enable_is_one_state_change(archive_library):
    library, _ = archive_library
    before = library.list_documents()
    document = before.items[0]
    removed = library.archive_document(document.id, archived=True, expected_revision=before.revision,
        expected_document_version=1, actor="owner")
    restored = library.archive_document(document.id, archived=False, restore_enabled=True,
        expected_revision=removed.revision, expected_document_version=1, actor="owner")
    assert restored.revision == removed.revision + 1
    assert restored.document.enabled and not restored.document.archived
    assert restored.document.current_version == 1
    assert library.list_documents(archived_only=True).total == 0


def test_restore_capacity_failure_keeps_document_removed(archive_library):
    library, _ = archive_library
    before = library.list_documents()
    document = before.items[0]
    removed = library.archive_document(document.id, archived=True, expected_revision=before.revision,
        expected_document_version=1, actor="owner")
    other = library.create_document(source="c.md", content="# C\n" + "c" * 600, enabled=True,
        expected_revision=removed.revision, actor="owner")
    with pytest.raises(KnowledgeValidationError):
        library.archive_document(document.id, archived=False, restore_enabled=True,
            expected_revision=other.revision, expected_document_version=1, actor="owner")
    assert library.get_document(document.id).archived
    assert library.list_documents().revision == other.revision
