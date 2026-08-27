from datetime import UTC, datetime

import pytest

from apps.worker.main import _agent_conclusion_payload, _workflow_result
from domain.enums import (
    ExecutionStatus,
    ModelApiProtocol,
    ModelCallStatus,
    ModelProvider,
    ModelReviewVerdict,
    ReviewAgent,
)
from domain.model_review import ModelReviewOutput, ModelReviewResult, ModelTokenUsage
from domain.workflow import (
    WorkflowAction,
    WorkflowTransitionError,
    next_automatic_stage,
    transition,
)
from services.agent_workflow import AgentExecution, FixedAgentWorkflow, WorkflowExecution
from services.task_queue import TaskQueueError
from tests.unit.test_model_review import make_model_input


class StaticReviewer:
    def __init__(
        self,
        result: ModelReviewResult,
        *,
        name: str,
        calls: list[str],
        inputs: list | None = None,
    ) -> None:
        self.result = result
        self.name = name
        self.calls = calls
        self.inputs = inputs

    def review(self, review_input):
        self.calls.append(self.name)
        if self.inputs is not None:
            self.inputs.append(review_input)
        return self.result

    def close(self) -> None:
        return None


def model_result(
    fingerprint_digit: str,
    *,
    provider: ModelProvider = ModelProvider.OPENAI,
    protocol: ModelApiProtocol = ModelApiProtocol.CHAT_COMPLETIONS,
    model: str = "test-model",
    status: ModelCallStatus = ModelCallStatus.SUCCEEDED,
    request_id: str | None = None,
) -> ModelReviewResult:
    succeeded = status is ModelCallStatus.SUCCEEDED
    return ModelReviewResult(
        provider=provider,
        api_protocol=protocol,
        model=model,
        status=status,
        prompt_version="test",
        request_fingerprint=fingerprint_digit * 64,
        provider_request_id=request_id if succeeded else None,
        response_status=200 if succeeded else None,
        duration_ms=10,
        usage=(
            ModelTokenUsage(input_tokens=10, output_tokens=2)
            if succeeded
            else ModelTokenUsage(input_tokens=0, output_tokens=0)
        ),
        output=(
            ModelReviewOutput(
                verdict=ModelReviewVerdict.NO_ACTIONABLE_ISSUE,
                summary="当前审查范围内未发现可报告问题。",
                checked_areas=("测试范围",),
                findings=(),
            )
            if succeeded
            else ModelReviewOutput(findings=())
        ),
    )


def test_fixed_dag_automatic_edges() -> None:
    assert next_automatic_stage(ExecutionStatus.CI) is ExecutionStatus.PLANNING
    assert (
        next_automatic_stage(ExecutionStatus.AGENT_BATCHES)
        is ExecutionStatus.AGGREGATING
    )
    assert next_automatic_stage(ExecutionStatus.AWAITING_APPROVAL) is None


def test_approval_can_only_open_manual_publish_gate() -> None:
    result = transition(ExecutionStatus.AWAITING_APPROVAL, WorkflowAction.APPROVE)
    assert result.after is ExecutionStatus.APPROVED
    assert next_automatic_stage(result.after) is ExecutionStatus.AWAITING_PUBLISH
    with pytest.raises(WorkflowTransitionError):
        transition(ExecutionStatus.AGENT_BATCHES, WorkflowAction.APPROVE)
    with pytest.raises(WorkflowTransitionError):
        transition(ExecutionStatus.AWAITING_APPROVAL, WorkflowAction.PUBLISH)


def test_reject_and_stage_retry_are_explicit() -> None:
    rejected = transition(ExecutionStatus.AWAITING_APPROVAL, WorkflowAction.REJECT)
    assert rejected.after is ExecutionStatus.REJECTED
    retried = transition(
        ExecutionStatus.REJECTED,
        WorkflowAction.RETRY_STAGE,
        target_stage=ExecutionStatus.AGENT_BATCHES,
    )
    assert retried.after is ExecutionStatus.AGENT_BATCHES


def test_approved_review_can_be_rejected_before_publish() -> None:
    rejected = transition(
        ExecutionStatus.AWAITING_PUBLISH,
        WorkflowAction.REJECT,
    )

    assert rejected.after is ExecutionStatus.REJECTED


