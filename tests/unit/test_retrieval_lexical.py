import math
import random
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from domain.retrieval import SearchQuery
from services.code_indexing import code_tokens
from services.retrieval_lexical import (
    LexicalIndex,
    LexicalSnapshotCache,
    _retained_size,
)


def corpus():
    rng = random.Random(42)
    return [(f"{n:04}", f"file{n % 5}.java", f"Type.method{n}",
        {word: count for word in ("alpha", "beta", "gamma", "delta") if (count := rng.randrange(5))}, n + 1, n + 3) for n in range(180)]


def reference(documents, query, limit):
    terms = sorted(code_tokens(query))
    average = max(1, sum(sum(row[3].values()) for row in documents) / len(documents))
    frequencies = {term: sum(term in row[3] for row in documents) for term in terms}
    hits = []
    for key, _, _, tokens, _, _ in documents:
        if not set(terms).intersection(tokens):
            continue
        score = 0.0
        for term in terms:
            if term in tokens:
                frequency = tokens[term]
                weight = math.log(1 + (len(documents) - frequencies[term] + .5) / (frequencies[term] + .5))
                score += weight * frequency * 2.5 / (frequency + 1.5 * (.25 + .75 * sum(tokens.values()) / average))
        hits.append((key, score))
    return sorted(hits, key=lambda item: (-item[1], item[0]))[:limit]


@pytest.mark.parametrize("query", ["alpha", "alpha beta", "gamma beta delta", "unknown", ""])
def test_optimized_bm25_preserves_scores_and_tie_breaking(query):
    documents = corpus()
    actual = LexicalIndex(documents, query_capacity=0).search(query, 20)
    expected = reference(documents, query, 20)
    assert [key for key, _ in actual] == [key for key, _ in expected]
    assert [score for _, score in actual] == pytest.approx([score for _, score in expected])


def test_query_cache_returns_independent_results_and_seed_lookup_is_scoped(monkeypatch):
    index = LexicalIndex(corpus())
    first = index.search("alpha beta", 8)
    expected = list(first)
    first.clear()
    monkeypatch.setattr("services.retrieval_lexical.code_tokens", lambda _: pytest.fail("cached query was recomputed"))
    assert index.search("alpha beta", 8) == expected
    seeds = index.seeds(SearchQuery(query="x", seed_files=("file2.java",)))
    assert seeds and all(index.documents[key][0] == "file2.java" for key in seeds)
    assert {row[0] for row in index.documents_for_files(("file2.java",))} == {"file2.java"}


def test_snapshot_cache_reuses_two_versions_and_evicts_by_memory():
    index = LexicalIndex(corpus())
    measured = _retained_size(index.documents, index.postings, index._files, index._symbols, index._order)
    assert index.estimated_bytes >= measured + 3 * 1024 * 1024
    cache = LexicalSnapshotCache(max_bytes=index.estimated_bytes * 2, capacity=4)
    loaded = []
    def load(key):
        loaded.append(key)
        return LexicalIndex(corpus())
    first = cache.get("a", lambda: load("a"))
    cache.get("b", lambda: load("b"))
    assert cache.get("a", lambda: load("a")) is first
    cache.get("c", lambda: load("c"))
    assert cache.retained_bytes <= cache.max_bytes
    cache.get("b", lambda: load("b"))
    assert loaded == ["a", "b", "c", "b"]


def test_concurrent_cold_loads_are_coalesced_without_blocking_warm_hits():
    cache = LexicalSnapshotCache()
    warm = cache.get("warm", lambda: LexicalIndex(corpus()))
    entered, release = Event(), Event()
    loaded = []
    def load():
        loaded.append(True)
        entered.set()
        assert release.wait(10)
        return LexicalIndex(corpus())
    with ThreadPoolExecutor(max_workers=5) as pool:
        first = pool.submit(cache.get, "cold", load)
        assert entered.wait(5)
        second = pool.submit(cache.get, "cold", load)
        try:
            assert pool.submit(cache.get, "warm", load).result(timeout=5) is warm
        finally:
            release.set()
        assert first.result(timeout=5) is second.result(timeout=5)
    assert len(loaded) == 1


def test_failed_and_oversized_loads_do_not_poison_the_cache():
    value = LexicalIndex(corpus())
    cache = LexicalSnapshotCache(max_bytes=value.estimated_bytes - 1)
    with pytest.raises(ValueError):
        cache.get("a", lambda: (_ for _ in ()).throw(ValueError("loader failed")))
    assert cache.get("a", lambda: value) is value
    assert cache.retained_bytes == 0
