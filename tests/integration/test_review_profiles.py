"""审查配置冻结、版本恢复与人工经验的仓库隔离。"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from domain.enums import ModelProvider, ReviewAgent
from domain.platform import (
    KnowledgeProposalWrite,
    PlatformConflictError,
    ProfileCreate,
    WorkItemCreate,
    WorkItemUpdate,
)
from domain.repository_policy import RepositoryPolicy
from domain.retrieval import RetrievalSettings, RetrievalSettingsView
from persistence.models import ReviewFindingRecord, ReviewProfileRecord, ReviewRunRecord
from persistence.review_profiles import ReviewProfileRepository
from persistence.work_items import WorkItemRepository
from services.ai_secret_rotation import AiSecretRotationService
from services.ai_settings import AiSecretCipher
from services.model_review import ModelPricing, ModelServiceSettings
from services.rag import ManagedMarkdownKnowledgeBase
from services.rbac import ResourceScope
from services.review_learning import ReviewLearningService
from services.review_profiles import ReviewProfileRuntimeLoader, ReviewProfileService
from services.team import RepositoryWrite, TeamService
from tests.integration.test_finding_pagination import _seed_review
from tests.integration.test_github_context_persistence import _submit
from tests.integration.test_management_api import database as database
from tests.support import TEST_USERNAME

ALL = ResourceScope.unrestricted_scope()


def profile_services(database, tmp_path):
    team = TeamService(database.sessions, TEST_USERNAME)
    policy = team.save_repository(
        RepositoryWrite(
            repository="lboverfys/NiuMa", expected_revision=0, policy=RepositoryPolicy()
        ),
        TEST_USERNAME,
    )
    cipher = AiSecretCipher(b"o" * 32)
    knowledge = ManagedMarkdownKnowledgeBase(database.sessions, tmp_path / "knowledge")
    knowledge.list_documents()
    models = {
        agent: ModelServiceSettings(
            ModelProvider.OPENAI,
            "fixture-original",
            "fixture-private-key",
            pricing=ModelPricing(
                Decimal("1.2"),
                Decimal("3.4"),
                cache_read_usd_per_million=Decimal("0.1"),
            ),
        )
        for agent in ReviewAgent
    }
    fake_ai = SimpleNamespace(
        get=lambda: SimpleNamespace(
            max_units=100,
            max_scope_depth=32,
            max_unit_input_bytes=196608,
            max_total_input_bytes=2097152,
        )
    )
    fake_agents = SimpleNamespace(model_settings=lambda: dict(models))
    fake_retrieval = SimpleNamespace(
        runtime=lambda: (
            RetrievalSettingsView(
                revision=0, settings=RetrievalSettings(), key_configured=False
            ),
            None,
        )
    )
    repository = ReviewProfileRepository(database.sessions)
    service = ReviewProfileService(
        repository, cipher, fake_ai, fake_agents, knowledge, fake_retrieval
    )
    return team, policy, repository, service, knowledge, models


def draft(name="初始方案"):
    return ProfileCreate(
        name=name, repository="lboverfys/NiuMa", expected_ai_revision=0
    )


def test_profile_freezes_configuration_and_restores_without_changing_old_runs(
    database, tmp_path, monkeypatch
):
    _, policy, repository, service, _, models = profile_services(database, tmp_path)
    first = service.create(draft(), TEST_USERNAME, ALL)
    assert "fixture-private-key" not in first.model_dump_json()
    revision = repository.activate(first.id, policy.revision, TEST_USERNAME, ALL)
    _, run_id = _submit(database, "profile-bound", "a" * 40)
    models[ReviewAgent.LOGIC] = ModelServiceSettings(
        ModelProvider.OPENAI, "fixture-candidate", "fixture-new-key"
    )
    second = service.create(draft("候选方案"), TEST_USERNAME, ALL)
    assert second.fingerprint != first.fingerprint
    revision = repository.activate(second.id, revision, TEST_USERNAME, ALL)
    with pytest.raises(PlatformConflictError):
        repository.activate(first.id, revision - 1, TEST_USERNAME, ALL)
    repository.activate(first.id, revision, TEST_USERNAME, ALL)
    with database.sessions() as session:
        assert (
            session.get(ReviewRunRecord, run_id).repository_policy["review_profile_id"]
            == first.id
        )
        stored = session.get(ReviewProfileRecord, first.id)
        assert b"fixture-private-key" not in stored.ciphertext
    monkeypatch.setattr(
        "services.review_profiles.create_model_reviewer",
        lambda settings: SimpleNamespace(close=lambda: None),
    )
    runtime = ReviewProfileRuntimeLoader(repository, service.cipher)(first.id)
    assert runtime.profile_id == first.id
    price = runtime.agent_workflow.agent_settings[ReviewAgent.LOGIC].pricing
    assert price is not None and price.input_usd_per_million == Decimal("1.2")
    assert price.cache_read_usd_per_million == Decimal("0.1")
    assert (
        runtime.agent_workflow.agent_settings[ReviewAgent.LOGIC].model
        == "fixture-original"
    )
    assert (
        runtime.agent_workflow.agent_settings[ReviewAgent.LOGIC].api_key
        == "fixture-private-key"
    )
    runtime.agent_workflow.close()
    assert not repository.list(ResourceScope.deny_all()).items


def test_configuration_change_during_capture_is_rejected(database, tmp_path):
    team, policy, _, service, _, models = profile_services(database, tmp_path)

    def change_policy():
        team.save_repository(
            RepositoryWrite(
                repository=policy.repository,
                expected_revision=policy.revision,
                policy=RepositoryPolicy(approval_timeout_hours=48),
            ),
            TEST_USERNAME,
            policy.id,
        )
        return dict(models)

    service.agents = SimpleNamespace(model_settings=change_policy)
    with pytest.raises(PlatformConflictError):
        service.create(draft(), TEST_USERNAME, ALL)
    with database.sessions() as session:
        assert session.scalar(select(ReviewProfileRecord.id)) is None


def test_profile_secret_rotation_preserves_snapshot_and_runtime(
    database, tmp_path, monkeypatch
):
    _, _, repository, service, _, _ = profile_services(database, tmp_path)
    profile = service.create(draft(), TEST_USERNAME, ALL)
    cipher = AiSecretCipher(b"n" * 32, key_version=2, previous_keys=((1, b"o" * 32),))
    result = AiSecretRotationService(database.sessions, cipher).rotate_batch()
    assert result.profile_secrets == 1 and result.complete
    monkeypatch.setattr(
        "services.review_profiles.create_model_reviewer",
        lambda settings: SimpleNamespace(close=lambda: None),
    )
    runtime = ReviewProfileRuntimeLoader(
        repository, AiSecretCipher(b"n" * 32, key_version=2)
    )(profile.id)
    assert (
        runtime.agent_workflow.agent_settings[ReviewAgent.SECURITY].api_key
        == "fixture-private-key"
    )
    runtime.agent_workflow.close()
    assert repository.list(ALL).items[0].fingerprint == profile.fingerprint


def test_human_learning_is_disabled_by_default_and_remains_repository_scoped(
    database, tmp_path
):
    team, _, _, service, knowledge, _ = profile_services(database, tmp_path)
    run_id = _seed_review(database, finding_count=1)
    with database.sessions() as session:
        finding_id = session.scalar(
            select(ReviewFindingRecord.id).where(
                ReviewFindingRecord.review_run_id == run_id
            )
        )
    work = WorkItemRepository(database.sessions, TEST_USERNAME)
    item = work.create(WorkItemCreate(finding_id=finding_id), TEST_USERNAME, ALL)
    learning = ReviewLearningService(work, knowledge)
    proposal = KnowledgeProposalWrite(
        expected_work_revision=1,
        expected_library_revision=knowledge.list_documents().revision,
        lesson="订单操作应校验资源归属，并补充越权回归用例。",
    )
    with pytest.raises(ValueError):
        learning.propose(item.id, proposal, TEST_USERNAME, ALL)
    item = work.update(
        item.id,
        WorkItemUpdate(
            expected_revision=1,
            status="resolved",
            note="已检查资源归属",
            fix_pull_request_number=90,
        ),
        TEST_USERNAME,
        ALL,
    )
    with pytest.raises(PlatformConflictError):
        learning.propose(item.id, proposal, TEST_USERNAME, ALL)
    proposal = proposal.model_copy(update={"expected_work_revision": 2})
    saved = learning.propose(item.id, proposal, TEST_USERNAME, ALL)
    document = knowledge.get_document(saved.document_id)
    assert not document.enabled and "版本 2" in document.content
    assert not knowledge.chunks()
    knowledge.update_document(
        document.id,
        source=document.source,
        content=document.content,
        enabled=True,
        expected_revision=saved.library_revision,
        expected_document_version=document.current_version,
        actor=TEST_USERNAME,
    )
    chunks = knowledge.chunks()
    assert chunks and all(
        chunk.repository_scope == "lboverfys/niuma" for chunk in chunks
    )
    own_profile = service.create(draft(), TEST_USERNAME, ALL)
    assert document.source in own_profile.knowledge_versions
    team.save_repository(
        RepositoryWrite(
            repository="lboverfys/Other", expected_revision=0, policy=RepositoryPolicy()
        ),
        TEST_USERNAME,
    )
    other_profile = service.create(
        ProfileCreate(
            name="其他仓库方案", repository="lboverfys/Other", expected_ai_revision=0
        ),
        TEST_USERNAME,
        ALL,
    )
    assert document.source not in other_profile.knowledge_versions
