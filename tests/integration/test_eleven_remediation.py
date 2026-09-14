"""十一项整改的离线回归；外部模型使用明确的测试替身。"""

from sqlalchemy import event

from domain.evaluation_workbench import EvaluationDatasetCreate
from domain.repository_policy import RepositoryPolicy
from domain.retrieval import RetrievalEvaluationCase, SearchQuery
from services.evaluation_workbench import EvaluationWorkbench
from services.team import RepositoryWrite, TeamService
from tests.evaluation_support import seed_evaluation_runs
from tests.integration.test_evaluation_workbench import ALL, create_pair
from tests.integration.test_hybrid_retrieval import TARGET, sources
from tests.integration.test_hybrid_retrieval import retrieval as retrieval
from tests.integration.test_management_api import database as database
from tests.support import TEST_USERNAME


def test_manual_relation_search_finds_sql_without_answer_seeds_and_saves_detail(retrieval):
    service, _, engine = retrieval
    index = service.index_sources(TARGET, sources(), include_vectors=False)
    query = "load"
    plain = service.search(index.id, SearchQuery(query=query, strategy="bm25"))
    related = service.search(index.id, SearchQuery(query=query, strategy="lexical_relations"))
    assert "sample.UserMapper.getById" not in {item.symbol for item in plain.selected}
    assert "sample.UserMapper.getById" in {item.symbol for item in related.selected}
    assert next(route.candidate_count for route in related.routes if route.route == "relation") > 0
    statements = []
    event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    report = service.evaluate(index.id, (RetrievalEvaluationCase(id="sql", query=query,
        relevant_symbols=("sample.UserMapper.getById",)),), dataset_version="offline-contract",
        annotation_source="synthetic_contract", strategies=("bm25", "lexical_relations"))
    assert report.strategies[0].recall_at_k == 0
    assert report.strategies[1].recall_at_k == 1
    detail_id = report.strategies[1].cases[0]["trace_id"]
    assert isinstance(detail_id, str)
    detail = service.repository.search_record(detail_id, None)
    assert any("SELECT id FROM users" in item.content for item in detail.selected)
    assert all("candidates" not in case for strategy in report.strategies for case in strategy.cases)
    # 评测不会混入手动搜索历史；每份详情按主键读取。
    assert len(service.repository.search_history(index.id, None).items) == 2
    assert service.repository.evaluation_page(None, index_id="another-index").items == ()
    assert service.repository.evaluation_page(None, index_id=index.id).items[0].id == report.id
    assert sum("INSERT INTO retrieval_traces" in sql for sql in statements) == 1


def test_empty_relation_route_is_reported_honestly(retrieval):
    service, _, _ = retrieval
    index = service.index_sources(TARGET, sources(), include_vectors=False)
    trace = service.search(index.id, SearchQuery(query="no-matching-code", strategy="lexical_relations"))
    assert trace.requested_strategy == "lexical_relations"
    assert trace.strategy == "bm25"
    assert next(route.candidate_count for route in trace.routes if route.route == "relation") == 0
    assert any("未使用关系候选" in warning for warning in trace.warnings)


def test_single_review_statistics_do_not_include_other_pr_or_comparison(database):
    service, dataset, case_id, _ = create_pair(database)
    from tests.integration.test_evaluation_workbench import submit_ballot
    submit_ballot(service, case_id, "baseline", TEST_USERNAME, [("valid", None), ("false_positive", None)])
    baseline = service.overview(dataset.id, ALL, case_id, "baseline")
    candidate = service.overview(dataset.id, ALL, case_id, "candidate")
    assert baseline.observation_count == baseline.reviewed_observations == 1
    assert baseline.valid_findings == baseline.false_positive_findings == 1
    assert baseline.unreviewed_findings == 0
    assert candidate.valid_findings == candidate.reviewed_observations == 0
    assert candidate.unreviewed_findings == 1
    runs = seed_evaluation_runs(database, [{"label":"other", "pr":999, "findings":["其他问题"]}])
    other = EvaluationWorkbench(database.sessions).create_dataset(EvaluationDatasetCreate(
        name="另一个评测", review_run_ids=(runs["other"],)), "other", TEST_USERNAME, ALL)
    assert service.overview(other.id, ALL, case_id, "baseline").observation_count == 0


