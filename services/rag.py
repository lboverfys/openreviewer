"""无需向量基础设施的 Markdown 知识库检索。"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from threading import RLock
from uuid import uuid4

from sqlalchemy import and_, func, insert, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, defer, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from persistence.models import (
    KnowledgeDocumentRecord,
    KnowledgeDocumentVersionRecord,
    KnowledgeLibraryRecord,
)

_TOKEN = re.compile(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}")
_CJK_RUN = re.compile(r"^[\u4e00-\u9fff]+$")
_IDENTIFIER_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[0-9_]|$)|[A-Z]?[a-z]+|[0-9]+")


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    source: str
    heading: str
    content: str
    content_sha256: str
    version: str
    repository_scope: str | None = None


@dataclass(frozen=True, slots=True)
class RagCitation:
    source: str
    heading: str
    score: float
    excerpt: str
    version: str


def merge_review_citations(
    topics: tuple[RagCitation, ...], responsibilities: tuple[RagCitation, ...],
) -> tuple[RagCitation, ...]:
    """先保留变更主题，再补职责规则；不同查询的相同内容只传一次。"""
    combined: dict[tuple[str, str, str, str], RagCitation] = {}
    for item in (*topics, *responsibilities):
        combined.setdefault((item.source, item.heading, item.excerpt, item.version), item)
        if len(combined) == 8:
            break
    return tuple(combined.values())


@dataclass(frozen=True, slots=True)
class RagEvaluationCase:
    """一条离线检索评测样本。

    ``relevant_sources`` 使用知识文档的规范相对路径；同一文档的多个分片
    只计作一次命中，避免长文档因为分片数量较多而人为抬高分数。
    """

    query: str
    relevant_sources: frozenset[str]


@dataclass(frozen=True, slots=True)
class RagEvaluationReport:
    """固定评测集的 Recall@K、MRR 和样本数。"""

    sample_count: int
    recall_at_k: float
    mean_reciprocal_rank: float
    k: int


@dataclass(frozen=True, slots=True)
class KnowledgeVersionView:
    version: int
    content_sha256: str
    byte_size: int
    created_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class KnowledgeDocumentSummary:
    id: str
    source: str
    title: str
    enabled: bool
    archived: bool
    current_version: int
    content_sha256: str
    byte_size: int
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime
    repository_scope: str | None = field(default=None, kw_only=True)


@dataclass(frozen=True, slots=True)
class KnowledgeDocumentView(KnowledgeDocumentSummary):
    content: str
    versions: tuple[KnowledgeVersionView, ...]
    version_next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeLibraryView:
    revision: int
    total: int
    enabled_count: int
    total_enabled_bytes: int
    items: tuple[KnowledgeDocumentSummary, ...]
    offset: int = 0
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgeMutationView:
    revision: int
    document: KnowledgeDocumentView


@dataclass(frozen=True, slots=True)
class _SeedDocument:
    source: str
    content: str
    content_sha256: str
    byte_size: int
    repository_scope: str | None = None


class KnowledgeError(RuntimeError):
    """知识库管理错误基类。"""


class KnowledgeValidationError(KnowledgeError):
    """文档名称、内容或容量不符合边界。"""


class KnowledgeConflictError(KnowledgeError):
    """全局版本或文档版本已被其他管理员更新。"""


class KnowledgeNotFoundError(KnowledgeError):
    """目标知识文档或历史版本不存在。"""


class KnowledgePersistenceError(KnowledgeError):
    """数据库暂时无法完成知识库操作。"""


class MarkdownKnowledgeBase:
    """读取仓库内版本化 Markdown，并做确定性词法召回。"""

    def __init__(
        self,
        root: str | Path = "knowledge",
        *,
        max_files: int = 128,
        max_file_bytes: int = 512 * 1024,
        max_total_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        self.root = Path(root).resolve()
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self._chunks_cache: tuple[KnowledgeChunk, ...] | None = None
        self._indexed_chunks: tuple[KnowledgeChunk, ...] | None = None
        self._chunk_tokens: tuple[frozenset[str], ...] = ()
        self._chunk_token_counts: tuple[dict[str, int], ...] = ()
        self._token_index: dict[str, frozenset[int]] = {}
        self._token_document_frequency: dict[str, int] = {}
        self._search_index_lock = RLock()
        if max_files <= 0 or max_file_bytes <= 0 or max_total_bytes < max_file_bytes:
            raise ValueError("知识库边界无效")

    def chunks(self) -> tuple[KnowledgeChunk, ...]:
        if self._chunks_cache is not None:
            return self._chunks_cache
        if not self.root.exists():
            empty_chunks: tuple[KnowledgeChunk, ...] = ()
            self._replace_chunks_cache(empty_chunks)
            return empty_chunks
        paths = sorted(
            path
            for path in self.root.rglob("*.md")
            if path.is_file() and not path.is_symlink()
        )[: self.max_files]
        chunks: list[KnowledgeChunk] = []
        total = 0
        for path in paths:
            try:
                size = path.stat().st_size
                if size <= 0 or size > self.max_file_bytes or total + size > self.max_total_bytes:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            total += size
            relative = path.relative_to(self.root).as_posix()
            version = sha256(text.encode("utf-8")).hexdigest()[:16]
            scope = _source_repository_scope(text)
            chunks.extend(replace(chunk, repository_scope=scope) for chunk in _split_markdown(relative, text, version))
        loaded_chunks = tuple(chunks)
        self._replace_chunks_cache(loaded_chunks)
        return loaded_chunks

    def _replace_chunks_cache(self, chunks: tuple[KnowledgeChunk, ...]) -> None:
        """替换不可变 chunk 快照，并使对应的倒排索引失效。"""

        self._chunks_cache = chunks
        with self._search_index_lock:
            self._indexed_chunks = None
            self._chunk_tokens = ()
            self._chunk_token_counts = ()
            self._token_index = {}
            self._token_document_frequency = {}

    def _search_index_for(
        self,
        chunks: tuple[KnowledgeChunk, ...],
    ) -> tuple[tuple[frozenset[str], ...], dict[str, frozenset[int]]]:
        """为一个 immutable chunk 快照建立可复用的 token 集合和倒排索引。"""

        with self._search_index_lock:
            if self._indexed_chunks is chunks:
                return self._chunk_tokens, self._token_index
            token_counts = tuple(
                _token_counts(f"{chunk.source} {chunk.heading} {chunk.content}")
                for chunk in chunks
            )
            chunk_tokens = tuple(frozenset(counts) for counts in token_counts)
            inverted: dict[str, set[int]] = {}
            for index, tokens in enumerate(chunk_tokens):
                for token in tokens:
                    inverted.setdefault(token, set()).add(index)
            self._indexed_chunks = chunks
            self._chunk_tokens = chunk_tokens
            self._chunk_token_counts = token_counts
            self._token_index = {
                token: frozenset(indices) for token, indices in inverted.items()
            }
            self._token_document_frequency = {
                token: len(indices) for token, indices in self._token_index.items()
            }
            return self._chunk_tokens, self._token_index

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        chunks: tuple[KnowledgeChunk, ...] | None = None,
    ) -> tuple[RagCitation, ...]:
        normalized = query.strip()
        if not normalized:
            return ()
        if not 1 <= limit <= 20:
            raise ValueError("知识库检索数量必须在 1 到 20 之间")
        query_tokens = set(_tokens(normalized))
        if not query_tokens:
            return ()
        selected_chunks = self.chunks() if chunks is None else chunks
        chunk_tokens, token_index = self._search_index_for(selected_chunks)
        token_counts = self._chunk_token_counts
        document_frequency = self._token_document_frequency
        document_count = max(1, len(selected_chunks))
        # 平均文档长度与候选分片无关；提前计算，避免在候选循环中重复扫描
        # 全部分片（知识库较大时会把检索退化为 O(n²)）。
        average_length = max(
            1.0,
            sum(sum(item.values()) for item in token_counts)
            / max(1, len(token_counts)),
        )
        candidate_indices: set[int] = set()
        for token in query_tokens:
            candidate_indices.update(token_index.get(token, ()))
        scored: list[tuple[float, KnowledgeChunk]] = []
        for index in sorted(candidate_indices):
            chunk = selected_chunks[index]
            tokens = chunk_tokens[index]
            matched_tokens = query_tokens & tokens
            if not matched_tokens:
                continue
            counts = token_counts[index] if index < len(token_counts) else {}
            document_length = max(1, sum(counts.values()))
            # BM25 的有界词法得分：相比简单 overlap，能降低高频通用词的影响，
            # 同时让同一术语在正文中多次出现的分片更靠前。
            score = 0.0
            for token in matched_tokens:
                frequency = counts.get(token, 0)
                if frequency <= 0:
                    continue
                term_document_frequency = document_frequency.get(token, 0)
                inverse_frequency = math.log(
                    1.0
                    + (document_count - term_document_frequency + 0.5)
                    / (term_document_frequency + 0.5)
                )
                denominator = frequency + 1.5 * (
                    0.25 + 0.75 * document_length / average_length
                )
                score += inverse_frequency * frequency * 2.5 / denominator
            score /= max(1, len(query_tokens))
            if normalized.casefold() in chunk.content.casefold():
                score += 0.35
            if normalized.casefold() in chunk.heading.casefold():
                score += 0.15
            scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].source, item[1].heading))
        return tuple(
            RagCitation(
                source=chunk.source,
                heading=chunk.heading,
                score=round(score, 6),
                excerpt=_excerpt(chunk.content),
                version=chunk.version,
            )
            for score, chunk in scored[:limit]
        )

    def evaluate(
        self,
        cases: Iterable[RagEvaluationCase],
        *,
        k: int = 5,
    ) -> RagEvaluationReport:
        """在固定查询集上计算 Recall@K 与 MRR，不产生网络或数据库副作用。"""

        if not 1 <= k <= 20:
            raise ValueError("RAG evaluation k must be between 1 and 20")
        materialized = tuple(cases)
        if not materialized:
            return RagEvaluationReport(0, 0.0, 0.0, k)
        recalled = 0
        reciprocal_rank_total = 0.0
        for case in materialized:
            relevant = {source.casefold() for source in case.relevant_sources}
            if not relevant:
                continue
            citations = self.search(case.query, limit=k)
            ranked_sources = [citation.source.casefold() for citation in citations]
            first_rank = next(
                (
                    rank
                    for rank, source in enumerate(ranked_sources, start=1)
                    if source in relevant
                ),
                None,
            )
            if first_rank is not None:
                recalled += 1
                reciprocal_rank_total += 1.0 / first_rank
        count = len(materialized)
        return RagEvaluationReport(
            sample_count=count,
            recall_at_k=round(recalled / count, 8),
            mean_reciprocal_rank=round(reciprocal_rank_total / count, 8),
            k=k,
        )


class ManagedMarkdownKnowledgeBase(MarkdownKnowledgeBase):
    """以 PostgreSQL 为事实来源、保留确定性词法检索的 Markdown 知识库。"""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        seed_root: str | Path = "knowledge",
        *,
        max_files: int = 128,
        max_file_bytes: int = 512 * 1024,
        max_total_bytes: int = 5 * 1024 * 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            seed_root,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._seed_documents = _read_seed_documents(
            self.root,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
        self._cache_revision = -1
        self._seed_checked = False
        self._cache_lock = RLock()

    def chunks(self) -> tuple[KnowledgeChunk, ...]:
        with self._cache_lock:
            self._ensure_seeded()
            try:
                with self._sessions() as session:
                    revision = session.scalar(
                        select(KnowledgeLibraryRecord.revision).where(
                            KnowledgeLibraryRecord.id == 1
                        )
                    )
                    if revision is None:
                        raise KnowledgePersistenceError("知识库版本暂时无法读取")
                    if revision == self._cache_revision and self._chunks_cache is not None:
                        return self._chunks_cache
                    rows = session.execute(
                        select(
                            KnowledgeDocumentRecord.source,
                            KnowledgeDocumentRecord.repository_scope,
                            KnowledgeDocumentVersionRecord.content,
                            KnowledgeDocumentVersionRecord.content_sha256,
                        )
                        .join(
                            KnowledgeDocumentVersionRecord,
                            and_(
                                KnowledgeDocumentVersionRecord.document_id
                                == KnowledgeDocumentRecord.id,
                                KnowledgeDocumentVersionRecord.version
                                == KnowledgeDocumentRecord.current_version,
                            ),
                        )
                        .where(
                            KnowledgeDocumentRecord.enabled.is_(True),
                            KnowledgeDocumentRecord.archived_at.is_(None),
                        )
                        .order_by(KnowledgeDocumentRecord.source.asc())
                        .limit(self.max_files)
                    ).all()
            except KnowledgePersistenceError:
                raise
            except SQLAlchemyError as exc:
                raise KnowledgePersistenceError("知识库内容暂时无法读取") from exc

            chunks: list[KnowledgeChunk] = []
            for row in rows:
                chunks.extend(
                    replace(chunk, repository_scope=row.repository_scope) for chunk in _split_markdown(
                        row.source,
                        row.content,
                        row.content_sha256[:16],
                    )
                )
            loaded_chunks = tuple(chunks)
            self._replace_chunks_cache(loaded_chunks)
            self._cache_revision = revision
            return loaded_chunks

    def list_documents(
        self,
        *,
        include_archived: bool = False,
        archived_only: bool = False,
        limit: int = 10,
        offset: int = 0,
        query: str = "",
    ) -> KnowledgeLibraryView:
        if not 1 <= limit <= self.max_files:
            raise KnowledgeValidationError("知识文档数量必须在 1 到 128 之间")
        self._ensure_seeded()
        try:
            with self._sessions() as session:
                state = session.get(KnowledgeLibraryRecord, 1)
                if state is None:
                    raise KnowledgePersistenceError("知识库版本暂时无法读取")
                filters: tuple[ColumnElement[bool], ...] = (
                    (KnowledgeDocumentRecord.archived_at.is_not(None),) if archived_only
                    else () if include_archived
                    else (KnowledgeDocumentRecord.archived_at.is_(None),)
                )
                if query:
                    pattern = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
                    filters += (or_(KnowledgeDocumentRecord.source.ilike(pattern, escape="\\"), KnowledgeDocumentVersionRecord.content.ilike(pattern, escape="\\")),)
                rows = session.execute(
                    select(
                        KnowledgeDocumentRecord,
                        KnowledgeDocumentVersionRecord,
                    )
                    .join(
                        KnowledgeDocumentVersionRecord,
                        and_(
                            KnowledgeDocumentVersionRecord.document_id
                            == KnowledgeDocumentRecord.id,
                            KnowledgeDocumentVersionRecord.version
                            == KnowledgeDocumentRecord.current_version,
                        ),
                    )
                    .where(*filters)
                    .order_by(
                        KnowledgeDocumentRecord.archived_at.desc() if archived_only
                        else KnowledgeDocumentRecord.archived_at.asc(),
                        KnowledgeDocumentRecord.repository_scope.asc().nullslast(),
                        KnowledgeDocumentRecord.source.asc(),
                        KnowledgeDocumentRecord.id.asc(),
                    )
                    .offset(offset)
                    .limit(limit)
                ).all()
                total = session.scalar(
                    select(func.count(KnowledgeDocumentRecord.id)).join(KnowledgeDocumentVersionRecord, and_(KnowledgeDocumentVersionRecord.document_id == KnowledgeDocumentRecord.id, KnowledgeDocumentVersionRecord.version == KnowledgeDocumentRecord.current_version)).where(*filters)
                ) or 0
                enabled_count, enabled_bytes = self._enabled_totals(session)
                return KnowledgeLibraryView(
                    revision=state.revision,
                    offset=offset, has_more=offset + len(rows) < int(total),
                    total=int(total),
                    enabled_count=enabled_count,
                    total_enabled_bytes=enabled_bytes,
                    items=tuple(
                        _summary(document, version) for document, version in rows
                    ),
                )
        except (KnowledgePersistenceError, KnowledgeValidationError):
            raise
        except SQLAlchemyError as exc:
            raise KnowledgePersistenceError("知识文档暂时无法读取") from exc

    def get_document(self, document_id: str, version_limit: int = 10, version_cursor: int | None = None) -> KnowledgeDocumentView:
        self._ensure_seeded()
        try:
            with self._sessions() as session:
                row = session.execute(
                    select(
                        KnowledgeDocumentRecord,
                        KnowledgeDocumentVersionRecord,
                    )
                    .join(
                        KnowledgeDocumentVersionRecord,
                        and_(
                            KnowledgeDocumentVersionRecord.document_id
                            == KnowledgeDocumentRecord.id,
                            KnowledgeDocumentVersionRecord.version
                            == KnowledgeDocumentRecord.current_version,
                        ),
                    )
                    .where(KnowledgeDocumentRecord.id == document_id)
                ).one_or_none()
                if row is None:
                    raise KnowledgeNotFoundError("知识文档不存在")
                history_query = select(KnowledgeDocumentVersionRecord).options(defer(KnowledgeDocumentVersionRecord.content)).where(KnowledgeDocumentVersionRecord.document_id == document_id)
                if version_cursor is not None:
                    history_query = history_query.where(KnowledgeDocumentVersionRecord.version < version_cursor)
                history_rows = session.scalars(history_query.order_by(KnowledgeDocumentVersionRecord.version.desc()).limit(version_limit + 1)).all()
                versions = tuple(_version_view(version) for version in history_rows[:version_limit])
                return replace(_document_view(row[0], row[1], versions),
                    version_next_cursor=str(versions[-1].version) if len(history_rows) > version_limit else None)

        except KnowledgeNotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise KnowledgePersistenceError("知识文档暂时无法读取") from exc

    def create_document(
        self,
        *,
        source: str,
        content: str,
        enabled: bool,
        expected_revision: int,
        actor: str,
        repository_scope: str | None = None,
    ) -> KnowledgeMutationView:
        normalized_source = _validate_source(source)
        repository_scope = _validate_repository_scope(repository_scope)
        normalized_content, content_hash, byte_size = _validate_content(
            content,
            self.max_file_bytes,
        )
        self._ensure_seeded()
        now = self._clock()
        document_id = str(uuid4())
        try:
            with self._sessions() as session:
                state = self._lock_state(session, expected_revision, actor, now)
                if session.scalar(
                    select(KnowledgeDocumentRecord.id)
                    .where(KnowledgeDocumentRecord.source == normalized_source)
                    .limit(1)
                ) is not None:
                    raise KnowledgeValidationError("同名知识文档已经存在")
                document_count = session.scalar(
                    select(func.count(KnowledgeDocumentRecord.id))
                ) or 0
                if document_count >= self.max_files:
                    raise KnowledgeValidationError("知识文档数量已达到 128 个上限")
                _, enabled_bytes = self._enabled_totals(session)
                if enabled and enabled_bytes + byte_size > self.max_total_bytes:
                    raise KnowledgeValidationError("启用文档总大小不能超过 5 MiB")
                document = KnowledgeDocumentRecord(
                    id=document_id,
                    source=normalized_source,
                    repository_scope=repository_scope.casefold() if repository_scope else None,
                    enabled=enabled,
                    current_version=1,
                    created_by=actor,
                    updated_by=actor,
                    created_at=now,
                    updated_at=now,
                )
                session.add(document)
                session.add(
                    KnowledgeDocumentVersionRecord(
                        id=str(uuid4()),
                        document_id=document_id,
                        version=1,
                        content=normalized_content,
                        content_sha256=content_hash,
                        byte_size=byte_size,
                        created_by=actor,
                        created_at=now,
                    )
                )
                self._advance_state(state, actor, now)
                next_revision = state.revision
                session.commit()
        except (KnowledgeConflictError, KnowledgeValidationError):
            raise
        except IntegrityError as exc:
            raise KnowledgeConflictError("知识文档已被其他管理员更新") from exc
        except SQLAlchemyError as exc:
            raise KnowledgePersistenceError("知识文档暂时无法创建") from exc
        return KnowledgeMutationView(
            revision=next_revision,
            document=self.get_document(document_id),
        )

    def update_document(
        self,
        document_id: str,
        *,
        source: str,
        content: str,
        enabled: bool,
        expected_revision: int,
        expected_document_version: int,
        actor: str,
        repository_scope: str | None = None,
        update_repository_scope: bool = False,
    ) -> KnowledgeMutationView:
        normalized_source = _validate_source(source)
        if update_repository_scope:
            repository_scope = _validate_repository_scope(repository_scope)
        normalized_content, content_hash, byte_size = _validate_content(
            content,
            self.max_file_bytes,
        )
        now = self._clock()
        try:
            with self._sessions() as session:
                state = self._lock_state(session, expected_revision, actor, now)
                document, current = self._locked_document(session, document_id)
                if document.archived_at is not None:
                    raise KnowledgeValidationError("请先恢复归档文档再编辑")
                if document.current_version != expected_document_version:
                    raise KnowledgeConflictError("文档已被其他管理员更新，请重新读取")
                duplicate = session.scalar(
                    select(KnowledgeDocumentRecord.id)
                    .where(
                        KnowledgeDocumentRecord.source == normalized_source,
                        KnowledgeDocumentRecord.id != document_id,
                    )
                    .limit(1)
                )
                if duplicate is not None:
                    raise KnowledgeValidationError("同名知识文档已经存在")
                enabled_bytes = self._enabled_bytes_excluding(session, document_id)
                if enabled and enabled_bytes + byte_size > self.max_total_bytes:
                    raise KnowledgeValidationError("启用文档总大小不能超过 5 MiB")
                if current.content_sha256 != content_hash or (
                    update_repository_scope and document.repository_scope != repository_scope
                ):
                    document.current_version += 1
                    session.add(
                        KnowledgeDocumentVersionRecord(
                            id=str(uuid4()),
                            document_id=document_id,
                            version=document.current_version,
                            content=normalized_content,
                            content_sha256=content_hash,
                            byte_size=byte_size,
                            created_by=actor,
                            created_at=now,
                        )
                    )
                document.source = normalized_source
                document.enabled = enabled
                if update_repository_scope:
                    document.repository_scope = repository_scope
                document.updated_by = actor
                document.updated_at = now
                self._advance_state(state, actor, now)
                next_revision = state.revision
                session.commit()
        except (
            KnowledgeConflictError,
            KnowledgeNotFoundError,
            KnowledgeValidationError,
        ):
            raise
        except IntegrityError as exc:
            raise KnowledgeConflictError("知识文档已被其他管理员更新") from exc
        except SQLAlchemyError as exc:
            raise KnowledgePersistenceError("知识文档暂时无法保存") from exc
        return KnowledgeMutationView(
            revision=next_revision,
            document=self.get_document(document_id),
        )

    def archive_document(
        self,
        document_id: str,
        *,
        archived: bool,
        expected_revision: int,
        expected_document_version: int,
        actor: str,
        restore_enabled: bool = False,
    ) -> KnowledgeMutationView:
        now = self._clock()
        try:
            with self._sessions() as session:
                state = self._lock_state(session, expected_revision, actor, now)
                document, current = self._locked_document(session, document_id)
                if document.current_version != expected_document_version:
                    raise KnowledgeConflictError("文档已被其他管理员更新，请重新读取")
                if not archived and restore_enabled and (
                    self._enabled_bytes_excluding(session, document_id) + current.byte_size > self.max_total_bytes
                ):
                    raise KnowledgeValidationError("启用文档总大小不能超过 5 MiB")
                document.archived_at = now if archived else None
                document.enabled = not archived and restore_enabled
                document.updated_by = actor
                document.updated_at = now
                self._advance_state(state, actor, now)
                next_revision = state.revision
                session.commit()
        except (KnowledgeConflictError, KnowledgeNotFoundError, KnowledgeValidationError):
            raise
        except SQLAlchemyError as exc:
            raise KnowledgePersistenceError("知识文档状态暂时无法更新") from exc
        return KnowledgeMutationView(
            revision=next_revision,
            document=self.get_document(document_id),
        )

    def restore_version(
        self,
        document_id: str,
        version_number: int,
        *,
        expected_revision: int,
        expected_document_version: int,
        actor: str,
    ) -> KnowledgeMutationView:
        if version_number <= 0:
            raise KnowledgeValidationError("历史版本号必须大于 0")
        now = self._clock()
        try:
            with self._sessions() as session:
                state = self._lock_state(session, expected_revision, actor, now)
                document, current = self._locked_document(session, document_id)
                if document.archived_at is not None:
                    raise KnowledgeValidationError("请先恢复归档文档再还原版本")
                if document.current_version != expected_document_version:
                    raise KnowledgeConflictError("文档已被其他管理员更新，请重新读取")
                historical = session.scalar(
                    select(KnowledgeDocumentVersionRecord).where(
                        KnowledgeDocumentVersionRecord.document_id == document_id,
                        KnowledgeDocumentVersionRecord.version == version_number,
                    )
                )
                if historical is None:
                    raise KnowledgeNotFoundError("知识文档历史版本不存在")
                if historical.content_sha256 == current.content_sha256:
                    raise KnowledgeValidationError("所选版本内容与当前版本相同")
                enabled_bytes = self._enabled_bytes_excluding(session, document_id)
                if document.enabled and (
                    enabled_bytes + historical.byte_size > self.max_total_bytes
                ):
                    raise KnowledgeValidationError("启用文档总大小不能超过 5 MiB")
                document.current_version += 1
                document.updated_by = actor
                document.updated_at = now
                session.add(
                    KnowledgeDocumentVersionRecord(
                        id=str(uuid4()),
                        document_id=document_id,
                        version=document.current_version,
                        content=historical.content,
                        content_sha256=historical.content_sha256,
                        byte_size=historical.byte_size,
                        created_by=actor,
                        created_at=now,
                    )
                )
                self._advance_state(state, actor, now)
                next_revision = state.revision
                session.commit()
        except (
            KnowledgeConflictError,
            KnowledgeNotFoundError,
            KnowledgeValidationError,
        ):
            raise
        except IntegrityError as exc:
            raise KnowledgeConflictError("知识文档已被其他管理员更新") from exc
        except SQLAlchemyError as exc:
            raise KnowledgePersistenceError("知识文档版本暂时无法还原") from exc
        return KnowledgeMutationView(
            revision=next_revision,
            document=self.get_document(document_id),
        )

    def install_project_pack(self, expected_revision: int, actor: str) -> KnowledgeLibraryView:
        """批量补充内置项目资料，不覆盖已有文档或人工编辑。"""
        self._ensure_seeded()
        seeds = tuple(seed for seed in self._seed_documents if seed.repository_scope)
        now = self._clock()
        with self._sessions() as session, session.begin():
            state = self._lock_state(session, expected_revision, actor, now)
            existing = set(session.scalars(select(KnowledgeDocumentRecord.source).where(
                KnowledgeDocumentRecord.source.in_([seed.source for seed in seeds]),
            ).limit(self.max_files)))
            missing = [seed for seed in seeds if seed.source not in existing]
            count = session.scalar(select(func.count(KnowledgeDocumentRecord.id))) or 0
            _, enabled_bytes = self._enabled_totals(session)
            if count + len(missing) > self.max_files or enabled_bytes + sum(s.byte_size for s in missing) > self.max_total_bytes:
                raise KnowledgeValidationError("项目资料超过知识库文档或容量上限")
            documents, versions = [], []
            for seed in missing:
                identifier = str(uuid4())
                documents.append(dict(id=identifier, source=seed.source, repository_scope=seed.repository_scope,
                    enabled=True, current_version=1, created_by=actor, updated_by=actor, created_at=now, updated_at=now))
                versions.append(dict(id=str(uuid4()), document_id=identifier, version=1,
                    content=seed.content, content_sha256=seed.content_sha256, byte_size=seed.byte_size,
                    created_by=actor, created_at=now))
            if documents:
                session.execute(insert(KnowledgeDocumentRecord), documents)
                session.execute(insert(KnowledgeDocumentVersionRecord), versions)
                self._advance_state(state, actor, now)
        return self.list_documents()

    def _ensure_seeded(self) -> None:
        if self._seed_is_checked():
            return
        with self._cache_lock:
            if self._seed_is_checked():
                return
            now = self._clock()
            try:
                with self._sessions() as session:
                    state = self._lock_state(session, None, "system:seed", now)
                    existing = session.scalar(
                        select(KnowledgeDocumentRecord.id).limit(1)
                    )
                    if existing is None and self._seed_documents:
                        for seed in self._seed_documents:
                            document_id = str(uuid4())
                            session.add(
                                KnowledgeDocumentRecord(
                                    id=document_id,
                                    source=seed.source,
                                    repository_scope=seed.repository_scope,
                                    enabled=True,
                                    current_version=1,
                                    created_by="system:seed",
                                    updated_by="system:seed",
                                    created_at=now,
                                    updated_at=now,
                                )
                            )
                            session.add(
                                KnowledgeDocumentVersionRecord(
                                    id=str(uuid4()),
                                    document_id=document_id,
                                    version=1,
                                    content=seed.content,
                                    content_sha256=seed.content_sha256,
                                    byte_size=seed.byte_size,
                                    created_by="system:seed",
                                    created_at=now,
                                )
                            )
                        self._advance_state(state, "system:seed", now)
                    session.commit()
                    self._seed_checked = True
            except (KnowledgeConflictError, KnowledgeValidationError):
                raise
            except IntegrityError as exc:
                raise KnowledgeConflictError("知识库初始化发生并发冲突") from exc
            except SQLAlchemyError as exc:
                raise KnowledgePersistenceError("知识库暂时无法初始化") from exc

    def _seed_is_checked(self) -> bool:
        """在线程锁两侧读取标记，保留双重检查的并发语义。"""

        return self._seed_checked

    def _lock_state(
        self,
        session: Session,
        expected_revision: int | None,
        actor: str,
        now: datetime,
    ) -> KnowledgeLibraryRecord:
        state = session.scalar(
            select(KnowledgeLibraryRecord)
            .where(KnowledgeLibraryRecord.id == 1)
            .with_for_update()
        )
        if state is None:
            state = KnowledgeLibraryRecord(
                id=1,
                revision=0,
                updated_by=actor,
                updated_at=now,
            )
            session.add(state)
            session.flush()
        if expected_revision is not None and state.revision != expected_revision:
            raise KnowledgeConflictError("知识库已被其他管理员更新，请刷新后重试")
        return state

    @staticmethod
    def _advance_state(
        state: KnowledgeLibraryRecord,
        actor: str,
        now: datetime,
    ) -> None:
        state.revision += 1
        state.updated_by = actor
        state.updated_at = now

    @staticmethod
    def _locked_document(
        session: Session,
        document_id: str,
    ) -> tuple[KnowledgeDocumentRecord, KnowledgeDocumentVersionRecord]:
        row = session.execute(
            select(KnowledgeDocumentRecord, KnowledgeDocumentVersionRecord)
            .join(
                KnowledgeDocumentVersionRecord,
                and_(
                    KnowledgeDocumentVersionRecord.document_id
                    == KnowledgeDocumentRecord.id,
                    KnowledgeDocumentVersionRecord.version
                    == KnowledgeDocumentRecord.current_version,
                ),
            )
            .where(KnowledgeDocumentRecord.id == document_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise KnowledgeNotFoundError("知识文档不存在")
        return row[0], row[1]

    @staticmethod
    def _enabled_totals(session: Session) -> tuple[int, int]:
        row = session.execute(
            select(
                func.count(KnowledgeDocumentRecord.id),
                func.coalesce(func.sum(KnowledgeDocumentVersionRecord.byte_size), 0),
            )
            .join(
                KnowledgeDocumentVersionRecord,
                and_(
                    KnowledgeDocumentVersionRecord.document_id
                    == KnowledgeDocumentRecord.id,
                    KnowledgeDocumentVersionRecord.version
                    == KnowledgeDocumentRecord.current_version,
                ),
            )
            .where(
                KnowledgeDocumentRecord.enabled.is_(True),
                KnowledgeDocumentRecord.archived_at.is_(None),
            )
        ).one()
        return int(row[0] or 0), int(row[1] or 0)

    @staticmethod
    def _enabled_bytes_excluding(session: Session, document_id: str) -> int:
        return int(session.scalar(
            select(
                func.coalesce(func.sum(KnowledgeDocumentVersionRecord.byte_size), 0)
            )
            .join(
                KnowledgeDocumentRecord,
                and_(
                    KnowledgeDocumentRecord.id
                    == KnowledgeDocumentVersionRecord.document_id,
                    KnowledgeDocumentRecord.current_version
                    == KnowledgeDocumentVersionRecord.version,
                ),
            )
            .where(
                KnowledgeDocumentRecord.enabled.is_(True),
                KnowledgeDocumentRecord.archived_at.is_(None),
                KnowledgeDocumentRecord.id != document_id,
            )
        ) or 0)


def _read_seed_documents(
    root: Path,
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> tuple[_SeedDocument, ...]:
    if not root.exists():
        return ()
    total = 0
    result: list[_SeedDocument] = []
    paths = sorted(
        path
        for path in root.rglob("*.md")
        if path.is_file() and not path.is_symlink()
    )[:max_files]
    for path in paths:
        try:
            size = path.stat().st_size
            if size <= 0 or size > max_file_bytes or total + size > max_total_bytes:
                continue
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        encoded = content.encode("utf-8")
        if not encoded:
            continue
        total += len(encoded)
        result.append(
            _SeedDocument(
                source=path.relative_to(root).as_posix(),
                content=content,
                content_sha256=sha256(encoded).hexdigest(),
                byte_size=len(encoded),
                repository_scope=_source_repository_scope(content),
            )
        )
    return tuple(result)


def _validate_source(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if not normalized or len(normalized) > 200:
        raise KnowledgeValidationError("文档路径长度必须在 1 到 200 个字符之间")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix.casefold() != ".md"
    ):
        raise KnowledgeValidationError("文档路径必须是知识库内的 .md 相对路径")
    return path.as_posix()


def _validate_content(value: str, max_bytes: int) -> tuple[str, str, int]:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if normalized:
        normalized += "\n"
    encoded = normalized.encode("utf-8")
    if not encoded:
        raise KnowledgeValidationError("Markdown 内容不能为空")
    if len(encoded) > max_bytes:
        raise KnowledgeValidationError("单份 Markdown 文档不能超过 512 KiB")
    return normalized, sha256(encoded).hexdigest(), len(encoded)


def _document_title(source: str, content: str) -> str:
    heading = next(
        (
            line.lstrip("#").strip()
            for line in content.splitlines()
            if line.startswith("#") and line.lstrip("#").strip()
        ),
        "",
    )
    return (heading or PurePosixPath(source).stem)[:200]


def _source_repository_scope(content: str) -> str | None:
    match = re.search(r"^适用仓库：[ \t]*([^\r\n]+)$", content, re.M)
    return _validate_repository_scope(match.group(1) if match else None)


def _validate_repository_scope(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().casefold()
    if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", normalized) or len(normalized) > 255:
        raise KnowledgeValidationError("适用仓库应填写 owner/repository，留空表示通用规则")
    return normalized


def _summary(
    document: KnowledgeDocumentRecord,
    version: KnowledgeDocumentVersionRecord,
) -> KnowledgeDocumentSummary:
    return KnowledgeDocumentSummary(
        id=document.id,
        source=document.source,
        title=_document_title(document.source, version.content),
        enabled=document.enabled,
        archived=document.archived_at is not None,
        current_version=document.current_version,
        content_sha256=version.content_sha256,
        byte_size=version.byte_size,
        created_by=document.created_by,
        updated_by=document.updated_by,
        created_at=document.created_at,
        updated_at=document.updated_at,
        repository_scope=document.repository_scope,
    )


def _version_view(
    version: KnowledgeDocumentVersionRecord,
) -> KnowledgeVersionView:
    return KnowledgeVersionView(
        version=version.version,
        content_sha256=version.content_sha256,
        byte_size=version.byte_size,
        created_by=version.created_by,
        created_at=version.created_at,
    )


def _document_view(
    document: KnowledgeDocumentRecord,
    version: KnowledgeDocumentVersionRecord,
    versions: tuple[KnowledgeVersionView, ...],
) -> KnowledgeDocumentView:
    summary = _summary(document, version)
    return KnowledgeDocumentView(
        id=summary.id,
        source=summary.source,
        title=summary.title,
        enabled=summary.enabled,
        archived=summary.archived,
        current_version=summary.current_version,
        content_sha256=summary.content_sha256,
        byte_size=summary.byte_size,
        created_by=summary.created_by,
        updated_by=summary.updated_by,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
        repository_scope=summary.repository_scope,
        content=version.content,
        versions=versions,
    )


def _split_markdown(source: str, text: str, version: str) -> Iterable[KnowledgeChunk]:
    """按标题、段落和代码围栏切分 Markdown，确保长章节不会被截断。"""

    heading = source
    section_lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        # 仓库范围已作为 chunk 元数据保存，不把单独的范围标记召回给模型。
        if not in_fence and re.fullmatch(r"适用仓库：[ \t]*[^\r\n]+", line):
            continue
        is_heading = bool(re.match(r"^#{1,6}(?:\s|$)", line)) and not in_fence
        if is_heading:
            yield from _section_chunks(source, heading, section_lines, version)
            section_lines = []
            heading = line.lstrip("#").strip() or source
            continue
        section_lines.append(line)
        if line.lstrip().startswith("```") or line.lstrip().startswith("~~~"):
            in_fence = not in_fence
    yield from _section_chunks(source, heading, section_lines, version)


_MAX_CHUNK_CHARACTERS = 20_000
_CHUNK_OVERLAP_CHARACTERS = 240


def _section_chunks(
    source: str,
    heading: str,
    lines: list[str],
    version: str,
) -> Iterable[KnowledgeChunk]:
    content = "\n".join(lines).strip()
    if not content:
        return
    blocks = _markdown_blocks(content)
    current: list[str] = []
    current_length = 0
    for block in blocks:
        block_length = len(block)
        if current and current_length + 2 + block_length > _MAX_CHUNK_CHARACTERS:
            chunk_content = "\n\n".join(current).strip()
            if chunk_content:
                yield _chunk(source, heading, chunk_content, version)
            overlap = chunk_content[-_CHUNK_OVERLAP_CHARACTERS:]
            if overlap and len(overlap) + 2 + block_length <= _MAX_CHUNK_CHARACTERS:
                current = [overlap, block]
            else:
                current = [block]
            current_length = sum(len(item) for item in current) + max(0, len(current) - 1) * 2
            continue
        if block_length > _MAX_CHUNK_CHARACTERS:
            if current:
                yield _chunk(source, heading, "\n\n".join(current), version)
                current = []
                current_length = 0
            for fragment in _hard_split_text(block, _MAX_CHUNK_CHARACTERS):
                yield _chunk(source, heading, fragment, version)
            continue
        current.append(block)
        current_length += block_length + (2 if len(current) > 1 else 0)
    if current:
        yield _chunk(source, heading, "\n\n".join(current), version)


def _markdown_blocks(content: str) -> tuple[str, ...]:
    """按空行切块，但不拆开 fenced code block。"""

    lines = content.splitlines()
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in lines:
        stripped = line.lstrip()
        fence = stripped.startswith("```") or stripped.startswith("~~~")
        if not in_fence and not line.strip() and current:
            blocks.append("\n".join(current).strip())
            current = []
            continue
        current.append(line)
        if fence:
            in_fence = not in_fence
    if current:
        blocks.append("\n".join(current).strip())
    return tuple(block for block in blocks if block)


def _hard_split_text(value: str, limit: int) -> tuple[str, ...]:
    if limit <= 0:
        raise ValueError("text split limit must be positive")
    fragments: list[str] = []
    remaining = value
    while remaining:
        if len(remaining) <= limit:
            fragments.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut < max(1, limit // 2):
            cut = limit
        fragments.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return tuple(fragment for fragment in fragments if fragment)


def _chunk(source: str, heading: str, content: str, version: str) -> KnowledgeChunk:
    return KnowledgeChunk(
        source=source,
        heading=heading,
        content=content,
        content_sha256=sha256(content.encode("utf-8")).hexdigest(),
        version=version,
    )


def _tokens(value: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for item in _TOKEN.findall(value):
        normalized = item.casefold()
        tokens.append(normalized)
        if _CJK_RUN.fullmatch(item):
            # 不引入重量级分词库；相邻双字词足以覆盖中文规则中的常用术语，
            # 同时保留整段词以支持精确匹配和英文/数字标识符。
            tokens.extend(
                item[index : index + 2]
                for index in range(len(item) - 1)
            )
        else:
            # 保留完整标识符，同时让 Java 类名和 SQL 字段命中业务术语。
            tokens.extend(part.casefold() for part in _IDENTIFIER_PARTS.findall(item)
                          if len(part) > 1 and part.casefold() != normalized)
    return tuple(tokens)


def _token_counts(value: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in _tokens(value):
        counts[token] = counts.get(token, 0) + 1
    return counts


def _excerpt(value: str, limit: int = 360) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"
