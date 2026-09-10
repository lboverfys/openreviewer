"""不可变词项索引、Top-K 评分与有内存预算的多版本缓存。"""

import math
import sys
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import Future
from heapq import nsmallest
from threading import RLock, Semaphore
from typing import Any

from domain.retrieval import SearchQuery
from services.code_indexing import code_tokens

Document = tuple[str, str, int, int, int]
SourceDocument = tuple[str, str, str, dict[str, int], int, int]


def _retained_size(*roots: object) -> int:
    """按对象身份去重；词、路径和代码块ID共享的内存只计算一次。"""
    pending: list[Any] = list(roots)
    seen: set[int] = set()
    total = 0
    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(value)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (tuple, list, set)):
            pending.extend(value)
    return total


class LexicalIndex:
    def __init__(self, documents: Iterable[SourceDocument], *, query_capacity: int = 128) -> None:
        self.documents: dict[str, Document] = {}
        self.postings: dict[str, dict[str, float]] = defaultdict(dict)
        self._files: dict[str, list[str]] = defaultdict(list)
        self._symbols: dict[str, list[str]] = defaultdict(list)
        self._order: dict[str, int] = {}
        paths: dict[str, str] = {}
        symbols: dict[str, str] = {}
        total_length = 0
        for chunk_id, file, symbol, original_tokens, start_line, end_line in documents:
            file, symbol = paths.setdefault(file, file), symbols.setdefault(symbol, symbol)
            length = sum(original_tokens.values())
            self._order[chunk_id] = len(self.documents)
            self.documents[chunk_id] = file, symbol, length, start_line, end_line
            self._files[file].append(chunk_id)
            self._symbols[symbol].append(chunk_id)
            total_length += length
            for token, frequency in original_tokens.items():
                self.postings[token][chunk_id] = frequency
        self.average_length = max(1.0, total_length / max(1, len(self.documents)))
        count = len(self.documents)
        norms = {key: 1.5 * (0.25 + 0.75 * document[2] / self.average_length) for key, document in self.documents.items()}
        for hits in self.postings.values():
            inverse = math.log(1 + (count - len(hits) + 0.5) / (len(hits) + 0.5))
            for key, term_frequency in hits.items():
                hits[key] = inverse * term_frequency * 2.5 / (term_frequency + norms[key])
        self._queries: OrderedDict[tuple[str, int], tuple[tuple[str, float], ...]] = OrderedDict()
        self._query_capacity = max(0, min(128, query_capacity))
        self._query_lock = RLock()
        # 利用已知的共享结构计量，不在冷加载时再遍历全部倒排边。
        # 容器大小包含引用槽位；ID、路径与符号正文分别只计一次。
        size = sys.getsizeof(self.documents) + sum(sys.getsizeof(key) + sys.getsizeof(value)
            + sum(sys.getsizeof(number) for number in value[2:]) for key, value in self.documents.items())
        size += sum(sys.getsizeof(value) for value in paths.values()) + sum(sys.getsizeof(value) for value in symbols.values())
        size += sys.getsizeof(self._files) + sum(sys.getsizeof(items) for items in self._files.values())
        size += sys.getsizeof(self._symbols) + sum(sys.getsizeof(items) for items in self._symbols.values())
        size += sys.getsizeof(self._order) + len(self._order) * sys.getsizeof(0)
        size += sys.getsizeof(self.postings) + sum(sys.getsizeof(word) + sys.getsizeof(hits)
            + len(hits) * sys.getsizeof(0.0) for word, hits in self.postings.items())
        # 另留5%计量余量与最多128条有界查询的空间，不等同进程RSS硬上限。
        self.estimated_bytes = math.ceil(size * 1.05) + 3 * 1024 * 1024

    def search(self, query: str, limit: int) -> list[tuple[str, float]]:
        key = query, limit
        with self._query_lock:
            cached = self._queries.get(key)
            if cached is not None:
                self._queries.move_to_end(key)
                return list(cached)
        scores: dict[str, float] = defaultdict(float)
        # 只访问包含查询词的倒排列表，权重和长度归一化已按版本预计算。
        for term in sorted(code_tokens(query)):
            hits = self.postings.get(term)
            if hits is None:
                continue
            for chunk_id, contribution in hits.items():
                scores[chunk_id] += contribution
        result = nsmallest(limit, scores.items(), key=lambda item: (-item[1], item[0]))
        if self._query_capacity and len(query) <= 4000 and 0 < limit <= 50:
            with self._query_lock:
                self._queries[key] = tuple(result)
                self._queries.move_to_end(key)
                while len(self._queries) > self._query_capacity:
                    self._queries.popitem(last=False)
        return result

    def seeds(self, query: SearchQuery) -> list[str]:
        mapping, keys = (self._symbols, query.symbols) if query.symbols else (self._files, query.seed_files)
        ids = {item for key in keys for item in mapping.get(key, ())}
        return nsmallest(100, ids, key=self._order.__getitem__)

    def documents_for_files(self, files: Sequence[str]) -> Iterator[Document]:
        ids = {item for file in files for item in self._files.get(file, ())}
        return (self.documents[key] for key in sorted(ids, key=self._order.__getitem__))


class LexicalSnapshotCache:
    def __init__(self, *, max_bytes: int = 128 * 1024 * 1024, capacity: int = 4) -> None:
        self.max_bytes, self.capacity = max_bytes, capacity
        self.retained_bytes = 0
        self._entries: OrderedDict[str, LexicalIndex] = OrderedDict()
        self._loading: dict[str, Future[LexicalIndex]] = {}
        self._lock = RLock()
        self._cold_lane = Semaphore(1)

    def get(self, key: str, loader: Callable[[], LexicalIndex]) -> LexicalIndex:
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return cached
            future = self._loading.get(key)
            owner = future is None
            if future is None:
                future = Future()
                self._loading[key] = future
        if not owner:
            return future.result()
        try:
            # 冷加载串行控制峰值；已命中的其他版本不等待数据库加载。
            with self._cold_lane:
                value = loader()
                with self._lock:
                    if value.estimated_bytes <= self.max_bytes:
                        while self._entries and (len(self._entries) >= self.capacity or self.retained_bytes + value.estimated_bytes > self.max_bytes):
                            _, expired = self._entries.popitem(last=False)
                            self.retained_bytes -= expired.estimated_bytes
                        self._entries[key] = value
                        self.retained_bytes += value.estimated_bytes
                    future.set_result(value)
                return value
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._loading.pop(key, None)
