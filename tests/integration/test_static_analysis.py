"""静态报告来源、幂等、权限、分页与查询数量在隔离数据库中验证。"""

import json

import pytest
from sqlalchemy import event

from domain.platform import PlatformConflictError, PlatformNotFoundError
from domain.static_analysis import StaticReportUpload
from persistence.static_analysis import StaticAnalysisRepository
from services.rbac import ResourceScope
from services.static_analysis import parse_upload
from tests.integration.test_finding_pagination import _seed_review
from tests.integration.test_management_api import database as database

ALL = ResourceScope.unrestricted_scope()


def sarif(count=1, *, version="1.0", path="src/auth.py", fingerprint=True):
    return json.dumps({"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "Semgrep", "version": version}},
        "results": [{"ruleId": "auth-check", "message": {"text": "检查权限边界"},
            "partialFingerprints": {"matchBasedId/v1": f"id-{number}"} if fingerprint else {},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": path},
                "region": {"startLine": number + 1}}}]} for number in range(count)]}]})


def test_baseline_matching_requires_stable_id_and_same_scanner():
    draft = StaticReportUpload(head_sha="a" * 40, head_sarif=sarif(3), base_sha="b" * 40, base_sarif=sarif(1))
    _, findings, _ = parse_upload(draft)
    assert [item["baseline_state"] for item in findings] == ["existing", "new", "new"]
    _, missing, _ = parse_upload(draft.model_copy(update={"head_sarif": sarif(fingerprint=False)}))
    assert missing[0]["baseline_state"] == "unknown"
    with pytest.raises(ValueError):
        parse_upload(draft.model_copy(update={"base_sarif": sarif(version="2.0")}))


@pytest.mark.parametrize("path", ["../private", "%2e%2e/private", "file:///etc/passwd", "C:/secret", "https://example.org/code"])
def test_untrusted_sarif_paths_are_rejected(path):
    with pytest.raises(ValueError):
        parse_upload(StaticReportUpload(head_sha="a" * 40, head_sarif=sarif(path=path)))


def test_import_is_idempotent_scoped_and_paginated_with_fixed_query_count(database):
    run_id = _seed_review(database, finding_count=2)
    repository = StaticAnalysisRepository(database.sessions)
    upload = StaticReportUpload(head_sha="a" * 40, head_sarif=sarif(25))
    report = repository.upload(run_id, upload, "reviewer", ALL)
    assert repository.upload(run_id, upload, "reviewer", ALL).id == report.id
    assert report.unknown_count == 25
    with pytest.raises(PlatformConflictError):
        repository.upload(run_id, upload.model_copy(update={"head_sarif": sarif(1)}), "reviewer", ALL)
    with pytest.raises(PlatformNotFoundError):
        repository.get(run_id, ResourceScope())
    statements = []
    def count(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(database.engine, "before_cursor_execute", count)
    try:
        first = repository.findings(run_id, ALL)
        second = repository.findings(run_id, ALL, cursor=first.next_cursor)
    finally:
        event.remove(database.engine, "before_cursor_execute", count)
    assert len(statements) == 6
    assert len(first.items) == len(second.items) == 10
    assert not {row.id for row in first.items} & {row.id for row in second.items}


def test_sha_and_scan_limits_fail_before_persisting(database):
    run_id = _seed_review(database, finding_count=1)
    repository = StaticAnalysisRepository(database.sessions)
    with pytest.raises(ValueError):
        repository.upload(run_id, StaticReportUpload(head_sha="b" * 40, head_sarif=sarif()), "reviewer", ALL)
    with pytest.raises(ValueError):
        parse_upload(StaticReportUpload(head_sha="a" * 40, head_sarif=sarif(501)))
    assert repository.get(run_id, ALL) is None
