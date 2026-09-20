"""候选只改 Prompt，试跑只改新运行绑定，保留旧任务和发布边界。"""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select

from domain.enums import ModelProvider, ReviewAgent
from domain.platform import PlatformNotFoundError, ProfileCreate
from domain.repository_policy import RepositoryPolicy, RepositoryPolicySnapshot
from persistence.models import (
    OutboxEventRecord,
    RepositoryPolicyRecord,
    ReviewRunRecord,
)
from persistence.review_management import SqlAlchemyReviewManagementRepository
from services.model_providers import create_model_reviewer
from services.model_review import (
    PROMPT_VERSION,
    ModelServiceSettings,
    StructuredReviewPromptBuilder,
)
from services.rbac import ResourceScope
from services.review_management import (
    ReviewAction,
    ReviewActionConflictError,
    ReviewManagementService,
    ReviewNotFoundError,
)
from services.review_profiles import ReviewProfileRuntimeLoader
from services.review_reuse import reuse_identity
from services.team import RepositoryWrite
from tests.integration.test_management_api import database as database
from tests.integration.test_review_plan_persistence import (
    MutableClock,
    _prepare_planning_lease,
)
from tests.integration.test_review_profiles import ALL, draft, profile_services
from tests.support import TEST_USERNAME
from tests.unit.test_model_review import make_model_input, make_output


def test_candidate_freezes_base_settings_and_keeps_output_contract_and_prior_content(database, tmp_path):
    _, _, repository, service, _, models = profile_services(database, tmp_path)
    base = service.create(draft(), TEST_USERNAME, ALL)
    before = repository.load(base.id)
    models[ReviewAgent.LOGIC] = ModelServiceSettings(ModelProvider.OPENAI, "changed-live-model", "changed-live-key")
    candidate = service.create(ProfileCreate(name="只改逻辑指令", repository=base.repository, base_profile_id=base.id,
        role_instructions={ReviewAgent.LOGIC:"只报告本次修改引入、具有调用链证据的问题。"},
        supplementary_instructions="区分既有问题与新增问题。"), TEST_USERNAME, ALL)
    after = repository.load(candidate.id)
    assert repository.load(base.id) == before
    assert candidate.models == base.models
    assert candidate.base_profile_id == base.id
    assert candidate.prompt_version == base.prompt_version == PROMPT_VERSION
    assert candidate.prompt_content_sha256 != base.prompt_content_sha256
    assert candidate.fingerprint != base.fingerprint
    assert "fixture-private-key" not in candidate.model_dump_json()
    for key in ("agents", "knowledge", "retrieval", "planning"):
        assert before["snapshot"][key] == after["snapshot"][key]
    assert after["snapshot"]["prompt"]["system"] == before["snapshot"]["prompt"]["system"]
    original = StructuredReviewPromptBuilder(before["snapshot"]["prompt"])
    changed = StructuredReviewPromptBuilder(after["snapshot"]["prompt"])
    review_input = make_model_input().model_copy(update={"review_agent":ReviewAgent.LOGIC,
        "repository_policy":RepositoryPolicySnapshot.model_validate({"repository":base.repository,"revision":1,"incremental_review":True,"review_profile_id":base.id})})
    old_prompt = original.build(review_input, ModelProvider.OPENAI, "fixture-original")
    new_prompt = changed.build(review_input, ModelProvider.OPENAI, "fixture-original")
    new_body, old_body = json.loads(new_prompt.user), json.loads(old_prompt.user)
    assert new_body["review_role"]["responsibility"] == candidate.role_instructions["logic"]
    assert new_body["supplementary_review_requirements"] == candidate.supplementary_instructions
    assert new_body["output_contract"] == old_body["output_contract"]
    assert new_body["allowed_rule_references"] == old_body["allowed_rule_references"]
    assert new_prompt.system == old_prompt.system
    assert new_prompt.request_fingerprint != old_prompt.request_fingerprint
    settings = ModelServiceSettings(ModelProvider.OPENAI, "fixture-original", "fixture-key", prompt_snapshot=before["snapshot"]["prompt"])
    left = reuse_identity(review_input, settings)
    right = reuse_identity(review_input, replace(settings, prompt_snapshot=after["snapshot"]["prompt"]))
    assert left is not None and right is not None and left.key != right.key


