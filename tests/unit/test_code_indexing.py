from domain.retrieval import MAX_CHUNK_CHARS, SourceFile
from services.code_indexing import code_tokens, git_blob_sha, parse_sources


def source(path: str, text: str) -> SourceFile:
    return SourceFile(file=path, content=text, blob_sha=git_blob_sha(text))


def test_java_methods_and_mapper_sql_have_exact_relations():
    mapper = source("UserMapper.java", "package sample; public interface UserMapper { User getById(long id); }")
    service = source("UserService.java", "package sample; class UserService { UserMapper mapper; User load(long id) { return mapper.getById(id); } }")
    sql = source("UserMapper.xml", '<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" "http://mybatis.org/dtd/mybatis-3-mapper.dtd">\n<mapper namespace="sample.UserMapper">\n<select id="getById">SELECT id FROM users WHERE id = #{id}</select>\n</mapper>')
    parsed = parse_sources([mapper, service, sql])
    chunks = {chunk.id: chunk for chunk in parsed.chunks}
    assert not parsed.parse_error_files
    edges = {(chunks[item.source_id].symbol, chunks[item.target_id].symbol, item.kind) for item in parsed.relations}
    assert ("sample.UserService.load(long)", "sample.UserMapper.getById(long)", "reference") in edges
    assert ("sample.UserMapper.getById(long)", "sample.UserMapper.getById", "mapper_sql") in edges
    sql_chunk = next(chunk for chunk in parsed.chunks if chunk.kind == "sql")
    assert sql_chunk.start_line == 3
    assert sql_chunk.end_line == 3


def test_chunk_identity_and_embedding_reuse():
    original = "package sample; class A { void work() {} }"
    a = parse_sources([source("A.java", original)])
    b = parse_sources([source("A.java", "\n" + original)])
    method_a = next(c for c in a.chunks if c.kind == "method")
    method_b = next(c for c in b.chunks if c.kind == "method")
    assert method_a.id != method_b.id
    assert method_a.embedding_hash == method_b.embedding_hash


def test_long_methods_are_bounded_and_unicode_positions_are_valid():
    text = "package sample; class A { void work() {\n" + 'String value = "中文";\n' * 1000 + "} }"
    parsed = parse_sources([source("A.java", text)])
    assert len(parsed.chunks) > 2
    assert all(len(c.content) <= MAX_CHUNK_CHARS for c in parsed.chunks)
    assert all(c.start_line <= c.end_line for c in parsed.chunks)


def test_blob_mismatch_and_xml_entities_are_rejected():
    import pytest
    with pytest.raises(ValueError, match="Blob SHA"):
        parse_sources([SourceFile(file="A.java", blob_sha="a" * 40, content="class A {}")])
    text = '<!DOCTYPE mapper [<!ENTITY x "unsafe">]><mapper namespace="x">&x;</mapper>'
    with pytest.raises(ValueError, match="实体"):
        parse_sources([source("Mapper.xml", text)])


def test_code_tokens_keep_identifiers_and_split_camel_case():
    tokens = code_tokens("getUserById user_id 鉴权检查")
    assert {"getuserbyid", "get", "user", "by", "id", "user_id", "鉴权", "权检", "检查"} <= tokens.keys()


def test_xml_comments_do_not_create_sql_evidence():
    text = '<mapper namespace="sample.M"><!-- <select id="fake">SELECT secrets</select> -->\n<select id="real">SELECT id FROM users</select></mapper>'
    result = parse_sources([source("M.xml", text)])
    assert [chunk.symbol for chunk in result.chunks] == ["sample.M.real"]
    assert result.chunks[0].start_line == 2
