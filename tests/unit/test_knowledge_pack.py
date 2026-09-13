import json
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import event

from apps.maintenance.sync_knowledge_pack import sync_pack
from persistence.database import Database
from persistence.models import Base
from services.rag import (
    KnowledgeConflictError,
    ManagedMarkdownKnowledgeBase,
    MarkdownKnowledgeBase,
    merge_review_citations,
)


@pytest.fixture
def pack(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    old = {"rule.md": "# 旧规则\n\n旧约束。\n", "retired.md": "# 旧记录\n\n过期说明。\n"}
    for source, content in old.items():
        (root / source).write_text(content, encoding="utf-8")
    db = Database.connect(f"sqlite:///{(tmp_path / 'knowledge.sqlite3').as_posix()}")
    Base.metadata.create_all(db.engine)
    library = ManagedMarkdownKnowledgeBase(db.sessions, root)
    view = library.list_documents()
    (root / "rule.md").write_text("# 新规则\n\n能判断实际问题的约束。\n", encoding="utf-8")
    (root / "new.md").write_text("# 新知识\n\n重试不能重复扣款。\n", encoding="utf-8")
    (root / "curation-pack.json").write_text(json.dumps({
        "sources": ["rule.md", "new.md"],
        "previous": {name: [sha256(content.encode()).hexdigest()] for name, content in old.items()},
        "scopes": {name: None for name in old},
    }), encoding="utf-8")
    try:
        yield db, library, root, view
    finally:
        db.dispose()


def test_preview_is_read_only_and_apply_preserves_history(pack):
    db, library, root, before = pack
    sql = []
    event.listen(db.engine, "before_cursor_execute", lambda c, cur, statement, p, ctx, many: sql.append(statement))
    preview = sync_pack(db.sessions, root)
    assert len(sql) == 2 and all(statement.lstrip().upper().startswith("SELECT") for statement in sql)
    assert (preview["create"], preview["update"], preview["archive"]) == (["new.md"], ["rule.md"], ["retired.md"])
    applied = sync_pack(db.sessions, root, apply=True, expected_revision=before.revision)
    assert applied["applied"] and applied["revision"] == before.revision + 1
    current = library.list_documents(include_archived=True)
    by_source = {item.source: item for item in current.items}
    assert by_source["retired.md"].archived and not by_source["retired.md"].enabled
    rule = library.get_document(by_source["rule.md"].id)
    assert rule.current_version == 2 and len(rule.versions) == 2
    assert not library.search("过期说明")
    repeat = sync_pack(db.sessions, root, apply=True, expected_revision=current.revision)
    assert not repeat["applied"] and repeat["revision"] == current.revision


@pytest.mark.parametrize("source", ["rule.md", "retired.md"])
def test_manual_edit_aborts_the_whole_pack(pack, source):
    db, library, root, before = pack
    document = next(item for item in before.items if item.source == source)
    edited = library.update_document(document.id, source=source, content="# 人工编辑\n\n保留我的规则。",
        enabled=True, expected_revision=before.revision, expected_document_version=1, actor="owner")
    assert sync_pack(db.sessions, root)["conflicts"] == [source]
    with pytest.raises(KnowledgeConflictError):
        sync_pack(db.sessions, root, apply=True, expected_revision=edited.revision)
    after = library.list_documents(include_archived=True)
    assert after.revision == edited.revision and after.total == before.total
    assert not any(item.archived for item in after.items)
    assert library.get_document(document.id).content == edited.document.content


def test_stale_preview_and_changed_scope_cannot_be_overwritten(pack):
    db, library, root, before = pack
    document = next(item for item in before.items if item.source == "retired.md")
    edited = library.update_document(document.id, source=document.source,
        content=library.get_document(document.id).content, enabled=True,
        expected_revision=before.revision, expected_document_version=1, actor="owner",
        repository_scope="other/repo", update_repository_scope=True)
    with pytest.raises(KnowledgeConflictError, match="重新预览"):
        sync_pack(db.sessions, root, apply=True, expected_revision=before.revision)
    assert sync_pack(db.sessions, root)["conflicts"] == ["retired.md"]
    assert library.list_documents().revision == edited.revision


@pytest.mark.parametrize("count", [2, 32])
def test_pack_uses_bulk_queries_and_keeps_disabled_state(tmp_path, count):
    root = tmp_path / "bulk"
    root.mkdir()
    previous = {}
    for index in range(count):
        source = f"{index:02}.md"
        value = f"# 规则 {index}\n\n旧约束。\n"
        (root / source).write_text(value, encoding="utf-8")
        previous[source] = [sha256(value.encode()).hexdigest()]
    db = Database.connect(f"sqlite:///{(tmp_path / 'bulk.sqlite3').as_posix()}")
    Base.metadata.create_all(db.engine)
    try:
        library = ManagedMarkdownKnowledgeBase(db.sessions, root)
        before = library.list_documents()
        first = before.items[0]
        disabled = library.update_document(first.id, source=first.source,
            content=library.get_document(first.id).content, enabled=False,
            expected_revision=before.revision, expected_document_version=1, actor="owner")
        for source in previous:
            (root / source).write_text("# 新规则\n\n保持真实业务约束。\n", encoding="utf-8")
        (root / "curation-pack.json").write_text(json.dumps({
            "sources": list(previous), "previous": previous, "scopes": dict.fromkeys(previous),
        }), encoding="utf-8")
        sql = []
        event.listen(db.engine, "before_cursor_execute", lambda c, cur, statement, p, ctx, many: sql.append(statement))
        result = sync_pack(db.sessions, root, apply=True, expected_revision=disabled.revision)
        assert result["applied"] and len(result["update"]) == count
        assert len(sql) == 6
        assert not library.get_document(first.id).enabled
    finally:
        db.dispose()


def test_scope_is_metadata_and_not_a_standalone_retrieval_hit(tmp_path: Path):
    (tmp_path / "rule.md").write_text(
        "# 订单规则\n\n适用仓库：owner/repo\n\n## 付款\n\n重复请求不能再次扣费。\n", encoding="utf-8",
    )
    chunks = MarkdownKnowledgeBase(tmp_path).chunks()
    assert len(chunks) == 1
    assert chunks[0].repository_scope == "owner/repo"
    assert chunks[0].content == "重复请求不能再次扣费。"


@pytest.mark.parametrize("identifier", ["ProfileTagDeletionWorkflowServiceImpl.java", "profile_tag_id"])
def test_code_identifiers_find_self_contained_business_rules(tmp_path, identifier):
    (tmp_path / "tags.md").write_text("# 个人标签（ProfileTag）\n\n选择与删除必须互斥。\n", encoding="utf-8")
    (tmp_path / "architecture.md").write_text("# 模块边界\n\nService 与 Workflow 维护各自的边界。\n", encoding="utf-8")
    hits = MarkdownKnowledgeBase(tmp_path).search(identifier, limit=2)
    tag = next(item for item in hits if item.source == "tags.md")
    assert tag.excerpt == "选择与删除必须互斥。"


@pytest.mark.parametrize(("filenames", "expected_heading"), [
    ("ProfileTagDeletionWorkflowServiceImpl ProfileTagMapper", "个人标签"),
    ("CompanionGameServiceImpl", "管理员授权"),
    ("RefundServiceImpl", "退款"),
])
def test_review_topics_are_not_displaced_by_general_role_rules(filenames, expected_heading):
    library = MarkdownKnowledgeBase("knowledge")
    topics = library.search(filenames, limit=4)
    responsibilities = library.search("logic reliability business database correctness 逻辑 可靠性 数据库", limit=8)
    selected = merge_review_citations(topics, responsibilities)
    assert any(expected_heading in item.heading for item in selected)
    assert len(selected) <= 8
    assert len({(item.source, item.heading, item.excerpt) for item in selected}) == len(selected)
