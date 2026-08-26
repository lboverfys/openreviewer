from services.rag import MarkdownKnowledgeBase


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
