"""独立备份副本上的有界 HTTP 并发回放；模型始终使用内存模拟。

服务和客户端分别运行在限额容器中，通过同一隔离网络的回环地址通信。
仅供人工性能验证，不由 pytest 或生产应用导入。
"""

import argparse
import asyncio
import json
import math
import os
import resource
import statistics
import time
from collections import Counter
from pathlib import Path
from threading import Lock

import httpx
import uvicorn
from sqlalchemy import event, func, select

from apps.api.main import create_app
from domain.retrieval import RetrievalSettings, RetrievalSettingsView
from persistence.auth import SqlAlchemySessionStore
from persistence.database import Database
from persistence.models import (
    CodeEmbeddingRecord,
    CodeIndexChunkRecord,
    RetrievalSettingsRecord,
    RetrievalTraceRecord,
)
from persistence.retrieval import RetrievalRepository
from services.retrieval import HybridRetrievalService
from services.retrieval_lexical import LexicalSnapshotCache
from services.retrieval_providers import EmbeddingResult, RerankResult
from tests.support import TEST_PASSWORD, TEST_USERNAME, make_auth_service

INDEX_ID = "faa7a64cad5bcf491ff8dd72d0672e79e2db091ce40c5490c56b116f80f3f251"
CASES = json.loads(Path("tests/evaluation/retrieval_niuma.json").read_text())["cases"]


def serve():
    assert os.environ.get("OPENREVIEWER_BENCHMARK_DISPOSABLE") == "true"
    assert os.environ.get("OPENREVIEWER_RETRIEVAL_API_DISABLED") == "true"
    db = Database.from_environment()
    assert db.engine.url.database == "openreviewer_perf" and db.engine.url.host == "127.0.0.1"
    repo = RetrievalRepository(db.sessions)
    with db.sessions() as session:
        vectors = tuple(tuple(float(x) for x in row) for row in session.scalars(select(CodeEmbeddingRecord.embedding).join(
            CodeIndexChunkRecord, CodeIndexChunkRecord.embedding_id == CodeEmbeddingRecord.id,
        ).where(CodeIndexChunkRecord.index_id == INDEX_ID).order_by(CodeIndexChunkRecord.chunk_id).limit(20)))
        traces_before = session.scalar(select(func.count()).select_from(RetrievalTraceRecord))
    assert len(vectors) == 20
    counters = Counter()
    lock = Lock()

    class OfflineSettings:
        def runtime(self):
            # 只读适配器不解密真实Key；注入模拟客户端，进程的真实API暂停开关不变。
            # 每次仍读取数据库配置，保留正式检索路径的查询次数。
            with db.sessions() as session:
                current = RetrievalSettings.model_validate(session.scalar(select(RetrievalSettingsRecord.settings).where(RetrievalSettingsRecord.id == 1)))
            return RetrievalSettingsView(revision=0, settings=current, key_configured=True, external_calls_paused=False), "offline-fixture-only"

    class FakeModels:
        def __init__(self, settings, key):
            assert key == "offline-fixture-only"

        def close(self):
            pass

        def embed(self, texts):
            with lock:
                counters["fake_embedding_calls"] += 1
            time.sleep(0.02)
            return EmbeddingResult(tuple(vectors[sum(text.encode()) % len(vectors)] for text in texts), 20, None)

        def rerank(self, query, texts):
            with lock:
                counters["fake_rerank_calls"] += 1
            time.sleep(0.04)
            return RerankResult(tuple((i, 1 / (i + 1)) for i in range(len(texts))), 40, None)

    original_loader = repo.lexical_documents

    def lexical_documents(index_id):
        with lock:
            counters["lexical_loads"] += 1
        yield from original_loader(index_id)

    repo.lexical_documents = lexical_documents
    service = HybridRetrievalService(repo, OfflineSettings(), client_factory=FakeModels)
    app = create_app(retrieval_service=service, auth_service=make_auth_service(session_store=SqlAlchemySessionStore(db.sessions)))

    @event.listens_for(db.engine, "before_cursor_execute")
    def count_sql(*args):
        with lock:
            counters["sql_statements"] += 1

    @app.post("/benchmark/reset")
    def reset(cold: bool = False):
        if cold:
            service._lexical_cache = LexicalSnapshotCache()
        with lock:
            counters.clear()
        return {"reset": True}

    @app.get("/benchmark/stats")
    def stats():
        with lock:
            result = dict(counters)
        with db.sessions() as session:
            traces_after = session.scalar(select(func.count()).select_from(RetrievalTraceRecord))
        result.update(manual_trace_growth=traces_after - traces_before,
            peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            checked_out_connections=db.engine.pool.checkedout(), real_model_requests=0)
        return result

    uvicorn.run(app, host="127.0.0.1", port=18090, access_log=False, log_level="warning")


