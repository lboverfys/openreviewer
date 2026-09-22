"""按变更组构造有界查询，轮流分配上下文名额并保留适用的审查单元。"""

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence

from domain.retrieval import ContextBudget, ContextEvidence, RetrievalTrace, SearchQuery
from domain.review_planning import ReviewUnit
from services.code_indexing import code_tokens
from services.retrieval_lexical import Document


def review_queries(units: Sequence[ReviewUnit], strategy, limit: int) -> tuple[tuple[SearchQuery, tuple[str, ...]], ...]:
    groups: dict[str, list[ReviewUnit]] = defaultdict(list)
    for unit in units:
        groups[unit.group_key or unit.file].append(unit)
    values = list(groups.values())
    # 每个角色最多8组，所有变更单元均分配给一组，不再只读取前4个单元。
    width = max(1, math.ceil(len(values) / 8))
    result = []
    for offset in range(0, len(values), width):
        group = [unit for items in values[offset:offset + width] for unit in items]
        terms: dict[str, int] = {}
        for unit in group:
            # 优先使用变更行和路径，控制单个大补丁对查询的支配。
            changed = "\n".join(line[1:] for line in unit.patch.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
            for token in code_tokens(unit.file + " " + changed):
                terms[token] = terms.get(token, 0) + 1
        # 高频跨文件标识符在前，稳定排序；限制同一查询长度。
        query = " ".join(sorted(terms, key=lambda term: (-terms[term], term)))[:3000] or "changed implementation"
        result.append((SearchQuery(query=query, seed_files=tuple(dict.fromkeys(unit.file for unit in group))[:100], strategy=strategy, limit=limit), tuple(unit.unit_key for unit in group)))
    return tuple(result)


def changed_symbols(documents: Iterable[Document], units: Sequence[ReviewUnit]) -> tuple[str, ...]:
    ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for unit in units:
        ranges[unit.file].extend((int(match.group(1)), int(match.group(1)) + max(1, int(match.group(2) or 1)) - 1)
            for match in re.finditer(r"^@@ -[0-9]+(?:,[0-9]+)? [+]([0-9]+)(?:,([0-9]+))? @@", unit.patch, re.M))
    return tuple(dict.fromkeys(symbol for file, symbol, _, first, last in documents
        if file in ranges and any(first <= end and last >= begin for begin, end in ranges[file])))[:100]


def select_contexts(candidates: Sequence[ContextEvidence], limit: int, max_bytes: int, *,
                    distinct_symbols: bool = False) -> tuple[tuple[ContextEvidence, ...], ContextBudget]:
    selected = size = excluded = 0
    symbols: set[tuple[str, str]] = set()
    result = []
    for item in candidates:
        identity = (item.file, item.symbol)
        include = False
        if selected < limit and (not distinct_symbols or identity not in symbols):
            amount = len(item.content.encode())
            if size + amount <= max_bytes:
                include = True
                selected += 1
                size += amount
                symbols.add(identity)
            else:
                excluded += 1
        result.append(item.model_copy(update={"selected": include}))
    return tuple(result), ContextBudget(snippet_limit=limit, byte_limit=max_bytes,
        selected_bytes=size, excluded_by_size=excluded)


def merge_contexts(traces: Sequence[tuple[RetrievalTrace, tuple[str, ...]]], limit: int,
                   max_bytes: int = 24_000) -> tuple[tuple[ContextEvidence, ...], ContextBudget]:
    """多查询轮流选证据，合并重复片段，限制同一符号的碎片和正文总量。"""
    by_id: dict[str, ContextEvidence] = {}
    order = []
    for rank in range(max((len(trace.candidates) for trace, _ in traces), default=0)):
        for trace, keys in traces:
            if rank >= len(trace.candidates):
                continue
            item = trace.candidates[rank]
            previous = by_id.get(item.reference_id)
            if previous is None:
                order.append(item.reference_id)
                by_id[item.reference_id] = item.model_copy(update={"unit_keys": keys})
            else:
                by_id[item.reference_id] = previous.model_copy(update={"unit_keys": tuple(dict.fromkeys((*previous.unit_keys, *keys)))})
    candidates = tuple(by_id[key].model_copy(update={"rank": rank}) for rank, key in enumerate(order[:30], 1))
    return select_contexts(candidates, limit, max_bytes, distinct_symbols=True)
