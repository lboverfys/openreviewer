"""静态提取 Java 方法与 MyBatis SQL；不运行仓库代码。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from hashlib import sha1, sha256
from pathlib import PurePosixPath
from xml.parsers import expat

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from domain.retrieval import (
    MAX_CHUNK_CHARS,
    MAX_INDEX_CHUNKS,
    MAX_INDEX_FILES,
    MAX_SOURCE_BYTES,
    PARSER_VERSION,
    CodeChunk,
    CodeRelation,
    ParsedSources,
    SourceFile,
    stable_key,
)

_JAVA_LANGUAGE = Language(tree_sitter_java.language())
_PACKAGE = re.compile(r"\bpackage\s+([\w.]+)\s*;")
_IMPORT = re.compile(r"\bimport\s+(?:static\s+)?([\w.]+)\s*;")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_WORDS = re.compile(r"[A-Za-z][A-Za-z0-9_]*|[\u4e00-\u9fff]+")
_TYPES = {"class_declaration", "interface_declaration", "record_declaration", "enum_declaration"}
_METHODS = {"method_declaration", "constructor_declaration"}


def git_blob_sha(content: str) -> str:
    raw = content.encode()
    return sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def code_tokens(text: str) -> dict[str, int]:
    tokens: list[str] = []
    for match in _WORDS.finditer(text):
        word = match.group()
        if "\u4e00" <= word[0] <= "\u9fff":
            tokens.extend(word[i:i + 2] for i in range(max(1, len(word) - 1)))
        else:
            tokens.append(word.casefold())
            tokens.extend(part.casefold() for part in _CAMEL.sub(r"\1 \2", word).replace("_", " ").split() if part.casefold() != word.casefold())
    return dict(Counter(tokens))


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8") if node is not None and node.text is not None else ""


def _nodes(node: Node) -> Iterator[Node]:
    pending = [node]
    while pending:
        current = pending.pop()
        yield current
        pending.extend(reversed(current.named_children))


def _chunks(
    source: SourceFile, text: str, start_line: int, *, language: str,
    kind: str, symbol: str, aliases: tuple[str, ...] = (),
    references: tuple[str, ...] = (), parse_error: bool = False,
) -> list[CodeChunk]:
    result: list[CodeChunk] = []
    pieces: list[tuple[str, int, int]] = []
    lines = text.splitlines(keepends=True) or [text]
    buffer = ""
    first = start_line
    line_number = start_line
    for line in lines:
        if buffer and len(buffer) + len(line) > MAX_CHUNK_CHARS:
            pieces.append((buffer, first, line_number - 1))
            buffer = ""
            first = line_number
        if len(line) > MAX_CHUNK_CHARS:
            for offset in range(0, len(line), MAX_CHUNK_CHARS):
                pieces.append((line[offset:offset + MAX_CHUNK_CHARS], line_number, line_number))
            first = line_number + 1
        else:
            buffer += line
        line_number += 1
    if buffer:
        pieces.append((buffer, first, line_number - 1))
    for fragment, (content, first_line, last_line) in enumerate(pieces):
        if not content.strip():
            continue
        digest = sha256(content.encode()).hexdigest()
        result.append(CodeChunk(
            id=stable_key(PARSER_VERSION, source.file, source.blob_sha, kind, symbol, first_line, fragment, digest),
            file=source.file, blob_sha=source.blob_sha, language=language, kind=kind,
            symbol=symbol, start_line=first_line, end_line=max(first_line, last_line),
            content=content, content_hash=digest, aliases=aliases, references=references,
            fragment=fragment, parse_error=parse_error,
        ))
    return result


def _java(source: SourceFile) -> list[CodeChunk]:
    raw_source = source.content.encode()
    parser = Parser(_JAVA_LANGUAGE)
    tree = parser.parse(raw_source)
    root = tree.root_node
    package_match = _PACKAGE.search(source.content)
    package = package_match.group(1) if package_match else ""
    imports = {value.rsplit(".", 1)[-1]: value for value in _IMPORT.findall(source.content)}
    result: list[CodeChunk] = []

    def qualified(type_name: str) -> str:
        name = type_name.split("<", 1)[0].split("[", 1)[0].strip()
        return imports.get(name, name if "." in name or not package else f"{package}.{name}")

    pending: list[tuple[Node, str, dict[str, str]]] = [(root, "", {})]
    while pending:
        node, owner, fields = pending.pop()
        if node.type in _TYPES:
            name = _text(node.child_by_field_name("name"))
            owner = f"{owner}.{name}" if owner else f"{package}.{name}".strip(".")
            body = node.child_by_field_name("body")
            fields = {}
            if body is not None:
                for member in body.named_children:
                    if member.type in {"field_declaration", "constant_declaration"}:
                        field_type = qualified(_text(member.child_by_field_name("type")))
                        for declaration in member.named_children:
                            if declaration.type == "variable_declarator":
                                fields[_text(declaration.child_by_field_name("name"))] = field_type
            # The class header and fields are a separate bounded evidence block.
            first_method = next((child for child in body.named_children if child.type in _METHODS or child.type in _TYPES), None) if body else None
            end_byte = first_method.start_byte if first_method else node.end_byte
            header = raw_source[node.start_byte:end_byte].decode()
            result.extend(_chunks(source, header, node.start_point.row + 1, language="java", kind="type", symbol=owner, aliases=(owner,), parse_error=root.has_error))
        if node.type in _METHODS:
            name = _text(node.child_by_field_name("name"))
            parameters = node.child_by_field_name("parameters")
            locals_map = dict(fields)
            parameter_types: list[str] = []
            if parameters is not None:
                for parameter in parameters.named_children:
                    raw_type = _text(parameter.child_by_field_name("type"))
                    parameter_types.append(raw_type)
                    locals_map[_text(parameter.child_by_field_name("name"))] = qualified(raw_type)
            alias = f"{owner}.{name}"
            symbol = alias + "(" + ",".join(parameter_types) + ")"
            refs: set[str] = set()
            for child in _nodes(node):
                if child.type == "local_variable_declaration":
                    type_name = qualified(_text(child.child_by_field_name("type")))
                    for declaration in child.named_children:
                        if declaration.type == "variable_declarator":
                            locals_map[_text(declaration.child_by_field_name("name"))] = type_name
                if child.type == "method_invocation":
                    called_name = _text(child.child_by_field_name("name"))
                    receiver = _text(child.child_by_field_name("object"))
                    args = child.child_by_field_name("arguments")
                    arity = len(args.named_children) if args is not None else 0
                    receiver_type = owner if receiver in {"", "this"} else locals_map.get(receiver.removeprefix("this."), imports.get(receiver))
                    refs.add(f"{receiver_type}.{called_name}#{arity}" if receiver_type else f"unresolved:{receiver[:200]}.{called_name}")
            previous = node.prev_named_sibling
            start = previous if previous is not None and previous.type == "block_comment" and _text(previous).startswith("/**") and not raw_source[previous.end_byte:node.start_byte].strip() else node
            content = raw_source[start.start_byte:node.end_byte].decode()
            result.extend(_chunks(source, content, start.start_point.row + 1, language="java", kind="method", symbol=symbol, aliases=(alias, f"{alias}#{len(parameter_types)}"), references=tuple(sorted(refs)), parse_error=root.has_error))
        pending.extend((child, owner, fields) for child in reversed(node.named_children))
    if not result:
        result.extend(_chunks(source, source.content, 1, language="java", kind="file", symbol=source.file, parse_error=root.has_error))
    return result


def _xml(source: SourceFile) -> list[CodeChunk]:
    # Parse XML events for source locations, so comment text cannot create fake
    # SQL nodes. External MyBatis DTDs are never loaded.
    if "<!ENTITY" in source.content.upper():
        raise ValueError("XML 实体声明不允许进入代码索引")
    try:
        root = ET.fromstring(source.content)
    except ET.ParseError:
        return _chunks(source, source.content, 1, language="xml", kind="file", symbol=source.file, parse_error=True)
    namespace = root.attrib.get("namespace", "")
    if root.tag != "mapper" or not namespace:
        return _chunks(source, source.content, 1, language="xml", kind="file", symbol=source.file)
    raw = source.content.encode()
    parser = expat.ParserCreate()
    depth = 0
    active: tuple[str, str, int, int] | None = None
    spans: list[tuple[str, str, int, str]] = []

    def start(tag: str, attrs: dict[str, str]) -> None:
        nonlocal depth, active
        depth += 1
        if depth == 2 and tag in {"select", "insert", "update", "delete", "sql"} and attrs.get("id"):
            active = (tag, attrs["id"], parser.CurrentByteIndex, parser.CurrentLineNumber)

    def end(tag: str) -> None:
        nonlocal depth, active
        if depth == 2 and active is not None:
            kind, name, begin, line = active
            finish = parser.CurrentByteIndex
            if raw[finish:finish + 2] == b"</":
                finish = raw.find(b">", finish) + 1
            spans.append((kind, name, line, raw[begin:finish].decode()))
            active = None
        depth -= 1

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.Parse(raw, True)
    elements = {child.attrib["id"]: child for child in root if "id" in child.attrib}
    result: list[CodeChunk] = []
    for kind, name, line, content in spans:
        element = elements.get(name)
        refs: set[str] = set()
        if element is not None:
            for include in element.iter("include"):
                target = include.attrib.get("refid", "")
                if target:
                    refs.add(target if "." in target else f"{namespace}.{target}")
        symbol = f"{namespace}.{name}"
        result.extend(_chunks(source, content, line, language="xml", kind="sql" if kind != "sql" else "sql_fragment", symbol=symbol, aliases=(symbol,), references=tuple(sorted(refs))))
    return result


def parse_cache_key(source: SourceFile) -> str:
    return stable_key(PARSER_VERSION, source.file, source.blob_sha, "xml-events-v1") if source.file.lower().endswith(".xml") else stable_key(PARSER_VERSION, source.file, source.blob_sha, "javadoc-v2")


def parse_sources(sources: Sequence[SourceFile], cached: Mapping[str, tuple[CodeChunk, ...]] | None = None) -> ParsedSources:
    if len(sources) > MAX_INDEX_FILES:
        raise ValueError("代码索引文件数超过上限")
    chunks: list[CodeChunk] = []
    seen_files: set[str] = set()
    for source in sources:
        if source.file in seen_files:
            raise ValueError("代码索引包含重复文件")
        seen_files.add(source.file)
        if len(source.content.encode()) > MAX_SOURCE_BYTES:
            raise ValueError("代码索引源文件超过大小上限")
        if git_blob_sha(source.content) != source.blob_sha:
            raise ValueError("代码索引源文件与 Blob SHA 不一致")
        suffix = PurePosixPath(source.file).suffix.casefold()
        reused = cached.get(parse_cache_key(source)) if cached is not None else None
        if reused is not None:
            if any(chunk.file != source.file or chunk.blob_sha != source.blob_sha for chunk in reused):
                raise ValueError("解析缓存的文件身份不一致")
            parsed = list(reused)
        else:
            parsed = _java(source) if suffix == ".java" else _xml(source) if suffix == ".xml" else _chunks(source, source.content, 1, language="markdown", kind="document", symbol=source.file)
        chunks.extend(parsed)
        if len(chunks) > MAX_INDEX_CHUNKS:
            raise ValueError("代码索引分块数超过上限")
    aliases: dict[str, list[CodeChunk]] = defaultdict(list)
    for chunk in chunks:
        for alias in chunk.aliases:
            aliases[alias].append(chunk)
    relations: dict[tuple[str, str, str], CodeRelation] = {}
    unresolved = 0
    for chunk in chunks:
        for reference in chunk.references:
            matches = aliases.get(reference, [])
            symbols = {item.symbol for item in matches}
            if not matches or len(symbols) > 1:
                unresolved += 1
                continue
            for target in matches:
                if target.id != chunk.id:
                    relations[(chunk.id, target.id, "reference")] = CodeRelation(source_id=chunk.id, target_id=target.id, kind="reference")
        if chunk.kind == "sql":
            for target in aliases.get(chunk.symbol, []):
                if target.kind == "method":
                    for source_id, target_id in ((chunk.id, target.id), (target.id, chunk.id)):
                        relations[(source_id, target_id, "mapper_sql")] = CodeRelation(source_id=source_id, target_id=target_id, kind="mapper_sql")
    return ParsedSources(
        chunks=tuple(chunks), relations=tuple(relations.values()),
        unresolved_references=unresolved,
        parse_error_files=tuple(sorted({chunk.file for chunk in chunks if chunk.parse_error})),
    )
