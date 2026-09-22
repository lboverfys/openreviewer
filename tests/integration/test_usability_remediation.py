"""本轮八项整改的本地回归；模型和 GitHub 均为明确的离线测试替身。"""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from domain.evaluation_workbench import (
    EvaluationDatasetCreate,
    EvaluationDecision,
    FindingReviewWrite,
)
from domain.model_review import materialize_findings
from domain.repository_policy import RepositoryPolicy
from domain.retrieval import RetrievalSettings, SearchQuery
from domain.security import SafeApplicationError
from persistence.auth import SqlAlchemySessionStore
from persistence.models import (
    CodeIndexRecord,
    FindingLifecycleRecord,
    KnowledgeDocumentVersionRecord,
)
from persistence.retrieval import RetrievalRepository
from persistence.review_management import SqlAlchemyReviewManagementRepository
from persistence.team import SqlAlchemyMemberStore
from services.ai_settings import AiSecretCipher
from services.auth import AuthService
from services.evaluation_workbench import EvaluationWorkbench
from services.github_access import with_repository_grants
from services.rag import KnowledgeNotFoundError, ManagedMarkdownKnowledgeBase
from services.retrieval import HybridRetrievalService, RetrievalSettingsService
from services.review_management import ReviewAction, ReviewManagementService
from services.review_planning import DeterministicReviewPlanner
from services.team import MemberScope, MemberWrite, RepositoryWrite, TeamService
from tests.evaluation_support import seed_evaluation_runs
from tests.integration.test_evaluation_workbench import ALL
from tests.integration.test_hybrid_retrieval import TARGET, FakeModels, sources
from tests.integration.test_hybrid_retrieval import retrieval as retrieval
from tests.integration.test_review_plan_persistence import (
    MutableClock,
    StaticModelReviewer,
    _prepare_planning_lease,
    _rules,
    _submit,
)
from tests.integration.test_review_plan_persistence import database as database
from tests.support import (
    TEST_GITHUB_ACCESS_POLICY,
    TEST_HASHER,
    TEST_PASSWORD,
    TEST_USERNAME,
    make_auth_service,
)
from tests.unit.test_knowledge_archive import archive_library as archive_library


def test_delete_all_documents_does_not_reseed_and_removes_profile_sources(archive_library):
    library, _ = archive_library
    frozen = library.chunks()
    before = library.list_documents()
    first, second = before.items
    revision = library.delete_document(first.id, expected_revision=before.revision,
        expected_document_version=1, actor="owner")
    library.delete_document(second.id, expected_revision=revision,
        expected_document_version=1, actor="owner")
    assert library.chunks() == () and library.filter_active_chunks(frozen) == ()
    restarted = ManagedMarkdownKnowledgeBase(library._sessions, library.root)
    assert restarted.list_documents(include_archived=True).total == 0
    with pytest.raises(KnowledgeNotFoundError):
        restarted.get_document(first.id)
    with library._sessions() as session:
        assert session.scalar(select(func.count()).select_from(KnowledgeDocumentVersionRecord)) == 0
    assert frozen[0].content  # 旧快照的正文保持完整。


def test_manual_search_history_is_paged_version_scoped_and_keeps_code(retrieval):
    service, _, _ = retrieval
    index = service.index_sources(TARGET, sources(), include_vectors=False)
    first = service.search(index.id, SearchQuery(query="user", strategy="bm25"))
    second = service.search(index.id, SearchQuery(query="user", strategy="lexical_relations"))
    page = service.repository.search_history(index.id, None, limit=1)
    assert page.items[0].id == second.id and page.next_cursor
    assert service.repository.search_history(index.id, None, limit=1, cursor=page.next_cursor).items[0].id == first.id
    assert service.repository.search_history(index.id, None, strategy="bm25").items[0].head_sha == TARGET["head_sha"]
    assert service.repository.search_record(first.id, None).candidates == first.candidates
    assert service.repository.search_history(index.id, ALL, query="no match").items == ()
    assert service.repository.evaluations(None) == ()


