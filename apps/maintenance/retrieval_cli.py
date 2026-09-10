"""在服务器运行代码索引与固定样本检索评测。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from domain.retrieval import RetrievalEvaluationCase, SourceFile, stable_key
from persistence.database import Database
from persistence.retrieval import RetrievalRepository
from services.ai_settings import AiSecretCipher
from services.code_indexing import parse_sources
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
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--index-id", required=True)
    evaluate.add_argument("--dataset", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
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
            index_id = service.enqueue(target)
            if service.repository.get(index_id).status == "failed":
                service.retry_index(index_id, None)
            result = service.index_sources(target, sources)
            print(result.model_dump_json())
        else:
            dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
            cases = tuple(RetrievalEvaluationCase.model_validate(item) for item in dataset["cases"])
            report = service.evaluate(
                args.index_id, cases, dataset_version=dataset["version"],
                annotation_source=dataset["annotation_source"], k=dataset.get("k", 8),
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
            print(json.dumps({"report_id": report.id, "sample_count": len(cases), "output": str(args.output)}))
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
