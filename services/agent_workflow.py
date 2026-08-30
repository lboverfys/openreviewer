"""固定三路审查 Agent 的并行编排器。

该模块只负责确定性调度和结果汇总，模型不能改变节点顺序或触发外部副作用。
数据库租约和批次表由持久化队列负责；这里的纯函数边界便于离线测试并发上限。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from domain.enums import ModelCallStatus, ReviewAgent
from domain.model_review import (
    MAX_MODEL_FINDINGS,
    ModelFindingCandidate,
    ModelReviewInput,
    ModelReviewResult,
)
from domain.security import SafeError
from services.model_review import ModelReviewer, ModelServiceSettings

PARALLEL_AGENTS: tuple[ReviewAgent, ...] = (
    ReviewAgent.SECURITY,
    ReviewAgent.CONVENTION,
    ReviewAgent.LOGIC,
)


@dataclass(frozen=True, slots=True)
class AgentExecution:
    agent: ReviewAgent
    status: str
    result: ModelReviewResult | None
    duration_ms: int
    error: str | None
    references: tuple[str, ...] = ()
    safe_error: SafeError | None = None

    @property
    def finding_count(self) -> int:
        return len(self.result.output.findings) if self.result is not None else 0


@dataclass(frozen=True, slots=True)
class WorkflowExecution:
    status: str
    agents: tuple[AgentExecution, ...]
    findings: tuple[ModelFindingCandidate, ...]
    summary: str
    started_at: datetime
    completed_at: datetime
    summary_execution: AgentExecution | None = None


class FixedAgentWorkflow:
    """以固定线程池并行执行三路 Agent，再确定性去重排序。"""

    def __init__(
        self,
        reviewers: Mapping[ReviewAgent, ModelReviewer],
        *,
        summary_reviewer: ModelReviewer | None = None,
        agent_settings: Mapping[ReviewAgent, ModelServiceSettings] | None = None,
        max_concurrency: int = 3,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= max_concurrency <= len(PARALLEL_AGENTS):
            raise ValueError("Agent 并发上限必须在 1 到 3 之间")
        self._reviewers = dict(reviewers)
        self._summary_reviewer = summary_reviewer
        self._agent_settings = dict(agent_settings or {})
        self._max_concurrency = max_concurrency
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def reviewers(self) -> Mapping[ReviewAgent, ModelReviewer]:
        """返回固定三路 Agent 的适配器映射副本。"""

        return dict(self._reviewers)

    @property
    def summary_reviewer(self) -> ModelReviewer | None:
        return self._summary_reviewer

    @property
    def agent_settings(self) -> Mapping[ReviewAgent, ModelServiceSettings]:
        """返回 Worker 内部使用的各 Agent 批次配置副本。"""

        return dict(self._agent_settings)

    @property
    def max_concurrency(self) -> int:
        """返回三路审查 Agent 的实际并发上限。"""

        return self._max_concurrency

    def run(
        self,
        review_input: ModelReviewInput,
        *,
        references: Mapping[ReviewAgent, tuple[str, ...]] | None = None,
        on_aggregating: Callable[[], None] | None = None,
    ) -> WorkflowExecution:
        started_at = self._clock()
        refs = references or {}
        executions: dict[ReviewAgent, AgentExecution] = {}

        def invoke(agent: ReviewAgent) -> AgentExecution:
            reviewer = self._reviewers.get(agent)
            if reviewer is None:
                return AgentExecution(
                    agent,
                    "disabled",
                    None,
                    0,
                    "Agent 未启用",
                    refs.get(agent, ()),
                )
            begin = time.monotonic()
            try:
                agent_input = review_input.model_copy(
                    update={
                        "knowledge_references": refs.get(agent, ()),
                        "review_agent": agent,
                    }
                )
                result = reviewer.review(agent_input)
                if (
                    result.status is not ModelCallStatus.SUCCEEDED
                    and not (
                        result.status is ModelCallStatus.SKIPPED
                        and not review_input.units
                    )
                ):
                    return AgentExecution(
                        agent,
                        "failed",
                        result,
                        max(0, int((time.monotonic() - begin) * 1000)),
                        "Agent 未返回成功结果",
                        refs.get(agent, ()),
                    )
                return AgentExecution(
                    agent,
                    "completed",
                    result,
                    max(0, int((time.monotonic() - begin) * 1000)),
                    None,
                    refs.get(agent, ()),
                )
            except Exception as exc:
                safe_error = SafeError.from_exception(exc)
                return AgentExecution(
                    agent,
                    "failed",
                    None,
                    max(0, int((time.monotonic() - begin) * 1000)),
                    safe_error.safe_message,
                    refs.get(agent, ()),
                    safe_error,
                )

        with ThreadPoolExecutor(max_workers=self._max_concurrency) as pool:
            futures = {pool.submit(invoke, agent): agent for agent in PARALLEL_AGENTS}
            for future in as_completed(futures):
                execution = future.result()
                executions[execution.agent] = execution

        ordered = tuple(executions[agent] for agent in PARALLEL_AGENTS)
        findings = _merge_findings(ordered)
        failed = any(item.status == "failed" for item in ordered)
        disabled = any(item.status == "disabled" for item in ordered)
        status = "failed" if failed or disabled else "completed"
        summary = (
            "三路 Agent 均完成，未发现可靠问题"
            if not findings and not failed and not disabled
            else f"三路 Agent 汇总 {len(findings)} 条候选问题"
            if not failed and not disabled
            else "至少一个 Agent 未启用或失败，结果覆盖不完整"
        )
        if not failed and not disabled and on_aggregating is not None:
            # 回调位于三路结果已确定、汇总模型尚未调用的边界。持久化实现可在
            # 这里用短事务暴露 DAG 状态，异常则直接阻止后续外部模型请求。
            on_aggregating()
        # 汇总模型只能处理确定性准备好的候选；失败时仍保留部分结果，不能伪造成功。
        summary_execution: AgentExecution | None = None
        if (
            self._summary_reviewer is not None
            and not failed
            and not disabled
            and review_input.units
        ):
            summary_started = time.monotonic()
            summary_input = review_input.model_copy(
                update={
                    "knowledge_references": refs.get(ReviewAgent.SUMMARY, ()),
                    "prior_agent_results": _execution_context(ordered),
                    "review_agent": ReviewAgent.SUMMARY,
                }
            )
            try:
                summary_result = self._summary_reviewer.review(summary_input)
                if summary_result.status is ModelCallStatus.SUCCEEDED:
                    findings = _merge_candidates(findings, summary_result.output.findings)
                    if summary_result.output.summary is not None:
                        summary = summary_result.output.summary
                    summary_execution = AgentExecution(
                        ReviewAgent.SUMMARY,
                        "completed",
                        summary_result,
                        max(0, int((time.monotonic() - summary_started) * 1000)),
                        None,
                        refs.get(ReviewAgent.SUMMARY, ()),
                    )
                else:
                    status = "failed"
                    summary = "汇总 Agent 未返回成功结果，结果覆盖不完整"
                    summary_execution = AgentExecution(
                        ReviewAgent.SUMMARY,
                        "failed",
                        summary_result,
                        max(0, int((time.monotonic() - summary_started) * 1000)),
                        "汇总 Agent 未返回成功结果",
                        refs.get(ReviewAgent.SUMMARY, ()),
                    )
            except Exception as exc:
                safe_error = SafeError.from_exception(exc)
                status = "failed"
                summary = "汇总 Agent 调用失败，结果覆盖不完整"
                summary_execution = AgentExecution(
                    ReviewAgent.SUMMARY,
                    "failed",
                    None,
                    max(0, int((time.monotonic() - summary_started) * 1000)),
                    safe_error.safe_message,
                    refs.get(ReviewAgent.SUMMARY, ()),
                    safe_error,
                )
        completed_at = self._clock()
        return WorkflowExecution(
            status=status,
            agents=ordered,
            findings=findings,
            summary=summary,
            started_at=started_at,
            completed_at=completed_at,
            summary_execution=summary_execution,
        )

    def close(self) -> None:
        """释放四个 Agent 适配器持有的 HTTP 客户端。"""

        closed: set[int] = set()
        for reviewer in (*self._reviewers.values(), self._summary_reviewer):
            if reviewer is None or id(reviewer) in closed:
                continue
            closed.add(id(reviewer))
            reviewer.close()


def _merge_findings(
    executions: tuple[AgentExecution, ...],
) -> tuple[ModelFindingCandidate, ...]:
    candidates: list[ModelFindingCandidate] = []
    for execution in executions:
        if execution.result is not None:
            candidates.extend(execution.result.output.findings)
    return _merge_candidates((), tuple(candidates))


def _merge_candidates(
    first: tuple[ModelFindingCandidate, ...],
    second: tuple[ModelFindingCandidate, ...],
) -> tuple[ModelFindingCandidate, ...]:
    by_identity: dict[str, ModelFindingCandidate] = {}
    for candidate in (*first, *second):
        identity = _candidate_identity(candidate)
        existing = by_identity.get(identity)
        if existing is None or candidate.confidence > existing.confidence:
            by_identity[identity] = candidate
    rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    return tuple(
        item
        for _key, item in sorted(
            by_identity.items(),
            key=lambda pair: (
                -rank[pair[1].severity.value],
                -pair[1].confidence,
                pair[0],
            ),
        )[:MAX_MODEL_FINDINGS]
    )


def _candidate_identity(candidate: ModelFindingCandidate) -> str:
    location = candidate.location
    payload = {
        "unit_key": candidate.unit_key,
        "category": candidate.category.value,
        "file": location.file if location else None,
        "symbol": _normalize(location.symbol if location else None),
        "title": _normalize(candidate.title),
        "rule_reference": candidate.rule_reference,
    }
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _normalize(value: str | None) -> str | None:
    return " ".join(value.casefold().split()) if value is not None else None


def _execution_context(executions: tuple[AgentExecution, ...]) -> tuple[str, ...]:
    """把前三路结构化结果压缩成汇总提示词可用且有界的上下文。"""

    context: list[str] = []
    for execution in executions:
        findings = (
            execution.result.output.findings
            if execution.result is not None
            else ()
        )
        payload = {
            "agent": execution.agent.value,
            "status": execution.status,
            "verdict": (
                execution.result.output.verdict.value
                if execution.result is not None
                and execution.result.output.verdict is not None
                else None
            ),
            "summary": (
                execution.result.output.summary
                if execution.result is not None
                else None
            ),
            "checked_areas": (
                list(execution.result.output.checked_areas)
                if execution.result is not None
                else []
            ),
            "findings": [
                {
                    "unit_key": item.unit_key,
                    "severity": item.severity.value,
                    "category": item.category.value,
                    "location": (
                        item.location.model_dump(mode="json")
                        if item.location is not None
                        else None
                    ),
                    "title": item.title,
                    "evidence": item.evidence[:800],
                    "impact": item.impact[:800],
                    "suggestion": item.suggestion[:800],
                    "confidence": item.confidence,
                    "rule_reference": item.rule_reference,
                }
                for item in findings[:MAX_MODEL_FINDINGS]
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        context.append(encoded[:2_000])
    return tuple(context)