def test_pause_only_accepts_persistable_resume_origins() -> None:
    paused = transition(ExecutionStatus.AGGREGATING, WorkflowAction.PAUSE)
    assert paused.after is ExecutionStatus.PAUSED

    with pytest.raises(WorkflowTransitionError):
        transition(ExecutionStatus.WAITING_FOR_CI, WorkflowAction.PAUSE)
    with pytest.raises(WorkflowTransitionError):
        transition(ExecutionStatus.PUBLISHING, WorkflowAction.PAUSE)


def test_fixed_agents_mark_aggregating_before_summary() -> None:
    calls: list[str] = []
    reviewers = {
        agent: StaticReviewer(
            model_result(str(index)),
            name=agent.value,
            calls=calls,
        )
        for index, agent in enumerate(
            (
                ReviewAgent.SECURITY,
                ReviewAgent.CONVENTION,
                ReviewAgent.LOGIC,
            ),
            start=1,
        )
    }
    summary = StaticReviewer(
        model_result("4", model="summary-model"),
        name="summary",
        calls=calls,
    )
    workflow = FixedAgentWorkflow(
        reviewers,
        summary_reviewer=summary,
        max_concurrency=3,
    )

    execution = workflow.run(
        make_model_input(),
        on_aggregating=lambda: calls.append("aggregating"),
    )

    assert execution.status == "completed"
    assert set(calls[:3]) == {"security", "convention", "logic"}
    assert calls[-2:] == ["aggregating", "summary"]


def test_non_success_agent_skips_aggregating_and_summary() -> None:
    calls: list[str] = []
    reviewers = {
        ReviewAgent.SECURITY: StaticReviewer(
            model_result("1", status=ModelCallStatus.SKIPPED),
            name="security",
            calls=calls,
        ),
        ReviewAgent.CONVENTION: StaticReviewer(
            model_result("2"),
            name="convention",
            calls=calls,
        ),
        ReviewAgent.LOGIC: StaticReviewer(
            model_result("3"),
            name="logic",
            calls=calls,
        ),
    }
    summary = StaticReviewer(
        model_result("4"),
        name="summary",
        calls=calls,
    )
    workflow = FixedAgentWorkflow(reviewers, summary_reviewer=summary)

    execution = workflow.run(
        make_model_input(),
        on_aggregating=lambda: calls.append("aggregating"),
    )

    assert execution.status == "failed"
    assert "aggregating" not in calls
    assert "summary" not in calls
    security = next(
        item for item in execution.agents if item.agent is ReviewAgent.SECURITY
    )
    assert security.status == "failed"
    assert security.error == "Agent 未返回成功结果"


def test_empty_review_completes_with_skipped_agents() -> None:
    calls: list[str] = []
    skipped_input = make_model_input().model_copy(
        update={"units": (), "total_estimated_input_bytes": 0}
    )
    reviewers = {
        agent: StaticReviewer(
            model_result(str(index), status=ModelCallStatus.SKIPPED),
            name=agent.value,
            calls=calls,
        )
        for index, agent in enumerate(
            (
                ReviewAgent.SECURITY,
                ReviewAgent.CONVENTION,
                ReviewAgent.LOGIC,
            ),
            start=1,
        )
    }
    summary = StaticReviewer(
        model_result("4", status=ModelCallStatus.SKIPPED),
        name="summary",
        calls=calls,
    )
    workflow = FixedAgentWorkflow(reviewers, summary_reviewer=summary)

    execution = workflow.run(
        skipped_input,
        on_aggregating=lambda: calls.append("aggregating"),
    )
    combined = _workflow_result(skipped_input, execution)

    assert execution.status == "completed"
    assert execution.summary_execution is None
    assert calls[-1] == "aggregating"
    assert "summary" not in calls
    assert combined.status is ModelCallStatus.SKIPPED
    assert combined.response_status is None
    assert combined.usage.total_input_tokens == 0


def test_fixed_agents_receive_scoped_knowledge_references() -> None:
    calls: list[str] = []
    inputs: list = []
    reviewers = {
        agent: StaticReviewer(
            model_result(str(index)),
            name=agent.value,
            calls=calls,
            inputs=inputs,
        )
        for index, agent in enumerate(
            (
                ReviewAgent.SECURITY,
                ReviewAgent.CONVENTION,
                ReviewAgent.LOGIC,
            ),
            start=1,
        )
    }
    summary = StaticReviewer(
        model_result("4"),
        name="summary",
        calls=calls,
        inputs=inputs,
    )
    references = {
        agent: (f"{agent.value}-reference",)
        for agent in ReviewAgent
    }

    FixedAgentWorkflow(reviewers, summary_reviewer=summary).run(
        make_model_input(),
        references=references,
    )

    by_reference = {
        item.knowledge_references[0]: item for item in inputs
    }
    assert set(by_reference) == {
        "security-reference",
        "convention-reference",
        "logic-reference",
        "summary-reference",
    }
    assert by_reference["summary-reference"].prior_agent_results
    for agent in ReviewAgent:
        assert by_reference[f"{agent.value}-reference"].review_agent is agent


