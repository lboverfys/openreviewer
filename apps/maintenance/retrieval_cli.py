"""在服务器运行代码索引与固定样本检索评测。"""

from __future__ import annotations

import argparse
import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from domain.retrieval import (
    PARSER_VERSION,
    RetrievalEvaluationCase,
    SourceFile,
    stable_key,
)
from persistence.database import Database
from persistence.models import Base
from persistence.retrieval import RetrievalRepository
from services.ai_settings import AiSecretCipher
from services.code_indexing import git_blob_sha, parse_sources
from services.retrieval import (
    HybridRetrievalService,
    RetrievalSettingsService,
    _embedding_batches,
)


def load_snapshot(directory: Path, manifest: Path) -> tuple[SourceFile, ...]:
    root = directory.resolve()
    sources = []
    for entry in manifest.read_bytes().split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, blob_sha = metadata.decode("ascii").split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            continue
        relative = raw_path.decode("utf-8")
        path = root / relative
        if path.suffix.lower() not in {".java", ".xml", ".md"} or not path.exists():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("快照路径越界或包含符号链接")
        sources.append(SourceFile(file=relative, blob_sha=blob_sha, content=path.read_bytes().decode("utf-8")))
    return tuple(sources)


def regression_fixture():
    root = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "retrieval_regression"
    dataset = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
    sources = tuple(SourceFile(file=name, content=(content := (root / name).read_text(encoding="utf-8")),
                               blob_sha=git_blob_sha(content)) for name in dataset["files"])
    fingerprint = stable_key(dataset["version"], dataset["cases"], [(source.file, source.blob_sha) for source in sources])
    return dataset, sources, fingerprint


def check_regression(report, baseline, fingerprint):
    if baseline.get("parser_version") != PARSER_VERSION or baseline.get("dataset_fingerprint") != fingerprint:
        raise ValueError("检索回归基线尚未确认，或数据/解析器版本已变化")
    for strategy in report.strategies:
        validation = next(item for item in strategy.splits if item.split == "validation")
        expected = baseline["validation"][strategy.strategy]
        if validation.sample_count != expected["sample_count"]:
            raise ValueError("检索回归验证样本数发生变化")
        for metric in ("recall_at_k", "mrr"):
            value = getattr(validation, metric)
            if value is None or value + 1e-12 < expected[metric]:
                raise ValueError(f"检索回归失败：{strategy.strategy} 的 {metric} 低于固定基线")


def run_regression(database: Database, *, check: bool = True):
    dataset, sources, fingerprint = regression_fixture()

    def reject_external(*_args):
        raise RuntimeError("确定性检索回归禁止创建外部模型客户端")

    service = HybridRetrievalService(RetrievalRepository(database.sessions),
        RetrievalSettingsService(database.sessions, AiSecretCipher(b"r" * 32)), client_factory=reject_external)
    index = service.index_sources({"installation_id":1, "repository_id":1, "repository":"fixture/retrieval-regression",
        "head_sha":sha256(fingerprint.encode()).hexdigest()[:40]}, sources, include_vectors=False)
    report = service.evaluate(index.id, tuple(RetrievalEvaluationCase.model_validate(case) for case in dataset["cases"]),
        dataset_version=dataset["version"], annotation_source="synthetic_contract", k=dataset["k"],
        strategies=("bm25", "lexical_relations"))
    if report.total_model_requests != 0:
        raise ValueError("确定性检索回归发生了外部请求")
    if check:
        check_regression(report, dataset["baseline"], fingerprint)
    return report, fingerprint