async def run(output: Path, sustained_seconds: int):
    url = "http://127.0.0.1:18090"
    async with httpx.AsyncClient(base_url=url, timeout=30, limits=httpx.Limits(max_connections=20)) as client:
        response = await client.post("/api/v1/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
        response.raise_for_status()
        path = f"/api/v1/retrieval/indexes/{INDEX_ID}/search"
        reports = []

        async def batch(name, concurrency, total, strategy, *, cold=False, unique=False):
            (await client.post("/benchmark/reset", params={"cold": str(cold).lower()})).raise_for_status()
            gate = asyncio.Semaphore(concurrency)

            async def request(n):
                case = CASES[n % len(CASES)]
                query = case["query"] + (f" request_unique_{concurrency}_{n}" if unique else "")
                payload = {"query": query, "strategy": strategy, "seed_files": case["seed_files"], "symbols": case["symbols"]}
                async with gate:
                    start = time.perf_counter()
                    try:
                        response = await client.post(path, json=payload)
                        data = response.json()
                        return {"duration_ms": (time.perf_counter() - start) * 1000, "status": response.status_code,
                            "strategy": data.get("strategy"), "requested": strategy, "candidates": len(data.get("candidates", []))}
                    except httpx.HTTPError:
                        return {"duration_ms": (time.perf_counter() - start) * 1000, "status": 0, "strategy": None, "requested": strategy, "candidates": 0}

            start = time.perf_counter()
            samples = await asyncio.gather(*(request(n) for n in range(total)))
            elapsed = time.perf_counter() - start
            timings = sorted(sample["duration_ms"] for sample in samples)
            stats = (await client.get("/benchmark/stats")).json()
            report = {"name": name, "concurrency": concurrency, "requests": total,
                "median_ms": statistics.median(timings), "p95_ms": timings[math.ceil(len(timings) * .95) - 1],
                "requests_per_second": total / elapsed,
                "failures": sum(sample["status"] != 200 or sample["candidates"] == 0 for sample in samples),
                "degraded": sum(sample["strategy"] != strategy for sample in samples), "stats": stats}
            reports.append(report)
            print(json.dumps(report), flush=True)

        await batch("cold_lexical", 16, 16, "bm25", cold=True)
        for case in CASES:
            response = await client.post(path, json={"query": case["query"], "strategy": "reranked", "seed_files": case["seed_files"], "symbols": case["symbols"]})
            assert response.status_code == 200 and response.json()["strategy"] == "reranked"
        for strategy in ("bm25", "lexical_relations", "reranked"):
            for concurrency in (1, 4, 8, 16):
                await batch("warm_" + strategy, concurrency, 40, strategy)
        await batch("uncached_queries", 16, 40, "bm25", unique=True)
        async def sustained(strategy):
            (await client.post("/benchmark/reset")).raise_for_status()
            deadline = time.perf_counter() + sustained_seconds
            timings = []
            failures = degraded = 0

            async def sustained_worker(offset):
                nonlocal failures, degraded
                n = offset
                while time.perf_counter() < deadline:
                    case = CASES[n % len(CASES)]
                    start = time.perf_counter()
                    response = await client.post(path, json={"query": case["query"], "strategy": strategy,
                        "seed_files": case["seed_files"], "symbols": case["symbols"]})
                    timings.append((time.perf_counter() - start) * 1000)
                    failures += response.status_code != 200
                    degraded += response.json().get("strategy") != strategy
                    n += 1

            start = time.perf_counter()
            await asyncio.gather(*(sustained_worker(n) for n in range(16)))
            elapsed = time.perf_counter() - start
            timings.sort()
            report = {"name": "sustained_" + strategy, "concurrency": 16, "requests": len(timings),
                "elapsed_seconds": elapsed, "median_ms": statistics.median(timings),
                "p95_ms": timings[math.ceil(len(timings) * .95) - 1], "requests_per_second": len(timings) / elapsed,
                "failures": failures, "degraded": degraded, "stats": (await client.get("/benchmark/stats")).json()}
            reports.append(report)
            print(json.dumps(report), flush=True)

        for strategy in ("bm25", "reranked"):
            await sustained(strategy)
        output.write_text(json.dumps({"index_id": INDEX_ID, "real_model_requests": 0,
            "scope": "HTTP with database session/configuration reads; mock model caches warmed; no reverse proxy or TLS; 40 requests per warm case plus bounded sustained runs",
            "reports": reports}, indent=2))
        assert all(report["failures"] == 0 and report["degraded"] == 0 for report in reports)
        assert all(report["stats"]["manual_trace_growth"] == 0 and report["stats"]["checked_out_connections"] == 0 for report in reports)
        assert reports[0]["stats"]["lexical_loads"] == 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "run"))
    parser.add_argument("--output", type=Path, default=Path("/work/artifacts/concurrency.json"))
    parser.add_argument("--sustained-seconds", type=int, choices=range(10, 61), default=30)
    args = parser.parse_args()
    if args.mode == "serve":
        serve()
    else:
        asyncio.run(run(args.output, args.sustained_seconds))
