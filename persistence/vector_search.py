"""有界 HNSW 候选、严格快照过滤与精确回退。"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from math import sqrt

from sqlalchemy import literal_column, select, text, true
from sqlalchemy.orm import Session, sessionmaker

from persistence.models import (
    CodeEmbeddingRecord,
    CodeIndexChunkRecord,
    CodeIndexRecord,
)
from services.retrieval_providers import RetrievalError

ANN_MIN_VECTORS = 1000
MIN_ANN_COVERAGE = 0.5


@dataclass(frozen=True)
class VectorSearchResult:
    hits: tuple[tuple[str, float], ...]
    mode: str


def exact_statement(index_id: str, vector: Sequence[float], limit: int):
    distance = CodeEmbeddingRecord.embedding.cosine_distance(list(vector))
    # 只物化ID与距离；不把每条4KB向量写入CTE临时存储。
    # 内层没有向量排序，保证回退路径计算完整快照，不隐式使用ANN。
    snapshot = select(CodeIndexChunkRecord.chunk_id, distance.label("distance")).join(
        CodeEmbeddingRecord, CodeEmbeddingRecord.id == CodeIndexChunkRecord.embedding_id,
    ).where(CodeIndexChunkRecord.index_id == index_id).cte("snapshot_scores").prefix_with("MATERIALIZED", dialect="postgresql")
    return select(snapshot.c.chunk_id, snapshot.c.distance).order_by(snapshot.c.distance, snapshot.c.chunk_id).limit(limit)


def approximate_statement(index_id: str, configuration: str, vector: Sequence[float], limit: int):
    distance = CodeEmbeddingRecord.embedding.cosine_distance(list(vector))
    # HNSW扫描保留原始距离排序，避免额外排序键让规划器放弃向量索引。
    nearest = select(CodeEmbeddingRecord.id, distance.label("distance")).where(
        CodeEmbeddingRecord.configuration_key == configuration,
    ).order_by(distance).limit(max(40, limit * 4)).cte("nearest_embeddings").prefix_with("MATERIALIZED", dialect="postgresql")
    # 全局候选必须经过(index_id, embedding_id)关联；其他提交的块不能返回。
    matches = select(CodeIndexChunkRecord.chunk_id).where(
        CodeIndexChunkRecord.index_id == index_id, CodeIndexChunkRecord.embedding_id == nearest.c.id,
    ).order_by(CodeIndexChunkRecord.chunk_id).limit(limit).correlate(nearest).lateral("snapshot_matches")
    # 每个向量最多取K个同分块，足够组成全局Top-K；避免扫描整个关联表。
    return select(matches.c.chunk_id, nearest.c.distance).select_from(nearest).join(
        matches, true(),
    ).order_by(nearest.c.distance, matches.c.chunk_id).limit(limit)


def search_vectors(sessions: sessionmaker[Session], index_id: str, vector: Sequence[float],
                   limit: int, *, exact: bool = False) -> VectorSearchResult:
    if not 1 <= limit <= 50:
        raise ValueError("向量候选上限必须在1到50之间")
    mode = os.environ.get("OPENREVIEWER_VECTOR_SEARCH_MODE", "adaptive")
    if mode not in {"adaptive", "exact"}:
        raise ValueError("向量检索模式必须是adaptive或exact")
    with sessions() as session:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            if exact or mode == "exact":
                rows = session.execute(exact_statement(index_id, vector, limit)).all()
                return VectorSearchResult(tuple((row.chunk_id, 1.0 - float(row.distance)) for row in rows), "exact_snapshot")
            population = select(literal_column("reltuples")).select_from(text("pg_class")).where(text("oid = 'code_embeddings'::regclass")).scalar_subquery()
            info = session.execute(select(CodeIndexRecord.configuration_key, CodeIndexRecord.vector_count,
                population.label("population"),
            ).where(CodeIndexRecord.id == index_id)).one_or_none()
            if info is None:
                raise LookupError("代码索引不存在")
            selected_mode = "exact_snapshot"
            # 稀疏旧快照或统计未知时直接精确搜索，避免先扫ANN再回退的额外开销。
            if info.vector_count >= ANN_MIN_VECTORS and info.population > 0 and info.vector_count >= info.population * MIN_ANN_COVERAGE:
                # 设置只作用于本次事务，不修改数据库或其他会话的配置。
                session.execute(text("SET LOCAL hnsw.iterative_scan = 'strict_order'"))
                session.execute(text("SET LOCAL hnsw.ef_search = 80"))
                session.execute(text("SET LOCAL hnsw.max_scan_tuples = 20000"))
                rows = session.execute(approximate_statement(index_id, info.configuration_key, vector, limit)).all()
                if len(rows) >= min(limit, info.vector_count):
                    return VectorSearchResult(tuple((row.chunk_id, 1.0 - float(row.distance)) for row in rows), "hnsw_snapshot")
                selected_mode = "exact_fallback"
            rows = session.execute(exact_statement(index_id, vector, limit)).all()
            return VectorSearchResult(tuple((row.chunk_id, 1.0 - float(row.distance)) for row in rows), selected_mode)
        # SQLite只服务小型离线夹具，生产数据库采用上面的SQL路径。
        rows = session.execute(select(CodeIndexChunkRecord.chunk_id, CodeEmbeddingRecord.embedding).join(
            CodeEmbeddingRecord, CodeEmbeddingRecord.id == CodeIndexChunkRecord.embedding_id,
        ).where(CodeIndexChunkRecord.index_id == index_id).limit(2001)).all()
        if len(rows) > 2000:
            raise RetrievalError("SQLite 仅支持小型检索测试集")
        query_norm = sqrt(sum(value * value for value in vector))
        scores = []
        for row in rows:
            norm = sqrt(sum(value * value for value in row.embedding))
            score = sum(a * b for a, b in zip(vector, row.embedding, strict=True)) / (query_norm * norm) if query_norm and norm else 0
            scores.append((row.chunk_id, score))
        return VectorSearchResult(tuple(sorted(scores, key=lambda item: (-item[1], item[0]))[:limit]), "exact_snapshot")
