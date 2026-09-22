import json
from hashlib import sha256

import httpx
import pytest

from domain.enums import ModelProvider
from domain.retrieval import CodeChunk, RetrievalSettings, RetrievalTrace, stable_key
from services.model_review import (
    ModelServiceSettings,
    fit_model_context,
    plan_model_review_batches,
)
from services.retrieval_context import merge_contexts, select_contexts
from services.retrieval_indexing import embedding_batches
from services.retrieval_providers import AliyunRetrievalClient, RetrievalError
from tests.unit.test_model_review import make_model_input
from tests.unit.test_retrieval_evidence import evidence


def chunk(number, text):
    return CodeChunk(id=str(number), file=f"File{number}.java", blob_sha="a" * 40,
        language="java", kind="method", symbol=f"method{number}", start_line=1, end_line=1,
        content=text, content_hash=sha256(text.encode()).hexdigest())


def context(number, text):
    return evidence().model_copy(update={
        "reference_id": stable_key("index", str(number)), "chunk_id": str(number),
        "symbol": f"method{number}", "content": text,
        "content_hash": sha256(text.encode()).hexdigest(),
    })


@pytest.mark.parametrize(("field", "value"), [
    ("embedding_batch_max_bytes", 31_999), ("embedding_batch_max_bytes", 512_001),
    ("context_max_bytes", 3_999), ("context_max_bytes", 256_001),
])
def test_text_limit_validation(field, value):
    with pytest.raises(ValueError):
        RetrievalSettings(**{field: value})


def test_text_limits_preserve_index_and_vector_identity():
    original = RetrievalSettings()
    changed = original.model_copy(update={"embedding_batch_max_bytes": 128_000, "context_max_bytes": 48_000})
    assert original.embedding_fingerprint == changed.embedding_fingerprint
    assert original.index_key(1, 2, "a" * 40) == changed.index_key(1, 2, "a" * 40)


def test_embedding_packing_uses_utf8_bytes_and_keeps_twenty_item_limit():
    items = tuple(chunk(n, "中" * 2000) for n in range(25))
    assert [len(batch) for batch in embedding_batches(items, 64_000)] == [10, 10, 5]
    enlarged = tuple(embedding_batches(items, 128_000))
    assert [len(batch) for batch in enlarged] == [20, 5]
    assert [item.id for batch in enlarged for item in batch] == [item.id for item in items]
    assert all(sum(len(item.embedding_text.encode()) for item in batch) <= 128_000 for batch in enlarged)


def test_oversized_chunk_fails_instead_of_truncating_or_looping():
    with pytest.raises(RetrievalError, match="单个代码块"):
        tuple(embedding_batches((chunk(1, "中" * 2000),), 5000))


def test_provider_accepts_larger_configured_batch_but_enforces_both_limits():
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200, json={"data": [
            {"index": n, "embedding": [1.0] * 1024} for n in range(len(payload["input"]))
        ]})

    settings = RetrievalSettings(api_host="https://example.cn-beijing.maas.aliyuncs.com", embedding_batch_max_bytes=128_000)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = AliyunRetrievalClient(settings, "test-key", http)
        assert len(client.embed(tuple("x" * 5000 for _ in range(20))).vectors) == 20
        with pytest.raises(ValueError, match="128 KB"):
            client.embed(tuple("x" * 7000 for _ in range(20)))
        with pytest.raises(ValueError, match="1 到 20"):
            client.embed(tuple("x" for _ in range(21)))
    assert len(calls) == 1


def test_context_budget_counts_bytes_and_continues_with_smaller_candidates():
    candidates = (context(1, "中" * 2000), context(2, "中" * 2000), context(3, "x" * 1000))
    selected, budget = select_contexts(candidates, 20, 8000)
    assert [item.chunk_id for item in selected if item.selected] == ["1", "3"]
    assert budget.selected_bytes == 7000
    assert budget.excluded_by_size == 1
    expanded, budget = select_contexts(candidates, 20, 16_000)
    assert all(item.selected for item in expanded)
    assert budget.selected_bytes == 13_000 and budget.excluded_by_size == 0
    limited, budget = select_contexts(candidates, 1, 16_000)
    assert sum(item.selected for item in limited) == 1 and budget.excluded_by_size == 0


def test_agent_merge_applies_shared_budget_after_deduplication():
    items = (context(1, "x" * 5000), context(2, "x" * 5000))
    trace = RetrievalTrace(id="trace", index_id="index", query="query", strategy="bm25",
        candidates=items, routes=(), duration_ms=0)
    selected, budget = merge_contexts(((trace, ("unit-a",)), (trace, ("unit-b",))), 20, 8000)
    assert len(selected) == 2 and selected[0].unit_keys == ("unit-a", "unit-b")
    assert sum(item.selected for item in selected) == 1
    assert budget.selected_bytes == 5000 and budget.excluded_by_size == 1


def test_context_growth_respects_model_capacity_without_losing_changes():
    source = make_model_input()
    original_patch = source.units[0].patch
    source = source.model_copy(update={"context_evidence": tuple(context(n, "中" * 2000) for n in range(20))})
    settings = ModelServiceSettings(provider=ModelProvider.OPENAI, model="test-model", api_key="test-key",
        context_window_tokens=32_768, max_batch_input_tokens=12_000, max_output_tokens=4096)
    fitted = fit_model_context(source, settings)
    assert 0 < len(fitted.context_evidence) < 20
    assert fitted.context_evidence[0].reference_id == source.context_evidence[0].reference_id
    assert fitted.units == source.units and fitted.rules == source.rules
    batches = plan_model_review_batches(source, settings)
    assert "".join(unit.patch for batch in batches for unit in batch.review_input.units) == original_patch
    assert all(batch.estimated_input_tokens <= settings.batch_input_budget_tokens for batch in batches)
    assert all(batch.review_input.context_evidence == fitted.context_evidence for batch in batches)
    assert fit_model_context(fitted, settings) == fitted
