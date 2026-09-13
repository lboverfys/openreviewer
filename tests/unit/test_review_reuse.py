"""跨 SHA 复用必须匹配所有可见输入，复用用量不能再次计费。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.worker.batches import _PersistentBatchedReviewer
from apps.worker.results import _workflow_result
from domain.enums import ModelBatchStatus, ModelProvider, ReviewAgent
from domain.repository_policy import RepositoryPolicySnapshot
from services.agent_workflow import AgentExecution, WorkflowExecution
from services.model_review import ModelServiceSettings, combine_model_review_results
from services.review_reuse import restore_reused, reusable_payload, reuse_identity
from tests.unit.test_model_review import make_model_input, make_output, make_result


def inputs():
    source = make_model_input()
    policy = RepositoryPolicySnapshot(repository=source.repository, revision=1,
        review_profile_id="profile-1", incremental_review=True)
    source = source.model_copy(update={"repository_policy": policy, "review_agent": ReviewAgent.LOGIC,
        "reuse_dependencies": {"src/auth.py": source.units[0].blob_sha}})
    new_sha = "f" * 40
    target = source.model_copy(update={"head_sha": new_sha, "review_version_key": f"42:9:{new_sha}",
        "review_run_id": "run-2", "review_plan_id": "plan-2", "plan_fingerprint": "9" * 64,
        "units": (source.units[0].model_copy(update={"head_sha": new_sha, "unit_key": "1" * 64,
            "review_version_key": f"42:9:{new_sha}"}),)})
    settings = ModelServiceSettings(ModelProvider.OPENAI, "test-model", "fixture-key")
    return source, target, settings


def test_same_input_across_commits_rebinds_finding_identity_and_zeroes_usage():
    source, target, settings = inputs()
    first, second = reuse_identity(source, settings), reuse_identity(target, settings)
    assert first and second and first.key == second.key
    result = make_result(make_output(), "a" * 64)
    payload = reusable_payload(result, first, source.head_sha)
    restored = restore_reused(payload, second, source.review_run_id, target.head_sha)
    assert restored.output.findings[0].unit_key == target.units[0].unit_key
    assert restored.usage.input_tokens == restored.usage.output_tokens == 0
    assert restored.duration_ms == restored.estimated_cost_microusd == 0
    assert restored.reused_from_run_id == source.review_run_id
    assert restored.reused_input_tokens == 10
    assert restored.provider_request_id is None
    assert restored.response_status is None
    assert type(restored).model_validate(restored.model_dump(mode="json")).reused_from_run_id == source.review_run_id
    assert reusable_payload(restored, second, target.head_sha) is None


@pytest.mark.parametrize("field,value", [
    ("knowledge_references", ("policy.md#rule: new knowledge",)),
    ("knowledge_versions", {"policy.md": "2"}),
    ("reuse_dependencies", {"src/caller.java": "changed"}),
    ("pull_request_number", 10),
])
def test_changed_inputs_invalidate_cache(field, value):
    source, target, settings = inputs()
    assert reuse_identity(source, settings).key != reuse_identity(target.model_copy(update={field: value}), settings).key


def test_code_config_application_and_profile_changes_invalidate(monkeypatch):
    source, target, settings = inputs()
    original = reuse_identity(source, settings).key
    assert reuse_identity(target, replace(settings, model="other-model")).key != original
    changed = target.model_copy(update={"units": (target.units[0].model_copy(update={"patch": "changed"}),)})
    assert reuse_identity(changed, settings).key != original
    monkeypatch.setenv("OPENREVIEWER_DEPLOYMENT_IMAGE", "app:" + "d" * 40)
    assert reuse_identity(target, settings).key != original
    assert reuse_identity(target.model_copy(update={"review_agent": ReviewAgent.SUMMARY}), settings) is None
    assert reuse_identity(make_model_input(), settings) is None


def test_output_mentioning_old_sha_is_not_reused():
    source, _, settings = inputs()
    result = make_result(make_output().model_copy(update={"summary": source.head_sha}), "a" * 64)
    assert reusable_payload(result, reuse_identity(source, settings), source.head_sha) is None


def test_reused_only_and_mixed_workflows_preserve_actual_request_semantics():
    source, target, settings = inputs()
    original = make_result(make_output(), "b" * 64)
    reused = restore_reused(reusable_payload(original, reuse_identity(source, settings), source.head_sha),
                            reuse_identity(target, settings), source.review_run_id, target.head_sha)
    combined = combine_model_review_results(target, (reused,))
    assert combined.response_status is None and combined.reused_from_run_id == source.review_run_id
    now = datetime.now(UTC)
    cached_agent = AgentExecution(ReviewAgent.LOGIC, "completed", reused, 0, None)
    workflow = WorkflowExecution("completed", (cached_agent,), reused.output.findings, "复用结果", now, now)
    aggregate = _workflow_result(target, workflow)
    assert aggregate.response_status is None and aggregate.usage.total_input_tokens == 0
    actual = make_result(reused.output, "b" * 64)
    mixed = replace(workflow, agents=(cached_agent, AgentExecution(ReviewAgent.SECURITY, "completed", actual, 10, None)))
    aggregate = _workflow_result(target, mixed)
    assert aggregate.response_status == 200 and aggregate.usage.total_input_tokens == 10
    assert aggregate.reused_from_run_id is None and aggregate.reused_input_tokens == 10


def test_sealed_agent_reuse_resumes_without_replanning_or_calling_model():
    source, target, settings = inputs()
    reused = restore_reused(reusable_payload(make_result(make_output(), "b" * 64), reuse_identity(source, settings), source.head_sha),
                            reuse_identity(target, settings), source.review_run_id, target.head_sha)
    queue = SimpleNamespace(load_model_batches=lambda *args, **kwargs: (
        SimpleNamespace(result=reused, status=ModelBatchStatus.SUCCEEDED),))
    cursor = SimpleNamespace(lease=SimpleNamespace(review_run_id=target.review_run_id), raise_if_lease_lost=lambda: None)
    reviewer = Mock()
    reviewer.review.side_effect = AssertionError("已封存结果不应重新调用模型")
    wrapped = _PersistentBatchedReviewer(queue, cursor, ReviewAgent.LOGIC, reviewer, settings, timedelta(minutes=5))
    assert wrapped.review(target) == reused
    reviewer.review.assert_not_called()