def test_repository_budget_lookup_uses_exact_name_not_first_list_page(database):
    team = TeamService(database.sessions, TEST_USERNAME)
    team.save_repository(RepositoryWrite(repository="owner/target", expected_revision=0,
        policy=RepositoryPolicy()), TEST_USERNAME)
    result = team.repositories(limit=1, repository="OWNER/TARGET")
    assert len(result.items) == 1 and result.items[0].repository == "owner/target"
    assert not team.repositories(limit=1, repository="owner/tar").items


def test_parser_upgrade_keeps_old_reports_and_reuses_unchanged_vectors(retrieval, monkeypatch):
    import persistence.retrieval as storage
    import services.code_indexing as parser
    from domain.retrieval import RetrievalSettings, stable_key
    from tests.integration.test_hybrid_retrieval import FakeModels

    service, _, _ = retrieval
    with monkeypatch.context() as legacy:
        legacy.setattr(parser, "PARSER_VERSION", "java-mybatis-v1")
        legacy.setattr(storage, "PARSER_VERSION", "java-mybatis-v1")
        legacy.setattr(RetrievalSettings, "index_key", lambda self, installation, repository, sha:
            stable_key(installation, repository, sha, self.embedding_fingerprint))
        old = service.index_sources(TARGET, sources())
        old_trace = service.search(old.id, SearchQuery(query="load", strategy="lexical_relations"))
    previous_requests = FakeModels.embeddings
    upgraded = service.index_sources(TARGET, sources())
    assert upgraded.id != old.id
    assert upgraded.parser_version == "java-mybatis-v2"
    assert upgraded.vector_count == upgraded.chunk_count == upgraded.reused_count
    assert FakeModels.embeddings == previous_requests
    assert service.repository.get(old.id).parser_version == "java-mybatis-v1"
    assert service.repository.search_record(old_trace.id, None) == old_trace


def test_partial_review_keeps_legacy_index_after_parser_upgrade(retrieval, monkeypatch):
    from sqlalchemy import delete

    import persistence.retrieval as storage
    import services.code_indexing as parser
    from domain.enums import ReviewAgent
    from domain.models import ReviewRequest
    from domain.retrieval import RetrievalSettings, stable_key
    from persistence.models import RetrievalTraceRecord
    from persistence.repositories import SqlAlchemyReviewRepository
    from services.reviews import ReviewService
    from tests.unit.test_model_review import make_model_input

    service, sessions, _ = retrieval
    source = make_model_input()
    target = {"installation_id": 10, "repository_id": source.repository_id,
        "repository": source.repository, "head_sha": source.head_sha}
    run = ReviewService(SqlAlchemyReviewRepository(sessions)).submit(
        ReviewRequest(**target, pull_request_number=source.pull_request_number), "legacy-partial")
    source = source.model_copy(update={"review_run_id":run.review_run_id, "units":(
        source.units[0].model_copy(update={"review_domains":(ReviewAgent.SECURITY,ReviewAgent.CONVENTION,ReviewAgent.LOGIC)}),)})
    with monkeypatch.context() as legacy:
        legacy.setattr(parser, "PARSER_VERSION", "java-mybatis-v1")
        legacy.setattr(storage, "PARSER_VERSION", "java-mybatis-v1")
        legacy.setattr(RetrievalSettings, "index_key", lambda self, installation, repository, sha:
            stable_key(installation, repository, sha, self.embedding_fingerprint))
        old = service.index_sources(target, sources())
        first = service.review_context(source, lambda: None)
    with sessions() as session, session.begin():
        session.execute(delete(RetrievalTraceRecord).where(RetrievalTraceRecord.review_run_id == run.review_run_id,
            RetrievalTraceRecord.agent == "logic"))
    resumed = service.review_context(source, lambda: None)
    assert {item.index_id for item in resumed.context_evidence} == {old.id}
    assert tuple(item for item in resumed.context_evidence if item.agent == ReviewAgent.SECURITY) == tuple(
        item for item in first.context_evidence if item.agent == ReviewAgent.SECURITY)
