"""固定 Java/MyBatis 回归使用生产评分，明确不代表真实缺陷质量。"""

import pytest

from apps.maintenance.retrieval_cli import run_regression
from persistence.retrieval import RetrievalRepository
from tests.integration.test_management_api import database as database
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)


def assert_contract(database, monkeypatch):
    monkeypatch.setenv("OPENREVIEWER_RETRIEVAL_API_DISABLED", "true")
    report, _ = run_regression(database)
    assert report.total_model_requests == report.shared_prewarm_requests == 0
    assert report.real_review_accuracy is None and report.annotation_source == "synthetic_contract"
    assert report.vector_count == 0
    assert {item.strategy for item in report.strategies} == {"bm25", "lexical_relations"}
    monkeypatch.setattr(RetrievalRepository, "relation_search", lambda *args: [])
    with pytest.raises(ValueError, match="lexical_relations"):
        run_regression(database)


def test_regression_checker_rejects_a_disconnected_relation_route(database, monkeypatch):
    assert_contract(database, monkeypatch)


def test_postgres_java_mybatis_regression_and_negative_control(postgres_database, monkeypatch):
    assert_contract(postgres_database, monkeypatch)
