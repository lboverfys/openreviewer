"""检索的共享短租约、请求缓存与有界维护。HTTP 不占用数据库事务。"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import case, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, aliased, sessionmaker

from persistence.models import (
    CodeChunkRecord,
    CodeEmbeddingRecord,
    CodeIndexChunkRecord,
    CodeIndexRecord,
    CodeParseRecord,
    CodeSourceCacheRecord,
    RetrievalEvaluationRecord,
    RetrievalProviderStateRecord,
    RetrievalRequestBudgetRecord,
    RetrievalRerankCacheRecord,
    RetrievalTraceRecord,
)
from services.retrieval_providers import RetrievalError


class RetrievalRuntimeRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def charge_request(self, key: str, limit: int) -> bool:
        with self.sessions() as session, session.begin():
            factory = pg_insert if session.bind is not None and session.bind.dialect.name == "postgresql" else sqlite_insert
            session.execute(factory(RetrievalRequestBudgetRecord).values(id=key, used=0, created_at=datetime.now(UTC)).on_conflict_do_nothing())
            row = session.execute(update(RetrievalRequestBudgetRecord).where(
                RetrievalRequestBudgetRecord.id == key, RetrievalRequestBudgetRecord.used < limit,
            ).values(used=RetrievalRequestBudgetRecord.used + 1).returning(RetrievalRequestBudgetRecord.id)).scalar_one_or_none()
            return row is not None

    def request_usage(self, key: str) -> int:
        with self.sessions() as session:
            return session.scalar(select(RetrievalRequestBudgetRecord.used).where(
                RetrievalRequestBudgetRecord.id == key,
            )) or 0

    @contextmanager
    def model_lane(self, key: str) -> Iterator[None]:
        """同一凭据的 API/Worker 共用单请求通道；租约释放前完成缓存写入。"""
        now, owner = datetime.now(UTC), str(uuid4())
        with self.sessions() as session, session.begin():
            factory = pg_insert if session.bind is not None and session.bind.dialect.name == "postgresql" else sqlite_insert
            session.execute(factory(RetrievalProviderStateRecord).values(id=key, failures=0).on_conflict_do_nothing())
            acquired = session.execute(update(RetrievalProviderStateRecord).where(
                RetrievalProviderStateRecord.id == key,
                or_(RetrievalProviderStateRecord.lease_until.is_(None), RetrievalProviderStateRecord.lease_until < now),
                or_(RetrievalProviderStateRecord.blocked_until.is_(None), RetrievalProviderStateRecord.blocked_until < now),
            ).values(owner=owner, lease_until=now + timedelta(minutes=10)).returning(RetrievalProviderStateRecord.id)).scalar_one_or_none()
            if acquired is None:
                raise RetrievalError("检索模型正在处理其他请求或短暂熔断，已保留基础检索结果", retryable=True)
        failed = False
        try:
            yield
        except RetrievalError:
            failed = True
            raise
        finally:
            with self.sessions() as session, session.begin():
                values: dict[str, Any] = {"owner": None, "lease_until": None, "failures": 0, "blocked_until": None}
                if failed:
                    values.update(failures=RetrievalProviderStateRecord.failures + 1,
                        blocked_until=case((RetrievalProviderStateRecord.failures >= 2, datetime.now(UTC) + timedelta(seconds=60)), else_=None))
                session.execute(update(RetrievalProviderStateRecord).where(
                    RetrievalProviderStateRecord.id == key, RetrievalProviderStateRecord.owner == owner,
                ).values(**values))

    def vectors(self, keys: Sequence[str]) -> dict[str, tuple[float, ...]]:
        if not keys:
            return {}
        if len(keys) > 20:
            raise ValueError("向量正文读取必须限制在一个批次内")
        with self.sessions() as session:
            return {row.id: tuple(row.embedding) for row in session.execute(select(
                CodeEmbeddingRecord.id, CodeEmbeddingRecord.embedding,
            ).where(CodeEmbeddingRecord.id.in_(keys)).limit(len(keys)))}

    def rerank(self, key: str) -> tuple[tuple[int, float], ...] | None:
        with self.sessions() as session:
            value = session.scalar(select(RetrievalRerankCacheRecord.ranking).where(
                RetrievalRerankCacheRecord.id == key,
                RetrievalRerankCacheRecord.created_at > datetime.now(UTC) - timedelta(days=1),
            ))
            return tuple((int(n), float(score)) for n, score in value) if value is not None else None

    def cache_rerank(self, key: str, ranking: Sequence[tuple[int, float]]) -> None:
        with self.sessions() as session, session.begin():
            factory = pg_insert if session.bind is not None and session.bind.dialect.name == "postgresql" else sqlite_insert
            statement = factory(RetrievalRerankCacheRecord).values(id=key, ranking=[list(item) for item in ranking], created_at=datetime.now(UTC))
            session.execute(statement.on_conflict_do_update(index_elements=["id"], set_={"ranking": statement.excluded.ranking, "created_at": statement.excluded.created_at}))

    def source_blobs(self, keys: Sequence[str]) -> dict[str, str]:
        if not keys:
            return {}
        if len(keys) > 1000:
            raise ValueError("源码缓存查询超过文件上限")
        with self.sessions() as session:
            return {row.id: row.content for row in session.execute(select(CodeSourceCacheRecord.id, CodeSourceCacheRecord.content).where(
                CodeSourceCacheRecord.id.in_(keys),
            ).limit(len(keys)).execution_options(yield_per=50))}

    def cache_blobs(self, items: Sequence[tuple[str, str]]) -> None:
        if not items:
            return
        if len(items) > 20:
            raise ValueError("源码缓存写入超过批次上限")
        with self.sessions() as session, session.begin():
            factory = pg_insert if session.bind is not None and session.bind.dialect.name == "postgresql" else sqlite_insert
            session.execute(factory(CodeSourceCacheRecord).values([
                {"id": key, "content": content, "created_at": datetime.now(UTC)} for key, content in items
            ]).on_conflict_do_nothing())

    def cleanup(self, batch_size: int = 100) -> int:
        """保护有证据、评测引用或仍在使用的模型缓存，每轮固定小批删除。"""
        now = datetime.now(UTC)
        newer = aliased(CodeIndexRecord)
        expired_indexes = select(CodeIndexRecord.id).where(
            CodeIndexRecord.status.in_(("ready", "failed")), CodeIndexRecord.created_at < now - timedelta(days=30),
            exists(select(newer.id).where(newer.repository_id == CodeIndexRecord.repository_id, newer.created_at > CodeIndexRecord.created_at)),
            ~exists(select(RetrievalTraceRecord.id).where(RetrievalTraceRecord.index_id == CodeIndexRecord.id)),
            ~exists(select(RetrievalEvaluationRecord.id).where(RetrievalEvaluationRecord.index_id == CodeIndexRecord.id)),
        ).order_by(CodeIndexRecord.created_at).limit(batch_size)
        total = 0
        with self.sessions() as session, session.begin():
            total += self._delete_batch(session, RetrievalTraceRecord,
                RetrievalTraceRecord.review_run_id.is_(None) & RetrievalTraceRecord.agent.is_(None) & (RetrievalTraceRecord.created_at < now - timedelta(days=14)), batch_size)
            ids = session.scalars(expired_indexes).all()
            if ids:
                session.execute(delete(CodeIndexRecord).where(CodeIndexRecord.id.in_(ids)))
                total += len(ids)
            # 源码与解析缓存可重建；向量只清理没有任何索引使用的旧模型配置。
            total += self._delete_batch(session, CodeSourceCacheRecord, CodeSourceCacheRecord.created_at < now - timedelta(days=30), batch_size)
            total += self._delete_batch(session, CodeParseRecord, CodeParseRecord.created_at < now - timedelta(days=30), batch_size)
            total += self._delete_batch(session, RetrievalRerankCacheRecord, RetrievalRerankCacheRecord.created_at < now - timedelta(days=1), batch_size)
            total += self._delete_batch(session, RetrievalRequestBudgetRecord, RetrievalRequestBudgetRecord.created_at < now - timedelta(days=365), batch_size)
            total += self._delete_batch(session, CodeEmbeddingRecord, (CodeEmbeddingRecord.purpose == "query") & (CodeEmbeddingRecord.created_at < now - timedelta(days=7)) & ~exists(select(CodeIndexChunkRecord.chunk_id).where(CodeIndexChunkRecord.embedding_id == CodeEmbeddingRecord.id)), batch_size)
            total += self._delete_batch(session, CodeChunkRecord, (CodeChunkRecord.created_at < now - timedelta(days=30)) & ~exists(select(CodeIndexChunkRecord.chunk_id).where(CodeIndexChunkRecord.chunk_id == CodeChunkRecord.id)), batch_size)
            total += self._delete_batch(session, CodeEmbeddingRecord, (CodeEmbeddingRecord.created_at < now - timedelta(days=90)) & ~exists(select(CodeIndexRecord.id).where(CodeIndexRecord.configuration_key == CodeEmbeddingRecord.configuration_key)) & ~exists(select(CodeIndexChunkRecord.chunk_id).where(CodeIndexChunkRecord.embedding_id == CodeEmbeddingRecord.id)), batch_size)
        return total

    @staticmethod
    def _delete_batch(session: Session, model: Any, predicate: Any, limit: int) -> int:
        candidates = session.scalars(select(model.id).where(predicate).order_by(model.created_at, model.id).limit(limit)).all()
        if candidates:
            session.execute(delete(model).where(model.id.in_(candidates)))
        return len(candidates)

    def circuit_state(self, key: str) -> tuple[bool, bool]:
        now = datetime.now(UTC)
        with self.sessions() as session:
            row = session.execute(select(
                func.coalesce(RetrievalProviderStateRecord.lease_until > now, False),
                func.coalesce(RetrievalProviderStateRecord.blocked_until > now, False),
            ).where(RetrievalProviderStateRecord.id == key)).one_or_none()
        return (bool(row[0]), bool(row[1])) if row else (False, False)
