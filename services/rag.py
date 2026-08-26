"""无需向量基础设施的 Markdown 知识库检索。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from typing import Iterable


_TOKEN = re.compile(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}")


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

    def search(self, query: str, *, limit: int = 5) -> tuple[RagCitation, ...]:
        normalized = query.strip()
        if not normalized:
            return ()
        if not 1 <= limit <= 20:
            raise ValueError("知识库检索数量必须在 1 到 20 之间")
        query_tokens = set(_tokens(normalized))
        if not query_tokens:
            return ()
        scored: list[tuple[float, KnowledgeChunk]] = []
        for chunk in self.chunks():
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
    return tuple(item.casefold() for item in _TOKEN.findall(value))


def _excerpt(value: str, limit: int = 360) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"