def main() -> None:
    parser = argparse.ArgumentParser(description="运行版本化代码索引和检索评测")
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index")
    index.add_argument("--directory", type=Path, required=True)
    index.add_argument("--manifest", type=Path, required=True)
    index.add_argument("--repository", required=True)
    index.add_argument("--repository-id", type=int, required=True)
    index.add_argument("--installation-id", type=int, required=True)
    index.add_argument("--head-sha", required=True)
    index.add_argument("--dry-run", action="store_true", help="只估算代码块、缓存及请求批次数，不调用模型")
    index.add_argument("--include-vectors", action="store_true", help="显式请求向量补全；仍受服务端暂停和数量上限控制")
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--index-id", required=True)
    evaluate.add_argument("--dataset", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument("--strategies", nargs="+", choices=("bm25", "lexical_relations", "hybrid", "hybrid_relations", "reranked"), default=["bm25", "lexical_relations"])
    regression = commands.add_parser("regression", help="在空的本机 openreviewer_test PostgreSQL 中执行无外部模型回归")
    regression.add_argument("--output", type=Path, required=True)
    regression.add_argument("--measure", action="store_true", help="仅测量固定夹具，供审阅基线，不修改现有基线")
    args = parser.parse_args()
    if args.command == "regression":
        raw_url = os.environ.get("OPENREVIEWER_TEST_POSTGRES_URL", "")
        if not raw_url:
            raise ValueError("检索严格回归需要显式的 OPENREVIEWER_TEST_POSTGRES_URL，不能用 SQLite 替代")
        url = make_url(raw_url)
        if url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost"} or url.database != "openreviewer_test":
            raise ValueError("只允许本机隔离的 openreviewer_test 数据库")
        database = Database.connect(url)
        created = False
        try:
            if inspect(database.engine).get_table_names():
                raise ValueError("回归数据库不是空库，请使用独立空库；不会清理已有表")
            with database.engine.begin() as connection:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            created = True
            Base.metadata.create_all(database.engine)
            report, fingerprint = run_regression(database, check=not args.measure)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(report.model_dump_json(indent=2))
            print(json.dumps({"dataset_fingerprint":fingerprint, "parser_version":report.parser_version,
                "checked":not args.measure, "model_requests":report.total_model_requests,
                "validation":{item.strategy:next(split for split in item.splits if split.split == "validation").model_dump() for item in report.strategies}}))
        finally:
            if created:
                Base.metadata.drop_all(database.engine)
            database.dispose()
        return
    database = Database.from_environment()
    service = HybridRetrievalService(
        RetrievalRepository(database.sessions),
        RetrievalSettingsService(database.sessions, AiSecretCipher.from_environment()),
    )
    try:
        if args.command == "index":
            target: dict[str, Any] = {
                "repository": args.repository, "repository_id": args.repository_id,
                "installation_id": args.installation_id, "head_sha": args.head_sha,
            }
            sources = load_snapshot(args.directory, args.manifest)
            if args.dry_run:
                settings = service.settings.get().settings
                parsed = parse_sources(sources)
                unique = {chunk.embedding_hash: chunk for chunk in parsed.chunks}
                keys = {digest: stable_key(settings.embedding_fingerprint, digest) for digest in unique}
                existing = service.repository.existing_embeddings(tuple(keys.values()))
                missing = [chunk for digest, chunk in unique.items() if keys[digest] not in existing]
                batches = list(_embedding_batches(missing))
                print(json.dumps({
                    "dry_run": True, "files": len(sources), "chunks": len(parsed.chunks),
                    "unique_vectors": len(unique), "cached_vectors": len(existing),
                    "new_vectors": len(missing), "planned_embedding_requests": len(batches),
                    "request_ceiling_with_retries": len(batches) * 3,
                    "configured_new_vector_limit": settings.max_new_vectors_per_index,
                    "model_requests_made": 0,
                }))
                return
            index_id = service.enqueue(target, include_vectors=args.include_vectors)
            if service.repository.get(index_id).status == "failed":
                service.retry_index(index_id, None, include_vectors=args.include_vectors)
            result = service.index_sources(target, sources, include_vectors=args.include_vectors)
            print(result.model_dump_json())
        else:
            dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
            cases = tuple(RetrievalEvaluationCase.model_validate(item) for item in dataset["cases"])
            report = service.evaluate(
                args.index_id, cases, dataset_version=dataset["version"],
                annotation_source=dataset["annotation_source"], k=dataset.get("k", 8),
                strategies=tuple(args.strategies),
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
            print(json.dumps({"report_id": report.id, "sample_count": len(cases), "output": str(args.output)}))
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
