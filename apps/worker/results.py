"""Worker results 职责模块。"""

from hashlib import sha256

from domain.enums import ModelCallStatus, ModelReviewVerdict, ReviewAgent
from domain.model_review import (
    ModelReviewInput,
    ModelReviewOutput,
    ModelReviewResult,
    ModelTokenUsage,
)
from services.agent_workflow import WorkflowExecution
from services.task_queue import TaskQueueError


def _workflow_result(
    review_input: ModelReviewInput,
    execution: WorkflowExecution,
    *,
    allow_partial: bool = False,
) -> ModelReviewResult:
    """把多 Agent 结果折叠为一条兼容记录，汇总 Agent 作为配置代表。"""

    agent_results_list: list[ModelReviewResult] = []
    for agent_execution in execution.agents:
        if agent_execution.result is not None and (
            not allow_partial
            or agent_execution.status == "completed"
            or (
                agent_execution.agent is not ReviewAgent.SUMMARY
                and agent_execution.result.status
                in {ModelCallStatus.SUCCEEDED, ModelCallStatus.SKIPPED}
            )
        ):
            agent_results_list.append(agent_execution.result)
    agent_results = tuple(agent_results_list)
    summary_result: ModelReviewResult | None = None
    if execution.summary_execution is not None and (
        not allow_partial or execution.summary_execution.status == "completed"
    ):
        summary_result = execution.summary_execution.result
    results: tuple[ModelReviewResult, ...] = agent_results + (
        (summary_result,) if summary_result is not None else ()
    )
    if allow_partial:
        # 汇总失败或某一路失败时，只保留已经返回结构化结果的调用；失败
        # 节点由 execution 的状态字段和事件单独表达，不能阻塞本地合并。
        results = tuple(
            item
            for item in results
            if item.status in {ModelCallStatus.SUCCEEDED, ModelCallStatus.SKIPPED}
        )
    if not results:
        raise TaskQueueError("固定 Agent 没有可持久化的模型结果")
    all_succeeded = all(item.status is ModelCallStatus.SUCCEEDED for item in results)
    all_skipped = not review_input.units and all(
        item.status is ModelCallStatus.SKIPPED for item in results
    )
    if (not allow_partial and execution.status != "completed") or not (
        all_succeeded or all_skipped
    ):
        raise TaskQueueError("固定 Agent 仅能聚合全部成功的模型结果")
    # 旧表只能表达一组供应商配置。正常四 Agent 路径以最终汇总 Agent
    # 作为代表；各路真实配置、请求 ID 和计量仍保存在批次记录与结构化事件中。
    representative = summary_result or results[0]
    aggregate_status = (
        ModelCallStatus.SKIPPED if all_skipped else ModelCallStatus.SUCCEEDED
    )
    usage = ModelTokenUsage(
        input_tokens=sum(item.usage.input_tokens for item in results),
        output_tokens=sum(item.usage.output_tokens for item in results),
        cache_read_input_tokens=sum(
            item.usage.cache_read_input_tokens for item in results
        ),
        cache_write_input_tokens=sum(
            item.usage.cache_write_input_tokens for item in results
        ),
        reasoning_output_tokens=sum(
            item.usage.reasoning_output_tokens for item in results
        ),
    )
    fingerprint = sha256(
        "|".join(item.request_fingerprint for item in results).encode("ascii")
    ).hexdigest()
    final_findings = tuple(execution.findings)
    conclusion_source = (
        summary_result.output
        if summary_result is not None
        else next(
            (
                item.output
                for item in reversed(agent_results)
                if item.output.verdict is not None
            ),
            None,
        )
    )
    if (
        all_skipped
        or conclusion_source is None
        or conclusion_source.verdict is None
        or conclusion_source.summary is None
    ):
        output = ModelReviewOutput(findings=final_findings)
    else:
        verdict = (
            ModelReviewVerdict.INSUFFICIENT_CONTEXT
            if conclusion_source.verdict is ModelReviewVerdict.INSUFFICIENT_CONTEXT
            else (
                ModelReviewVerdict.ISSUES_FOUND
                if final_findings
                else ModelReviewVerdict.NO_ACTIONABLE_ISSUE
            )
        )
        output = ModelReviewOutput(
            verdict=verdict,
            summary=conclusion_source.summary,
            checked_areas=conclusion_source.checked_areas,
            findings=final_findings,
        )
    estimated_costs = tuple(item.estimated_cost_microusd for item in results)
    estimated_cost_microusd = (
        sum(cost for cost in estimated_costs if cost is not None)
        if all(cost is not None for cost in estimated_costs)
        else None
    )
    return ModelReviewResult(
        provider=representative.provider,
        api_protocol=representative.api_protocol,
        model=representative.model,
        status=aggregate_status,
        prompt_version=representative.prompt_version,
        request_fingerprint=fingerprint,
        provider_response_id=(
            representative.provider_response_id if all_succeeded else None
        ),
        provider_request_id=(
            representative.provider_request_id if all_succeeded else None
        ),
        response_status=(representative.response_status if all_succeeded else None),
        duration_ms=sum(item.duration_ms for item in results),
        usage=usage,
        estimated_cost_microusd=estimated_cost_microusd,
        output=output,
    )


def _agent_conclusion_payload(execution: object | None) -> dict[str, object]:
    """提取可公开的 Agent 结论，不包含提示词、原始响应或思维链。"""

    result = getattr(execution, "result", None)
    output = getattr(result, "output", None)
    verdict = getattr(output, "verdict", None)
    return {
        "verdict": verdict.value if verdict is not None else None,
        "summary": getattr(output, "summary", None),
        "checked_areas": list(getattr(output, "checked_areas", ())),
    }
