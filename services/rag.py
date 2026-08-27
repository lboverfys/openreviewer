"""无需向量基础设施的 Markdown 知识库检索。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
import re
from threading import RLock
from typing import Callable, Iterable
from uuid import uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from persistence.models import (
    KnowledgeDocumentRecord,
    KnowledgeDocumentVersionRecord,
    KnowledgeLibraryRecord,
)


_TOKEN = re.compile(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}")
_CJK_RUN = re.compile(r"^[\u4e00-\u9fff]+$")


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    source: str
    heading: str
    content: str
    content_sha256: str
    version: str


@dataclass(frozen=True, slots=True)
class RagCitation:
    source: str
    heading: str
    score: float
    excerpt: str
    version: str


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


@dataclass(frozen=True, slots=True)
class KnowledgeDocumentView(KnowledgeDocumentSummary):
    content: str
    versions: tuple[KnowledgeVersionView, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeLibraryView:
    revision: int
    total: int
    enabled_count: int
    total_enabled_bytes: int
    items: tuple[KnowledgeDocumentSummary, ...]


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
        if max_files <= 0 or max_file_bytes <= 0 or max_total_bytes < max_file_bytes:
            raise ValueError("知识库边界无效")

    def chunks(self) -> tuple[KnowledgeChunk, ...]:
        if self._chunks_cache is not None:
            return self._chunks_cache
        if not self.root.exists():
            self._chunks_cache = ()
            return self._chunks_cache
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
            chunks.extend(_split_markdown(relative, text, version))
        self._chunks_cache = tuple(chunks)
        return self._chunks_cache

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
        scored: list[tuple[float, KnowledgeChunk]] = []
        for chunk in self.chunks() if chunks is None else chunks:
            tokens = set(
                _tokens(f"{chunk.source} {chunk.heading} {chunk.content}")
            )
            overlap = len(query_tokens & tokens)
            if overlap == 0:
                continue
            score = overlap / max(1, len(query_tokens))
            if normalized.casefold() in chunk.content.casefold():
                score += 0.25
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
                    _split_markdown(
                        row.source,
                        row.content,
                        row.content_sha256[:16],
                    )
                )
            self._chunks_cache = tuple(chunks)
            self._cache_revision = revision
            return self._chunks_cache

    def list_documents(
        self,
        *,
        include_archived: bool = False,
        limit: int = 128,
    ) -> KnowledgeLibraryView:
        if not 1 <= limit <= self.max_files:
            raise KnowledgeValidationError("知识文档数量必须在 1 到 128 之间")
        self._ensure_seeded()
        try:
            with self._sessions() as session:
                state = session.get(KnowledgeLibraryRecord, 1)
                if state is None:
                    raise KnowledgePersistenceError("知识库版本暂时无法读取")
                filters = () if include_archived else (
                    KnowledgeDocumentRecord.archived_at.is_(None),
                )
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
                        KnowledgeDocumentRecord.archived_at.asc(),
                        KnowledgeDocumentRecord.source.asc(),
                    )
                    .limit(limit)
                ).all()
                total = session.scalar(
                    select(func.count(KnowledgeDocumentRecord.id)).where(*filters)
                ) or 0
                enabled_count, enabled_bytes = self._enabled_totals(session)
                return KnowledgeLibraryView(
                    revision=state.revision,
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

    def get_document(self, document_id: str) -> KnowledgeDocumentView:
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
                versions = tuple(
                    _version_view(version)
                    for version in session.scalars(
                        select(KnowledgeDocumentVersionRecord)
                        .where(
                            KnowledgeDocumentVersionRecord.document_id
                            == document_id
                        )
                        .order_by(
                            KnowledgeDocumentVersionRecord.version.desc()
                        )
                        .limit(50)
                    )
                )
                return _document_view(row[0], row[1], versions)
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
    ) -> KnowledgeMutationView:
        normalized_source = _validate_source(source)
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
    ) -> KnowledgeMutationView:
        normalized_source = _validate_source(source)
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
                if current.content_sha256 != content_hash:
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
    ) -> KnowledgeMutationView:
        now = self._clock()
        try:
            with self._sessions() as session:
                state = self._lock_state(session, expected_revision, actor, now)
                document, _ = self._locked_document(session, document_id)
                if document.current_version != expected_document_version:
                    raise KnowledgeConflictError("文档已被其他管理员更新，请重新读取")
                document.archived_at = now if archived else None
                document.enabled = False
                document.updated_by = actor
                document.updated_at = now
                self._advance_state(state, actor, now)
                next_revision = state.revision
                session.commit()
        except (KnowledgeConflictError, KnowledgeNotFoundError):
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

    def _ensure_seeded(self) -> None:
        if self._seed_checked:
            return
        with self._cache_lock:
            if self._seed_checked:
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
        content=version.content,
        versions=versions,
    )


def _split_markdown(source: str, text: str, version: str) -> Iterable[KnowledgeChunk]:
    heading = source
    buffer: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            if buffer:
                content = "\n".join(buffer).strip()
                if content:
                    yield _chunk(source, heading, content, version)
                buffer = []
            heading = line.lstrip("#").strip() or source
        else:
            buffer.append(line)
    if buffer:
        content = "\n".join(buffer).strip()
        if content:
            yield _chunk(source, heading, content, version)


def _chunk(source: str, heading: str, content: str, version: str) -> KnowledgeChunk:
    return KnowledgeChunk(
        source=source,
        heading=heading,
        content=content[:20_000],
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
    return tuple(tokens)


def _excerpt(value: str, limit: int = 360) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"