def test_workflow_compatibility_result_uses_summary_configuration() -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    security = model_result("1", model="security-model")
    convention = model_result(
        "2",
        provider=ModelProvider.ANTHROPIC,
        protocol=ModelApiProtocol.MESSAGES,
        model="convention-model",
    )
    logic = model_result(
        "3",
        protocol=ModelApiProtocol.RESPONSES,
        model="logic-model",
    )
    summary = model_result(
        "4",
        provider=ModelProvider.ANTHROPIC,
        protocol=ModelApiProtocol.MESSAGES,
        model="summary-model",
        request_id="summary-request",
    )
    execution = WorkflowExecution(
        status="completed",
        agents=(
            AgentExecution(ReviewAgent.SECURITY, "completed", security, 10, None),
            AgentExecution(ReviewAgent.CONVENTION, "completed", convention, 10, None),
            AgentExecution(ReviewAgent.LOGIC, "completed", logic, 10, None),
        ),
        findings=(),
        summary="完成",
        started_at=now,
        completed_at=now,
        summary_execution=AgentExecution(
            ReviewAgent.SUMMARY,
            "completed",
            summary,
            10,
            None,
        ),
    )

    combined = _workflow_result(make_model_input(), execution)

    assert combined.provider is ModelProvider.ANTHROPIC
    assert combined.api_protocol is ModelApiProtocol.MESSAGES
    assert combined.model == "summary-model"
    assert combined.provider_request_id == "summary-request"
    assert combined.usage.input_tokens == 40
    assert combined.usage.output_tokens == 8
    assert combined.duration_ms == 40
    assert combined.output.verdict is ModelReviewVerdict.NO_ACTIONABLE_ISSUE
    assert combined.output.summary == "当前审查范围内未发现可报告问题。"


def test_agent_completion_event_exposes_only_the_structured_conclusion() -> None:
    execution = AgentExecution(
        ReviewAgent.SECURITY,
        "completed",
        model_result("1"),
        10,
        None,
    )

    payload = _agent_conclusion_payload(execution)

    assert payload == {
        "verdict": "no_actionable_issue",
        "summary": "当前审查范围内未发现可报告问题。",
        "checked_areas": ["测试范围"],
    }
    assert "findings" not in payload
    assert "raw_response" not in payload


def test_workflow_result_keeps_recovered_legacy_summary_compatible() -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    legacy_summary = model_result("4").model_copy(
        update={"output": ModelReviewOutput(findings=())}
    )
    agent_results = tuple(model_result(str(index)) for index in range(1, 4))
    execution = WorkflowExecution(
        status="completed",
        agents=tuple(
            AgentExecution(agent, "completed", result, 10, None)
            for agent, result in zip(
                (
                    ReviewAgent.SECURITY,
                    ReviewAgent.CONVENTION,
                    ReviewAgent.LOGIC,
                ),
                agent_results,
                strict=True,
            )
        ),
        findings=(),
        summary="旧批次恢复完成",
        started_at=now,
        completed_at=now,
        summary_execution=AgentExecution(
            ReviewAgent.SUMMARY,
            "completed",
            legacy_summary,
            10,
            None,
        ),
    )

    combined = _workflow_result(make_model_input(), execution)

    assert combined.output.verdict is None
    assert combined.output.summary is None
    assert combined.output.checked_areas == ()


def test_workflow_compatibility_result_rejects_partial_success() -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    skipped = model_result("1", status=ModelCallStatus.SKIPPED)
    execution = WorkflowExecution(
        status="failed",
        agents=(
            AgentExecution(ReviewAgent.SECURITY, "failed", skipped, 0, "跳过"),
        ),
        findings=(),
        summary="不完整",
        started_at=now,
        completed_at=now,
    )

    with pytest.raises(TaskQueueError):
        _workflow_result(make_model_input(), execution)