def test_single_account_finishes_one_result_without_guessing_unknowns(database):
    runs = seed_evaluation_runs(database, [{"label": "one", "pr": 301, "findings": ["有效", "不确定", "未核对"], "cost": None}])
    service = EvaluationWorkbench(database.sessions)
    dataset = service.create_dataset(EvaluationDatasetCreate(name="单人核对", review_run_ids=(runs["one"],)), "single", "alice", ALL)
    case = service.cases(dataset.id, ALL).items[0]
    findings = service.findings(case.id, "baseline", ALL).items
    observation = service.observation(case.id, "baseline", ALL)
    observation = service.review_finding(case.id, "baseline", findings[0].finding.id,
        FindingReviewWrite(expected_revision=observation.observation.revision, decision=EvaluationDecision(verdict="valid")), "alice", ALL)
    observation = service.review_finding(case.id, "baseline", findings[1].finding.id,
        FindingReviewWrite(expected_revision=observation.observation.revision, decision=EvaluationDecision(verdict="uncertain")), "alice", ALL)
    completed = service.submit_review(case.id, "baseline", observation.observation.revision, "alice", ALL)
    assert completed.observation.assessment_status == "complete" and len(completed.ballots) == 1
    overview = service.overview(dataset.id, ALL)
    assert (overview.valid_findings, overview.uncertain_findings, overview.unreviewed_findings) == (1, 1, 1)
    assert overview.estimated_cost_microusd is None and overview.unpriced_observations == 1
    assert service.report(dataset.id, ALL).baseline.recall is None


def test_initial_admin_is_managed_and_password_survives_bootstrap(database):
    settings = make_auth_service().settings
    store = SqlAlchemyMemberStore(database.sessions)
    store.bootstrap(settings)
    team = TeamService(database.sessions, settings.username, password_hasher=TEST_HASHER)
    member = team.members().items[0]
    assert member.username == settings.username and member.role == "administrator"
    updated = team.save_member(settings.username, MemberWrite(expected_revision=1,
        role="administrator", scope=MemberScope(unrestricted=True), password="new-local-password"), settings.username)
    store.bootstrap(settings)
    auth = AuthService(settings, member_store=store, password_hasher=TEST_HASHER,
        session_store=SqlAlchemySessionStore(database.sessions))
    assert updated.revision == 2
    assert auth.verify_credentials(TEST_USERNAME, "new-local-password")
    assert not auth.verify_credentials(TEST_USERNAME, TEST_PASSWORD)
    with pytest.raises(ValueError, match="初始管理员"):
        team.save_member(settings.username, MemberWrite(expected_revision=2, role="viewer", enabled=False), settings.username)


def test_repository_policy_alone_does_not_grant_access_and_verified_connection_does(database):
    connector = Mock()
    connector.check.return_value = 789
    team = TeamService(database.sessions, TEST_USERNAME, connector=lambda: connector)
    policy = with_repository_grants(TEST_GITHUB_ACCESS_POLICY, database.sessions)
    draft = RepositoryWrite(repository="owner/new-repo", expected_revision=0, policy=RepositoryPolicy())
    saved = team.save_repository(draft, TEST_USERNAME)
    assert saved.connected_at is None and policy.denial_reason(10, draft.repository) is not None
    connected = team.save_repository(draft.model_copy(update={"expected_revision": 1, "installation_id": 10}), TEST_USERNAME, saved.id)
    assert connected.connected_at and connected.connection_repository_id == 789
    assert policy.denial_reason(10, draft.repository) is None
    assert policy.denial_reason(11, draft.repository) is not None
    assert policy.denial_reason(10, "owner/not-selected") is not None
    connector.check.assert_called_once_with(10, draft.repository)
    connector.check.side_effect = ValueError("仓库授权已撤销，请补充授权")
    disconnected = team.check_connection(saved.id, TEST_USERNAME)
    assert disconnected.connected_at is None and "已撤销" in disconnected.connection_error
    assert policy.denial_reason(10, draft.repository) is not None


def test_auto_vector_preparation_waits_then_reuses_frozen_evidence(database, monkeypatch):
    monkeypatch.setenv("OPENREVIEWER_RETRIEVAL_API_DISABLED", "false")
    clock = MutableClock(datetime.now(UTC))
    queue, lease, _, run_id = _prepare_planning_lease(database, clock, complete_context=True)
    planning = queue.load_planning_input(lease)
    rules = _rules(planning.target)
    queue.store_review_plan(lease, rules, DeterministicReviewPlanner().plan(planning.target, planning.files, rules))
    model_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    model_input = queue.load_model_review_input(model_lease)
    settings = RetrievalSettingsService(database.sessions, AiSecretCipher(b"a" * 32))
    settings.update(RetrievalSettings(enabled=True, api_host="https://sample.cn-beijing.maas.aliyuncs.com"), 0, "tester", "offline-key")
    service = HybridRetrievalService(RetrievalRepository(database.sessions), settings,
        client_factory=FakeModels, source_loader=lambda target, heartbeat: sources())
    with pytest.raises(SafeApplicationError):
        service.review_context(model_input, lambda: None)
    with database.sessions() as session:
        index = session.scalar(select(CodeIndexRecord).limit(1))
        assert index.source_target["include_vectors"] is True
    assert service.process_next()
    prepared = service.review_context(model_input, lambda: None)
    assert service.repository.get(index.id).vector_count == service.repository.get(index.id).chunk_count
    count = FakeModels.embeddings
    settings.update(settings.get().settings.model_copy(update={"external_calls_enabled": False}), 1, "tester")
    assert service.review_context(model_input, lambda: None).context_evidence == prepared.context_evidence
    assert FakeModels.embeddings == count


