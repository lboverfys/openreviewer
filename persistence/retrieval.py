"""混合检索的 SQL 存储；索引版本隔离、短租约和有界批量查询。"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, aliased, sessionmaker

from domain.retrieval import (
    MAX_INDEX_CHUNKS,
    MAX_INDEX_FILES,
    PARSER_VERSION,
    CodeChunk,
    CodeRelation,
    IndexTarget,
    IndexView,
    RetrievalEvaluationReport,
    RetrievalOperations,
    RetrievalSettings,
    RetrievalTrace,
    stable_key,
)
from persistence.models import (
    CodeChunkRecord,
    CodeEmbeddingRecord,
    CodeIndexChunkRecord,
    CodeIndexRecord,
    CodeParseRecord,
    CodeRelationRecord,
    RetrievalEvaluationRecord,
    RetrievalTraceRecord,
    ReviewRunRecord,
)
from persistence.resource_scope import resource_predicate
from services.code_indexing import code_tokens
from services.rbac import ResourceScope
from services.retrieval_providers import RetrievalError


def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=UTC).isoformat() if value is not None and value.tzinfo is None else value.isoformat() if value is not None else None


def _view(row: CodeIndexRecord) -> IndexView:
    return IndexView(
        id=row.id, repository=row.repository, repository_id=row.repository_id,
        installation_id=row.installation_id, head_sha=row.head_sha, status=row.status,
        lexical_ready=row.lexical_ready, vector_status=row.vector_status,
        vector_count=row.vector_count, vector_error=row.vector_error,
        embedding_model=row.embedding_model, dimensions=row.dimensions,
        file_count=row.file_count, parsed_files=row.parsed_files, reused_files=row.reused_files, chunk_count=row.chunk_count, relation_count=row.relation_count,
        embedded_count=row.embedded_count, reused_count=row.reused_count, duration_ms=row.duration_ms,
        parse_error_files=tuple(str(value) for value in row.parse_errors), error=row.error,
        created_at=_iso(row.created_at) or "", completed_at=_iso(row.completed_at),
    )


def _scope(scope: ResourceScope | None):
    return resource_predicate(scope, installation_column=CodeIndexRecord.installation_id, repository_column=CodeIndexRecord.repository)


class RetrievalRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    @staticmethod
    def _insert(session: Session, model: Any, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        factory = pg_insert if session.bind is not None and session.bind.dialect.name == "postgresql" else sqlite_insert
        session.execute(factory(model).values(rows).on_conflict_do_nothing())

    @staticmethod
    def _owned(session: Session, index_id: str, owner: str) -> CodeIndexRecord:
        row = session.scalar(select(CodeIndexRecord).where(
            CodeIndexRecord.id == index_id, CodeIndexRecord.lease_owner == owner,
            CodeIndexRecord.status == "building", CodeIndexRecord.lease_until > datetime.now(UTC),
        ).with_for_update())
        if row is None:
            raise RetrievalError("索引构建租约已失效", retryable=True)
        row.lease_until = datetime.now(UTC) + timedelta(minutes=5)
        return row

    def enqueue(self, target: dict[str, Any], settings: RetrievalSettings) -> str:
        index_id = stable_key(target["installation_id"], target["repository_id"], target["head_sha"], settings.embedding_fingerprint)
        with self.sessions() as session, session.begin():
            self._insert(session, CodeIndexRecord, [{
                "id": index_id, "installation_id": target["installation_id"],
                "repository_id": target["repository_id"], "repository": target["repository"].casefold(),
                "head_sha": target["head_sha"], "configuration_key": settings.embedding_fingerprint,
                "embedding_model": settings.embedding_model, "dimensions": settings.dimensions,
                "source_target": {**target, "vector_operation_id": str(uuid4())}, "status": "queued",
            }])
        return index_id


    def target_for_review(self, review_run_id: str, scope: ResourceScope | None) -> dict[str, Any]:
        with self.sessions() as session:
            row = session.execute(select(
                ReviewRunRecord.installation_id, ReviewRunRecord.repository_id,
                ReviewRunRecord.repository, ReviewRunRecord.head_sha,
            ).where(
                ReviewRunRecord.id == review_run_id,
                resource_predicate(scope, installation_column=ReviewRunRecord.installation_id,
                    repository_column=ReviewRunRecord.repository,
                    repository_key_column=ReviewRunRecord.repository_key),
            )).mappings().one_or_none()
            if row is None:
                raise LookupError("审查记录不存在")
            return dict(row)

    def targets(self, scope: ResourceScope | None) -> tuple[IndexTarget, ...]:
        with self.sessions() as session:
            rows = session.execute(select(
                ReviewRunRecord.id.label("review_run_id"), ReviewRunRecord.repository,
                ReviewRunRecord.pull_request_number, ReviewRunRecord.head_sha,
            ).where(resource_predicate(scope, installation_column=ReviewRunRecord.installation_id,
                repository_column=ReviewRunRecord.repository, repository_key_column=ReviewRunRecord.repository_key),
            ).order_by(ReviewRunRecord.created_at.desc()).limit(50)).mappings()
            return tuple(IndexTarget.model_validate(dict(row)) for row in rows)

    def operations(self, scope: ResourceScope | None) -> RetrievalOperations:
        with self.sessions() as session:
            rows = session.execute(select(CodeIndexRecord.status, CodeIndexRecord.lexical_ready,
                CodeIndexRecord.vector_status, func.count(), func.min(CodeIndexRecord.created_at),
            ).where(_scope(scope)).group_by(CodeIndexRecord.status, CodeIndexRecord.lexical_ready, CodeIndexRecord.vector_status)).all()
        pending = available = partial = 0
        oldest = 0.0
        for status, ready, vector_status, count, created in rows:
            if status in {"queued", "building"}:
                pending += count
                oldest = max(oldest, (datetime.now(UTC) - created.replace(tzinfo=UTC)).total_seconds())
            available += count if ready else 0
            partial += count if ready and vector_status != "ready" else 0
        return RetrievalOperations(pending_indexes=pending, oldest_pending_seconds=oldest, available_indexes=available, partial_indexes=partial)

    def get(self, index_id: str, scope: ResourceScope | None = None) -> IndexView:
        with self.sessions() as session:
            row = session.scalar(select(CodeIndexRecord).where(CodeIndexRecord.id == index_id, _scope(scope)))
            if row is None:
                raise LookupError("代码索引不存在")
            return _view(row)

    def list_indexes(self, scope: ResourceScope | None = None, limit: int = 20) -> tuple[IndexView, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("索引列表上限无效")
        with self.sessions() as session:
            rows = session.scalars(select(CodeIndexRecord).where(_scope(scope)).order_by(CodeIndexRecord.created_at.desc(), CodeIndexRecord.id.desc()).limit(limit)).all()
            return tuple(_view(row) for row in rows)

    def claim(self, index_id: str | None = None) -> tuple[str, str, dict[str, Any], str] | None:
        now = datetime.now(UTC)
        with self.sessions() as session, session.begin():
            statement = select(CodeIndexRecord).where(or_(
                CodeIndexRecord.status == "queued",
                (CodeIndexRecord.status == "building") & (CodeIndexRecord.lease_until < now),
            )).order_by(CodeIndexRecord.created_at).limit(1).with_for_update(skip_locked=True)
            if index_id is not None:
                statement = statement.where(CodeIndexRecord.id == index_id)
            row = session.scalar(statement)
            if row is None:
                return None
            owner = str(uuid4())
            row.status, row.lease_owner, row.lease_until = "building", owner, now + timedelta(minutes=5)
            row.error = None
            return row.id, owner, dict(row.source_target), row.configuration_key

    def retry(self, index_id: str, scope: ResourceScope | None, *, include_vectors: bool = False) -> None:
        self.get(index_id, scope)
        with self.sessions() as session, session.begin():
            row = session.scalar(select(CodeIndexRecord).where(CodeIndexRecord.id == index_id).with_for_update())
            if row is None or row.status not in {"ready", "failed"}:
                raise RetrievalError("索引正在构建或排队")
            row.source_target = {**row.source_target, "include_vectors": include_vectors, "vector_operation_id": str(uuid4())}
            row.status, row.error = "queued", None

    def renew(self, index_id: str, owner: str) -> None:
        with self.sessions() as session, session.begin():
            self._owned(session, index_id, owner)

    def fail(self, index_id: str, owner: str, message: str) -> None:
        with self.sessions() as session, session.begin():
            session.execute(update(CodeIndexRecord).where(
                CodeIndexRecord.id == index_id, CodeIndexRecord.lease_owner == owner,
                CodeIndexRecord.status == "building", CodeIndexRecord.lease_until > datetime.now(UTC),
            ).values(status="failed", error=message[:500], lease_owner=None, lease_until=None))


    def reset(self, index_id: str, owner: str) -> None:
        with self.sessions() as session, session.begin():
            self._owned(session, index_id, owner)
            session.execute(delete(CodeRelationRecord).where(CodeRelationRecord.index_id == index_id))
            session.execute(delete(CodeIndexChunkRecord).where(CodeIndexChunkRecord.index_id == index_id))

    def progress(self, index_id: str, owner: str, files: int, chunks: int, embedded: int, reused: int) -> None:
        with self.sessions() as session, session.begin():
            row = self._owned(session, index_id, owner)
            row.file_count, row.chunk_count = files, chunks
            row.embedded_count, row.reused_count = embedded, reused


    def cached_parses(self, keys: Sequence[str]) -> dict[str, tuple[CodeChunk, ...]]:
        if len(keys) > MAX_INDEX_FILES:
            raise ValueError("解析缓存查询超出文件上限")
        if not keys:
            return {}
        with self.sessions() as session:
            rows = session.execute(select(CodeParseRecord.id, CodeParseRecord.chunks).where(
                CodeParseRecord.id.in_(keys), CodeParseRecord.parser_version == PARSER_VERSION,
            ).limit(len(keys)).execution_options(yield_per=50))
            return {row.id: tuple(CodeChunk.model_validate(value) for value in row.chunks) for row in rows}

    def store_parses(self, items: Sequence[tuple[str, Sequence[CodeChunk]]]) -> None:
        if len(items) > 50:
            raise ValueError("解析缓存写入必须分批")
        with self.sessions() as session, session.begin():
            self._insert(session, CodeParseRecord, [
                {"id": key, "parser_version": PARSER_VERSION, "chunks": [chunk.model_dump(mode="json") for chunk in chunks]}
                for key, chunks in items
            ])

    def existing_embeddings(self, keys: Sequence[str]) -> set[str]:
        if len(keys) > MAX_INDEX_CHUNKS:
            raise ValueError("向量缓存查询必须分批")
        if not keys:
            return set()
        with self.sessions() as session:
            return set(session.scalars(select(CodeEmbeddingRecord.id).where(CodeEmbeddingRecord.id.in_(keys)).limit(len(keys))).all())

    def store_embeddings(self, configuration_key: str, items: Sequence[tuple[str, str, Sequence[float]]], *, purpose: str = "code") -> None:
        if len(items) > 20:
            raise ValueError("向量写入必须分批")
        with self.sessions() as session, session.begin():
            self._insert(session, CodeEmbeddingRecord, [{
                "id": key, "configuration_key": configuration_key,
                "input_hash": digest, "embedding": list(vector),
                "purpose": purpose,
            } for key, digest, vector in items])

    def cached_vector(self, key: str) -> tuple[float, ...] | None:
        with self.sessions() as session:
            value = session.scalar(select(CodeEmbeddingRecord.embedding).where(CodeEmbeddingRecord.id == key))
            return tuple(float(v) for v in value) if value is not None else None

    def store_chunk_batch(self, index_id: str, owner: str, chunks: Sequence[CodeChunk], configuration_key: str) -> None:
        if len(chunks) > 200:
            raise ValueError("代码块写入必须分批")
        with self.sessions() as session, session.begin():
            self._owned(session, index_id, owner)
            rows = []
            for chunk in chunks:
                row = chunk.model_dump(mode="json")
                row["embedding_hash"] = chunk.embedding_hash
                row["tokens"] = code_tokens(f"{chunk.file} {chunk.symbol} {chunk.content}")
                rows.append(row)
            self._insert(session, CodeChunkRecord, rows)
            self._insert(session, CodeIndexChunkRecord, [{
                "index_id": index_id, "chunk_id": chunk.id,
                "embedding_id": None,
            } for chunk in chunks])

    def finish(
        self, index_id: str, owner: str, relations: Sequence[CodeRelation], *,
        file_count: int, chunk_count: int, embedded_count: int, reused_count: int,
        duration_ms: int, parse_errors: Sequence[str], parsed_files: int = 0, reused_files: int = 0,
        release: bool = True,
    ) -> None:
        if len(relations) > MAX_INDEX_CHUNKS * 10:
            raise ValueError("代码关系数量超过上限")
        with self.sessions() as session, session.begin():
            row = self._owned(session, index_id, owner)
            session.execute(delete(CodeRelationRecord).where(CodeRelationRecord.index_id == index_id))
            # Core executemany keeps relationship insertion in one batch API.
            if relations:
                session.execute(insert(CodeRelationRecord).execution_options(insertmanyvalues_page_size=500), [
                    {"index_id": index_id, **relation.model_dump()} for relation in relations
                ])
            row.parsed_files, row.reused_files = parsed_files, reused_files
            row.file_count, row.chunk_count, row.relation_count = file_count, chunk_count, len(relations)
            row.embedded_count, row.reused_count, row.duration_ms = embedded_count, reused_count, duration_ms
            row.parse_errors = list(parse_errors)
            row.lexical_ready = True
            if release:
                row.status, row.completed_at = "ready", datetime.now(UTC)
                row.lease_owner = row.lease_until = None

    def link_vectors(self, index_id: str, owner: str, configuration_key: str) -> int:
        # 一个按快照与内容哈希关联的 UPDATE，避免逐块查找向量。
        with self.sessions() as session, session.begin():
            self._owned(session, index_id, owner)
            embedding = select(CodeEmbeddingRecord.id).join(CodeChunkRecord,
                CodeEmbeddingRecord.input_hash == CodeChunkRecord.embedding_hash,
            ).where(CodeChunkRecord.id == CodeIndexChunkRecord.chunk_id,
                CodeEmbeddingRecord.configuration_key == configuration_key).scalar_subquery()
            session.execute(update(CodeIndexChunkRecord).where(CodeIndexChunkRecord.index_id == index_id).values(embedding_id=embedding))
            return int(session.scalar(select(func.count()).select_from(CodeIndexChunkRecord).where(
                CodeIndexChunkRecord.index_id == index_id, CodeIndexChunkRecord.embedding_id.is_not(None),
            )) or 0)

    def complete(self, index_id: str, owner: str, *, vector_count: int, vector_status: str,
                 vector_error: str | None, embedded: int, reused: int, duration_ms: int) -> None:
        with self.sessions() as session, session.begin():
            row = self._owned(session, index_id, owner)
            row.vector_count, row.vector_status, row.vector_error = vector_count, vector_status, vector_error
            row.embedded_count, row.reused_count = embedded, reused
            row.duration_ms, row.status, row.completed_at = duration_ms, "ready", datetime.now(UTC)
            row.error = None
            row.lease_owner = row.lease_until = None

    def missing_vector_count(self, index_id: str) -> int:
        with self.sessions() as session:
            return int(session.scalar(select(func.count()).select_from(CodeIndexChunkRecord).where(
                CodeIndexChunkRecord.index_id == index_id, CodeIndexChunkRecord.embedding_id.is_(None),
            )) or 0)

    def missing_chunk_pages(self, index_id: str) -> Iterator[tuple[CodeChunk, ...]]:
        cursor = ""
        columns = [getattr(CodeChunkRecord, name) for name in CodeChunk.model_fields]
        while True:
            with self.sessions() as session:
                rows = session.execute(select(*columns).join(CodeIndexChunkRecord,
                    CodeIndexChunkRecord.chunk_id == CodeChunkRecord.id,
                ).where(CodeIndexChunkRecord.index_id == index_id, CodeIndexChunkRecord.embedding_id.is_(None),
                    CodeChunkRecord.id > cursor,
                ).order_by(CodeChunkRecord.id).limit(200)).mappings().all()
            if not rows:
                return
            yield tuple(CodeChunk.model_validate(dict(row)) for row in rows)
            cursor = rows[-1]["id"]

    def lexical_documents(self, index_id: str) -> Iterator[tuple[str, str, str, dict[str, int], int, int]]:
        statement = select(CodeChunkRecord.id, CodeChunkRecord.file, CodeChunkRecord.symbol, CodeChunkRecord.tokens, CodeChunkRecord.start_line, CodeChunkRecord.end_line).join(
            CodeIndexChunkRecord, CodeIndexChunkRecord.chunk_id == CodeChunkRecord.id,
        ).where(CodeIndexChunkRecord.index_id == index_id).order_by(CodeChunkRecord.id).limit(MAX_INDEX_CHUNKS + 1)
        with self.sessions() as session:
            for number, row in enumerate(session.execute(statement.execution_options(yield_per=500))):
                if number >= MAX_INDEX_CHUNKS:
                    raise RetrievalError("索引分块数超过检索上限")
                yield row.id, row.file, row.symbol, row.tokens, row.start_line, row.end_line

    def chunks(self, index_id: str, ids: Sequence[str]) -> dict[str, CodeChunk]:
        if not ids:
            return {}
        if len(ids) > 150:
            raise ValueError("候选代码必须有界读取")
        columns = [getattr(CodeChunkRecord, name) for name in CodeChunk.model_fields]
        with self.sessions() as session:
            rows = session.execute(select(*columns).join(
                CodeIndexChunkRecord, CodeIndexChunkRecord.chunk_id == CodeChunkRecord.id,
            ).where(CodeIndexChunkRecord.index_id == index_id, CodeChunkRecord.id.in_(ids)).limit(len(ids))).mappings()
            return {row["id"]: CodeChunk.model_validate(dict(row)) for row in rows}

    def vector_search(self, index_id: str, vector: Sequence[float], limit: int) -> list[tuple[str, float]]:
        with self.sessions() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                # Materialize the exact snapshot first. This gives an exact baseline
                # and prevents ANN post-filtering from silently losing candidates.
                candidates = select(CodeIndexChunkRecord.chunk_id, CodeEmbeddingRecord.embedding).join(
                    CodeEmbeddingRecord, CodeEmbeddingRecord.id == CodeIndexChunkRecord.embedding_id,
                ).where(CodeIndexChunkRecord.index_id == index_id).cte("snapshot_vectors").prefix_with("MATERIALIZED", dialect="postgresql")
                distance = candidates.c.embedding.cosine_distance(list(vector))
                rows = session.execute(select(candidates.c.chunk_id, distance.label("distance")).order_by(distance, candidates.c.chunk_id).limit(limit)).all()
                return [(row.chunk_id, 1.0 - float(row.distance)) for row in rows]
            # Small isolated SQLite fixtures use the same exact cosine baseline.
            from math import sqrt
            query_norm = sqrt(sum(v * v for v in vector))
            rows = session.execute(select(CodeIndexChunkRecord.chunk_id, CodeEmbeddingRecord.embedding).join(
                CodeEmbeddingRecord, CodeEmbeddingRecord.id == CodeIndexChunkRecord.embedding_id,
            ).where(CodeIndexChunkRecord.index_id == index_id).limit(2001)).all()
            if len(rows) > 2000:
                raise RetrievalError("SQLite 仅支持小型检索测试集")
            scores = []
            for row in rows:
                norm = sqrt(sum(v * v for v in row.embedding))
                score = sum(a * b for a, b in zip(vector, row.embedding, strict=True)) / (query_norm * norm) if query_norm and norm else 0
                scores.append((row.chunk_id, score))
            return sorted(scores, key=lambda item: (-item[1], item[0]))[:limit]

    def relation_search(self, index_id: str, seed_ids: Sequence[str], limit: int) -> list[tuple[str, float]]:
        if not seed_ids:
            return []
        first, second = aliased(CodeRelationRecord), aliased(CodeRelationRecord)
        with self.sessions() as session:
            rows = session.execute(select(
                first.target_id.label("direct"), first.kind.label("direct_kind"),
                second.target_id.label("indirect"), second.kind.label("indirect_kind"),
            ).outerjoin(second, (second.index_id == first.index_id) & (second.source_id == first.target_id)).where(
                first.index_id == index_id, first.source_id.in_(seed_ids[:100]),
            ).order_by(first.target_id, second.target_id).limit(500)).all()
        scores: dict[str, float] = {}
        seeds = set(seed_ids)
        for row in rows:
            if row.direct not in seeds:
                scores[row.direct] = max(scores.get(row.direct, 0), 2.0 if row.direct_kind == "mapper_sql" else 1.0)
            if row.indirect is not None and row.indirect not in seeds:
                scores[row.indirect] = max(scores.get(row.indirect, 0), 1.5 if row.indirect_kind == "mapper_sql" else 0.5)
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]

    def save_trace(self, trace: RetrievalTrace, review_run_id: str | None = None, agent: str | None = None) -> RetrievalTrace:
        with self.sessions() as session, session.begin():
            self._insert(session, RetrievalTraceRecord, [{
                "id": trace.id, "index_id": trace.index_id, "review_run_id": review_run_id,
                "agent": agent, "plan_fingerprint": trace.plan_fingerprint,
                "payload": trace.model_dump(mode="json"),
            }])
        if review_run_id and trace.plan_fingerprint and agent:
            cached = self.review_contexts(review_run_id, trace.plan_fingerprint)
            return cached.get(agent, trace)
        return trace

    def review_contexts(self, review_run_id: str, plan_fingerprint: str) -> dict[str, RetrievalTrace]:
        with self.sessions() as session:
            rows = session.execute(select(RetrievalTraceRecord.agent, RetrievalTraceRecord.payload).where(
                RetrievalTraceRecord.review_run_id == review_run_id,
                RetrievalTraceRecord.plan_fingerprint == plan_fingerprint,
            ).limit(4)).all()
            return {row.agent: RetrievalTrace.model_validate(row.payload) for row in rows}

    def traces(self, review_run_id: str, scope: ResourceScope | None, limit: int = 6) -> tuple[RetrievalTrace, ...]:
        with self.sessions() as session:
            rows = session.scalars(select(RetrievalTraceRecord.payload).join(
                CodeIndexRecord, CodeIndexRecord.id == RetrievalTraceRecord.index_id,
            ).where(RetrievalTraceRecord.review_run_id == review_run_id, _scope(scope)).order_by(RetrievalTraceRecord.created_at.desc()).limit(min(limit, 20))).all()
            return tuple(RetrievalTrace.model_validate(row) for row in rows)

    def save_evaluation(self, report: RetrievalEvaluationReport) -> None:
        with self.sessions() as session, session.begin():
            session.add(RetrievalEvaluationRecord(id=report.id, index_id=report.index_id, report=report.model_dump(mode="json")))

    def evaluations(self, scope: ResourceScope | None, limit: int = 20) -> tuple[RetrievalEvaluationReport, ...]:
        with self.sessions() as session:
            rows = session.scalars(select(RetrievalEvaluationRecord.report).join(
                CodeIndexRecord, CodeIndexRecord.id == RetrievalEvaluationRecord.index_id,
            ).where(_scope(scope)).order_by(RetrievalEvaluationRecord.created_at.desc()).limit(min(limit, 50))).all()
            return tuple(RetrievalEvaluationReport.model_validate(row) for row in rows)