def test_legacy_prompt_snapshot_keeps_its_exact_request_identity(database, tmp_path):
    _, _, repository, service, _, _ = profile_services(database, tmp_path)
    base = service.create(draft(), TEST_USERNAME, ALL)
    legacy = deepcopy(repository.load(base.id)["snapshot"]["prompt"])
    legacy.pop("content_sha256")
    digest = sha256(json.dumps(legacy, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    builder = StructuredReviewPromptBuilder(legacy)
    assert builder.version == f"{PROMPT_VERSION}.{digest}"
    first = builder.build(make_model_input(), ModelProvider.OPENAI, "fixture-original")
    expected = sha256(json.dumps({"provider":"openai", "api_protocol":None, "model":"fixture-original",
        "prompt_version":builder.version, "system":legacy["system"], "user":first.user},
        ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert first.request_fingerprint == expected
    service.create(ProfileCreate(name="候选", repository=base.repository, base_profile_id=base.id,
        role_instructions={ReviewAgent.LOGIC:"新版指令"}), TEST_USERNAME, ALL)
    assert StructuredReviewPromptBuilder(legacy).build(make_model_input(), ModelProvider.OPENAI, "fixture-original") == first


def test_saved_candidate_enters_actual_http_payload_and_result_provenance(database, tmp_path, monkeypatch):
    _, _, repository, service, _, _ = profile_services(database, tmp_path)
    base = service.create(draft(), TEST_USERNAME, ALL)
    candidate = service.create(ProfileCreate(name="请求验证", repository=base.repository, base_profile_id=base.id,
        role_instructions={ReviewAgent.LOGIC:"沿调用链核查事务提交边界"}), TEST_USERNAME, ALL)
    monkeypatch.setattr("services.review_profiles.create_model_reviewer", lambda settings: SimpleNamespace(close=lambda: None))
    runtime = ReviewProfileRuntimeLoader(repository, service.cipher)(candidate.id)
    settings = runtime.agent_workflow.agent_settings[ReviewAgent.LOGIC]

    def handler(request):
        payload = json.loads(request.content)
        body = json.loads(payload["input"][1]["content"][0]["text"])
        assert body["review_role"]["responsibility"] == "沿调用链核查事务提交边界"
        assert body["output_contract"]["required"] == ["verdict", "summary", "checked_areas", "findings"]
        return httpx.Response(200, json={"id":"candidate-call", "status":"completed", "output":[
            {"type":"message", "content":[{"type":"output_text", "text":make_output().model_dump_json()}]},
        ], "usage":{"input_tokens":100,"output_tokens":20}})

    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = create_model_reviewer(settings, client=client).review(make_model_input().model_copy(update={"review_agent":ReviewAgent.LOGIC}))
        assert result.provenance.prompt_content_sha256 == candidate.prompt_content_sha256
        assert result.provenance.prompt_protocol_version == PROMPT_VERSION
        assert result.prompt_version == PROMPT_VERSION
    finally:
        runtime.agent_workflow.close()


def test_candidate_contract_and_scope_cannot_edit_fixed_system_or_foreign_profile(database, tmp_path):
    _, _, _, service, _, _ = profile_services(database, tmp_path)
    base = service.create(draft(), TEST_USERNAME, ALL)
    with pytest.raises(ValidationError):
        ProfileCreate.model_validate({"name":"不允许", "repository":base.repository, "base_profile_id":base.id, "system":"移除输出契约"})
    with pytest.raises(ValidationError):
        ProfileCreate(name="过长", repository=base.repository, base_profile_id=base.id, role_instructions={ReviewAgent.LOGIC:"a" * 6001})
    with pytest.raises(PlatformNotFoundError):
        service.create(ProfileCreate(name="越权", repository=base.repository, base_profile_id=base.id), TEST_USERNAME, ResourceScope.deny_all())


def test_candidate_trial_keeps_sha_current_binding_budget_and_publication_guard(database, tmp_path):
    team, policy, profiles, creator, _, _ = profile_services(database, tmp_path)
    base = creator.create(draft(), TEST_USERNAME, ALL)
    revision = profiles.activate(base.id, policy.revision, TEST_USERNAME, ALL, reason="隔离测试")
    team.save_repository(RepositoryWrite(repository=base.repository,expected_revision=revision,
        policy=RepositoryPolicy(review_profile_id=base.id,monthly_budget_microusd=100_000,max_concurrent_reviews=2)), TEST_USERNAME, policy.id)
    candidate = creator.create(ProfileCreate(name="候选", repository=base.repository, base_profile_id=base.id,
        role_instructions={ReviewAgent.LOGIC:"核查事务边界"}), TEST_USERNAME, ALL)
    clock = MutableClock(datetime(2026, 9, 21, tzinfo=UTC))
    _, _, _, source = _prepare_planning_lease(database, clock, complete_context=True)
    management = ReviewManagementService(SqlAlchemyReviewManagementRepository(database.sessions, clock=clock))
    management.apply_action(source, ReviewAction.CANCEL, actor=TEST_USERNAME, request_id="finish-source")
    options = {"actor":TEST_USERNAME, "request_id":"trial", "review_profile_id":candidate.id, "capture_model_outputs":True, "scope":ALL}
    trial = management.apply_action(source, ReviewAction.REVIEW_SNAPSHOT, **options)
    assert management.apply_action(source, ReviewAction.REVIEW_SNAPSHOT, **options) == trial
    with pytest.raises(ReviewActionConflictError, match="方案"):
        management.apply_action(source, ReviewAction.REVIEW_SNAPSHOT, **{**options,"review_profile_id":base.id})
    with pytest.raises(ReviewNotFoundError):
        management.apply_action(source, ReviewAction.REVIEW_SNAPSHOT, **{**options,"request_id":"denied","scope":ResourceScope.deny_all()})
    with pytest.raises(ReviewActionConflictError):
        management.apply_action(source, ReviewAction.REVIEW_SNAPSHOT, **{**options,"request_id":"wrong-sha","head_sha":"b" * 40})
    with pytest.raises(ReviewActionConflictError):
        management.apply_action(trial[0], ReviewAction.PUBLISH, actor=TEST_USERNAME, request_id="cannot-publish", scope=ALL)
    with database.sessions() as session:
        run = session.get(ReviewRunRecord, trial[0])
        source_run = session.get(ReviewRunRecord, source)
        assert run.head_sha == source_run.head_sha and run.snapshot_review and run.capture_model_outputs
        assert run.repository_policy["review_profile_id"] == candidate.id
        assert run.repository_policy["incremental_review"] is False
        assert source_run.repository_policy["review_profile_id"] == base.id
        current_policy = session.scalar(select(RepositoryPolicyRecord.policy).where(RepositoryPolicyRecord.id == policy.id))
        assert current_policy["review_profile_id"] == base.id
        assert run.repository_policy["monthly_budget_microusd"] == current_policy.get("monthly_budget_microusd")
        assert run.repository_policy["monthly_budget_microusd"] == 100_000
        assert run.repository_policy["max_concurrent_reviews"] == 2
    # Outbox 到期后仍由持久化请求指纹守住幂等参数。
    with database.sessions() as session, session.begin():
        session.execute(delete(OutboxEventRecord).where(OutboxEventRecord.aggregate_id == source))
    with pytest.raises(ReviewActionConflictError, match="方案"):
        management.apply_action(source, ReviewAction.REVIEW_SNAPSHOT, **{**options,"review_profile_id":base.id})
    team.save_repository(RepositoryWrite(repository="other/repo",expected_revision=0,policy=RepositoryPolicy()), TEST_USERNAME)
    other = creator.create(ProfileCreate(name="其他仓库",repository="other/repo",expected_ai_revision=0), TEST_USERNAME, ALL)
    with pytest.raises(ReviewNotFoundError):
        management.apply_action(source, ReviewAction.REVIEW_SNAPSHOT, **{**options,"request_id":"foreign","review_profile_id":other.id})
