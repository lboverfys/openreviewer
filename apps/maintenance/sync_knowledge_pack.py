"""显式更新已知内置知识；默认只预览，批量写入保留历史和人工修改。"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, case, func, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from persistence.database import Database
from persistence.models import (
    KnowledgeDocumentRecord as Document,
)
from persistence.models import (
    KnowledgeDocumentVersionRecord as Version,
)
from persistence.models import (
    KnowledgeLibraryRecord as Library,
)
from services.rag import (
    KnowledgeConflictError,
    KnowledgeValidationError,
    _read_seed_documents,
    _validate_source,
)


def sync_pack(
    sessions: sessionmaker[Session], root: Path, *,
    apply: bool = False, expected_revision: int | None = None,
    actor: str = "maintenance:knowledge-curation",
) -> dict[str, Any]:
    """知识库最多 128 份；按全局版本锁和旧内容指纹一次更新整包。"""
    if apply and expected_revision is None:
        raise KnowledgeValidationError("写入必须提供预览时的知识库版本")
    manifest = json.loads((root / "curation-pack.json").read_text(encoding="utf-8"))
    sources = {_validate_source(source) for source in manifest["sources"]}
    previous = {_validate_source(source): hashes for source, hashes in manifest["previous"].items()}
    scopes = manifest["scopes"]
    if not sources or len(sources | previous.keys()) > 128 or any(
        not isinstance(hashes, list) or not hashes or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes
        ) for hashes in previous.values()
    ):
        raise KnowledgeValidationError("知识包清单或旧内容指纹无效")
    seeds = {seed.source: seed for seed in _read_seed_documents(
        root.resolve(), max_files=128, max_file_bytes=512 * 1024, max_total_bytes=5 * 1024 * 1024,
    ) if seed.source in sources}
    if seeds.keys() != sources:
        raise KnowledgeValidationError("知识包缺少文档或文档超过容量限制")
    now = datetime.now(UTC)
    with sessions() as session, session.begin():
        statement = select(Library).where(Library.id == 1)
        state = session.scalar(statement.with_for_update() if apply else statement)
        if state is None:
            raise KnowledgeValidationError("知识库尚未初始化")
        if expected_revision is not None and state.revision != expected_revision:
            raise KnowledgeConflictError("知识库已变化，请重新预览")
        # source 唯一索引 + (document_id, version) 唯一索引；只读本包元数据。
        rows = session.execute(select(
            Document.id, Document.source, Document.repository_scope, Document.enabled,
            Document.archived_at, Document.current_version, Version.content_sha256, Version.byte_size,
        ).join(Version, and_(Version.document_id == Document.id, Version.version == Document.current_version))
            .where(Document.source.in_(sorted(sources | previous.keys()))).limit(128)).mappings().all()
        existing = {row.source: row for row in rows}
        report: dict[str, Any] = dict(revision=state.revision, applied=False,
            create=[], update=[], archive=[], unchanged=[], conflicts=[])
        new_documents: list[dict[str, Any]] = []
        versions: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        byte_delta = 0
        for source in sorted(sources | previous.keys()):
            row, seed = existing.get(source), seeds.get(source)
            if seed is None:
                if row is None or row.archived_at is not None:
                    report["unchanged"].append(source)
                    continue
                if row.content_sha256 not in previous[source] or row.repository_scope != scopes[source]:
                    report["conflicts"].append(source)
                    continue
                report["archive"].append(source)
                byte_delta -= row.byte_size if row.enabled else 0
                updates.append(dict(id=row.id, archived_at=now, enabled=False,
                    current_version=row.current_version, repository_scope=row.repository_scope,
                    updated_by=actor, updated_at=now))
                continue
            if row is not None:
                if row.content_sha256 == seed.content_sha256 and row.repository_scope == seed.repository_scope:
                    report["unchanged"].append(source)
                    continue
                if row.content_sha256 not in previous.get(source, ()) or row.repository_scope != seed.repository_scope:
                    report["conflicts"].append(source)
                    continue
                report["update"].append(source)
                identifier, version = row.id, row.current_version + 1
                if row.enabled and row.archived_at is None:
                    byte_delta += seed.byte_size - row.byte_size
                updates.append(dict(id=identifier, archived_at=row.archived_at, enabled=row.enabled,
                    current_version=version, repository_scope=row.repository_scope, updated_by=actor, updated_at=now))
            else:
                report["create"].append(source)
                identifier, version = str(uuid4()), 1
                byte_delta += seed.byte_size
                new_documents.append(dict(id=identifier, source=source, repository_scope=seed.repository_scope,
                    enabled=True, current_version=1, created_by=actor, updated_by=actor, created_at=now, updated_at=now))
            versions.append(dict(id=str(uuid4()), document_id=identifier, version=version, content=seed.content,
                content_sha256=seed.content_sha256, byte_size=seed.byte_size, created_by=actor, created_at=now))
        if not apply:
            return report
        if report["conflicts"]:
            raise KnowledgeConflictError("以下文档已修改，整包未写入：" + "、".join(report["conflicts"]))
        if not new_documents and not updates:
            return report
        total, enabled_bytes = session.execute(select(
            func.count(Document.id), func.coalesce(func.sum(case(
                (and_(Document.enabled.is_(True), Document.archived_at.is_(None)), Version.byte_size), else_=0,
            )), 0),
        ).join(Version, and_(Version.document_id == Document.id, Version.version == Document.current_version))
            .limit(1)).one()
        if total + len(new_documents) > 128 or enabled_bytes + byte_delta > 5 * 1024 * 1024:
            raise KnowledgeValidationError("更新后的知识库超过容量限制")
        if new_documents:
            session.execute(insert(Document), new_documents)
        if versions:
            session.execute(insert(Version), versions)
        if updates:
            session.execute(update(Document), updates)
        state.revision += 1
        state.updated_by, state.updated_at = actor, now
        report.update(revision=state.revision, applied=True)
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("knowledge"))
    parser.add_argument("--apply", action="store_true", help="按预览版本显式写入，默认只读")
    parser.add_argument("--expected-revision", type=int)
    args = parser.parse_args()
    database = Database.from_environment()
    try:
        result = sync_pack(database.sessions, args.root, apply=args.apply, expected_revision=args.expected_revision)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