def test_model_result_is_preserved_as_history_if_head_changes_after_calls(database):
    clock = MutableClock(datetime.now(UTC))
    queue, lease, _, old_id = _prepare_planning_lease(database, clock, complete_context=True)
    planning = queue.load_planning_input(lease)
    rules = _rules(planning.target)
    queue.store_review_plan(lease, rules, DeterministicReviewPlanner().plan(planning.target, planning.files, rules))
    model_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    model_input = queue.load_model_review_input(model_lease)
    result = StaticModelReviewer().review(model_input)
    clock.value += timedelta(seconds=1)
    _submit(database, clock, "new-head", "b" * 40)
    queue.store_model_review(model_lease, model_input, result, materialize_findings(model_input, result.output))
    detail = ReviewManagementService(SqlAlchemyReviewManagementRepository(database.sessions)).details(old_id)
    assert detail.stored.snapshot_review and detail.stored.findings and detail.phase == "completed"
    assert ReviewAction.PUBLISH not in detail.available_actions
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(FindingLifecycleRecord)) == 0


def test_failed_node_retry_pins_old_sha_before_reusing_completed_batches(database):
    from sqlalchemy import update

    from domain.enums import ModelProvider
    from persistence.models import ReviewRunRecord, ReviewTaskRecord
    from services.model_review import ModelServiceSettings, plan_model_review_batches

    clock = MutableClock(datetime.now(UTC))
    queue, lease, task_id, old_id = _prepare_planning_lease(database, clock, complete_context=True)
    planning = queue.load_planning_input(lease)
    rules = _rules(planning.target)
    queue.store_review_plan(lease, rules, DeterministicReviewPlanner().plan(planning.target, planning.files, rules))
    model_lease = queue.claim_next("worker-1", timedelta(seconds=30))
    model_input = queue.load_model_review_input(model_lease)
    batches = plan_model_review_batches(model_input, ModelServiceSettings(provider=ModelProvider.OPENAI, model="offline-fixture", api_key="test-key"))
    result = StaticModelReviewer().review(model_input)
    # 只构造本地已成功检查点，不运行模型或发送网络请求。
    for agent in ("security", "convention", "logic"):
        queue.ensure_model_batches(model_lease, batches, agent=agent)
        queue.claim_model_batch(model_lease, 1, agent=agent, lease_duration=timedelta(seconds=30))
        queue.complete_model_batch(model_lease, 1, result, agent=agent)
    with database.sessions() as session, session.begin():
        session.execute(update(ReviewTaskRecord).where(ReviewTaskRecord.id == task_id).values(execution_status="failed", workflow_status="failed"))
        session.execute(update(ReviewRunRecord).where(ReviewRunRecord.id == old_id).values(execution_status="failed", workflow_status="failed", coverage_status="partial"))
    clock.value += timedelta(seconds=1)
    _submit(database, clock, "later-sha", "b" * 40)
    service = ReviewManagementService(SqlAlchemyReviewManagementRepository(database.sessions, clock=clock))
    service.apply_action(old_id, ReviewAction.RETRY_FAILED_NODE, actor="tester", request_id="old-retry")
    detail = service.details(old_id)
    assert detail.stored.snapshot_review and detail.stored.head_sha == "a" * 40
    page = service.batch_page(old_id, "security")
    assert page.items[0].status == "succeeded" and page.items[0].candidates
    assert page.items[0].candidates[0].title == result.output.findings[0].title


def test_vector_budget_builds_only_the_allowed_missing_part(retrieval):
    service, _, _ = retrieval
    view = service.settings.get()
    service.settings.update(view.settings.model_copy(update={"max_new_vectors_per_index": 2}), view.revision, "tester")
    FakeModels.embeddings = 0
    index = service.index_sources(TARGET, sources())
    assert index.status == "queued" and index.vector_status == "pending" and index.vector_count == 2
    assert FakeModels.embeddings == 2 and index.lexical_ready
    assert "2/5" in index.vector_error
