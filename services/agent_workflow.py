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
from threading import Event

from domain.enums import ModelCallStatus, ModelReviewVerdict, ReviewAgent
from domain.model_review import (
    MAX_MODEL_FINDINGS,
    ModelFindingCandidate,
    ModelReviewInput,
    ModelReviewResult,
)
from domain.review_planning import REVIEW_AGENTS
from domain.security import ErrorCode, SafeApplicationError, SafeError
from services.model_review import ModelReviewer, ModelServiceSettings
from services.task_queue import TaskLeaseLostError

PARALLEL_AGENTS: tuple[ReviewAgent, ...] = (
    ReviewAgent.SECURITY,
    ReviewAgent.CONVENTION,
    ReviewAgent.LOGIC,
)

# 汇总提示词中的每一路结果都必须是一个完整 JSON 字符串。限制按 UTF-8
# 字节计算，避免中文字符让字符数预算与实际 HTTP 请求大小发生偏差。
_MAX_EXECUTION_CONTEXT_BYTES = 2_000
_MAX_CONTEXT_SUMMARY_CHARS = 128
_MAX_CONTEXT_AREA_CHARS = 48
_MAX_CONTEXT_FINDING_TEXT_CHARS = 64
_MAX_CONTEXT_FINDING_TITLE_CHARS = 64
_MAX_CONTEXT_FINDING_SYMBOL_CHARS = 64
_MAX_CONTEXT_FINDING_FILE_CHARS = 160
_MAX_CONTEXT_FINDING_RULE_CHARS = 160


@dataclass(frozen=True, slots=True)
class AgentExecution:
    agent: ReviewAgent
    status: str
    result: ModelReviewResult | None
    duration_ms: int
    error: str | None
    references: tuple[str, ...] = ()
    safe_error: SafeError | None = None
    # 批次级恢复信息由 Worker 注入/读取；保留默认值以兼容旧的构造调用。
    completed_batches: tuple[int, ...] = ()
    failed_batches: tuple[int, ...] = ()
    applicable_unit_count: int = 0

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
    # ``status`` 继续保留旧的 completed/failed 语义，供旧队列和客户端使用。
    # 下列字段表达“是否有可用部分结果”以及汇总节点的真实状态，避免把
    # 未执行的汇总伪装成失败。
    coverage_status: str = "complete"
    partial_result: bool = False
    aggregation_status: str = "not_started"
    summary_status: str = "not_executed"
    failed_agents: tuple[ReviewAgent, ...] = ()
    failed_batches: tuple[tuple[str, int], ...] = ()


class _PartialAgentReviewError(SafeApplicationError):
    """某个 Agent 的后续批次失败，但前置批次已有可用结果。"""

    def __init__(
        self,
        error: SafeError,
        *,
        partial_result: ModelReviewResult,
        completed_batches: tuple[int, ...],
        failed_batch: int,
    ) -> None:
        super().__init__(error)
        self.partial_result = partial_result
        self.completed_batches = completed_batches
        self.failed_batch = failed_batch


def scope_model_review_input(
    review_input: ModelReviewInput,
    agent: ReviewAgent,
) -> ModelReviewInput:
    """按规划阶段标注的职责筛选一个 Agent 的模型输入。

    ``Review Plan`` 仍是完整快照；这里仅构造请求级子集，因此 Finding 的
    最终身份校验仍可使用完整输入。规则也同步收窄并重新计算字节总量，避免
    批次预算把未发送的规则计入请求。
    """

    if agent not in REVIEW_AGENTS or not review_input.units:
        return review_input.model_copy(update={"review_agent": agent})
    units = tuple(
        unit for unit in review_input.units if agent in unit.review_domains
    )
    if not units:
        return review_input.model_copy(
            update={
                "rules": (),
                "units": (),
                "total_estimated_input_bytes": 0,
                "review_agent": agent,
            }
        )
    rule_paths = {path for unit in units for path in unit.rule_paths}
    rules = tuple(rule for rule in review_input.rules if rule.path in rule_paths)
    total_bytes = sum(unit.estimated_input_bytes for unit in units)
    if review_input.planner_version in {"review-planner-v2", "review-planner-v3"}:
        total_bytes += sum(rule.byte_size for rule in rules)
    return review_input.model_copy(
        update={
            "rules": rules,
            "units": units,
            "total_estimated_input_bytes": total_bytes,
            "review_agent": agent,
        }
    )


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
        # 生产 Worker 开启增强模式；默认关闭是为了让升级过程中的旧调用方
        # 继续得到原有的 failed/不调用汇总行为。
        allow_partial_aggregation: bool = False,
        local_aggregation: bool = True,
        # 汇总失败后的人工重试会复用前三路结果，但必须再次进入汇总模型；
        # 该标记只由持久化租约投影，普通运行仍按候选去重结果决定是否调用。
        force_summary: bool = False,
    ) -> WorkflowExecution:
        started_at = self._clock()
        refs = references or {}
        executions: dict[ReviewAgent, AgentExecution] = {}
        # 当一个并行 Agent 发现租约已失效时，阻止尚未开始的 Future 再进入
        # reviewer；ThreadPoolExecutor 只能取消排队任务，正在进行的 HTTP
        # 请求仍由其自身超时/租约边界收敛。
        lease_lost = Event()

        def invoke(agent: ReviewAgent) -> AgentExecution:
            if lease_lost.is_set():
                raise TaskLeaseLostError()
            agent_input = scope_model_review_input(review_input, agent)
            # 有 Unit 但没有命中该 Agent 职责时，不发送空模型请求。这个状态
            # 是“不适用”，不是配置缺失，也不应出现在失败重试列表中。
            if review_input.units and not agent_input.units:
                return AgentExecution(
                    agent,
                    "not_applicable",
                    None,
                    0,
                    "当前变更不适用此 Agent",
                    refs.get(agent, ()),
                    applicable_unit_count=0,
                )
            reviewer = self._reviewers.get(agent)
            if reviewer is None:
                return AgentExecution(
                    agent,
                    "disabled",
                    None,
                    0,
                    "Agent 未启用",
                    refs.get(agent, ()),
                    applicable_unit_count=len(agent_input.units),
                )
            begin = time.monotonic()
            try:
                agent_input = agent_input.model_copy(
                    update={"knowledge_references": refs.get(agent, ())}
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
                        applicable_unit_count=len(agent_input.units),
                    )
                return AgentExecution(
                    agent,
                    "completed",
                    result,
                    max(0, int((time.monotonic() - begin) * 1000)),
                    None,
                    refs.get(agent, ()),
                    applicable_unit_count=len(agent_input.units),
                )
            except Exception as exc:
                safe_error = SafeError.from_exception(exc)
                # 租约丢失表示当前 Worker 已经失去写入权限，不能把它降级成
                # 普通 Agent 失败；由外层运行时跳过回写并交给恢复流程接管。
                if safe_error.code is ErrorCode.TASK_LEASE_LOST:
                    lease_lost.set()
                    raise
                if isinstance(exc, _PartialAgentReviewError):
                    return AgentExecution(
                        agent,
                        "failed",
                        exc.partial_result,
                        max(0, int((time.monotonic() - begin) * 1000)),
                        exc.error.safe_message,
                        refs.get(agent, ()),
                        exc.error,
                        completed_batches=exc.completed_batches,
                        failed_batches=(exc.failed_batch,),
                        applicable_unit_count=len(agent_input.units),
                    )
                raw_batch = safe_error.details.get("batch_number")
                failed_batches = (
                    (raw_batch,)
                    if isinstance(raw_batch, int) and raw_batch > 0
                    else ()
                )
                return AgentExecution(
                    agent,
                    "failed",
                    None,
                    max(0, int((time.monotonic() - begin) * 1000)),
                    safe_error.safe_message,
                    refs.get(agent, ()),
                    safe_error,
                    failed_batches=failed_batches,
                    applicable_unit_count=len(agent_input.units),
                )

        if self._max_concurrency == 1:
            # 生产环境的保守配置不需要创建线程池。更重要的是，租约丢失时
            # 能在首个 Agent 抛错的瞬间停止，不让已经排队的后续 Agent 继续
            # 发起模型请求。
            for agent in PARALLEL_AGENTS:
                executions[agent] = invoke(agent)
        else:
            with ThreadPoolExecutor(max_workers=self._max_concurrency) as pool:
                futures = {
                    pool.submit(invoke, agent): agent
                    for agent in PARALLEL_AGENTS
                }
                for future in as_completed(futures):
                    try:
                        execution = future.result()
                    except Exception as exc:
                        # 取消仍在队列中的任务，避免租约已经失效后继续
                        # 调度模型请求；正在运行的任务会在其边界自行停止。
                        safe_error = SafeError.from_exception(exc)
                        if safe_error.code is ErrorCode.TASK_LEASE_LOST:
                            lease_lost.set()
                            for pending in futures:
                                if pending is not future:
                                    pending.cancel()
                        raise
                    executions[execution.agent] = execution

        ordered = tuple(executions[agent] for agent in PARALLEL_AGENTS)
        raw_candidates = tuple(
            candidate
            for item in ordered
            if item.result is not None
            for candidate in item.result.output.findings
        )
        findings = _merge_findings(ordered)
        failed = any(item.status == "failed" for item in ordered)
        disabled = any(item.status == "disabled" for item in ordered)
        status = "failed" if failed or disabled else "completed"
        # ``not_applicable`` 表示当前变更没有落入该职责范围，不应阻塞工作流；
        # ``disabled`` 则是配置缺失，仍按失败覆盖处理。
        failed_agents = tuple(item.agent for item in ordered if item.status == "failed")
        usable_results = any(
            item.result is not None
            and item.result.status
            in {ModelCallStatus.SUCCEEDED, ModelCallStatus.SKIPPED}
            for item in ordered
        )
        partial_result = bool(
            allow_partial_aggregation
            and (failed or disabled)
            and usable_results
        )
        coverage_status = "partial" if partial_result else "complete"
        aggregation_status = "not_started"
        summary_status = "not_executed"
        summary = (
            "三路 Agent 均完成，未发现可靠问题"
            if not findings and not failed and not disabled
            else f"三路 Agent 汇总 {len(findings)} 条候选问题"
            if not failed and not disabled
            else "至少一个 Agent 未启用或失败，结果覆盖不完整"
        )
        if (not failed and not disabled or partial_result) and on_aggregating is not None:
            # 回调位于三路结果已确定、汇总模型尚未调用的边界。持久化实现可在
            # 这里用短事务暴露 DAG 状态，异常则直接阻止后续外部模型请求。
            on_aggregating()
            aggregation_status = "started"
        if allow_partial_aggregation and (failed or disabled):
            # 即使某一路失败，也先把已有候选在本地稳定合并；该状态不等于
            # 完整工作流成功，后续仍会保留失败节点供用户重试。
            aggregation_status = "local" if local_aggregation else "partial"
        # 汇总模型只能处理确定性准备好的候选；失败时仍保留部分结果，不能伪造成功。
        summary_execution: AgentExecution | None = None
        if (
            self._summary_reviewer is not None
            and not failed
            and not disabled
            and review_input.units
            and (
                force_summary
                or
                not allow_partial_aggregation
                or not local_aggregation
                or len(raw_candidates) > len(findings)
            )
        ):
            summary_status = "running"
            summary_started = time.monotonic()
            # 前三路已经逐批检查过完整补丁。汇总阶段只需要读取它们的
            # 结构化结论；再次携带所有 rules/patch 会把请求体放大数倍，
            # 在 32K 中转站上很容易触发超时或 524。保留目标身份和有界
            # prior_agent_results，明确告诉汇总模型没有新的代码单元可查。
            summary_input = review_input.model_copy(
                update={
                    "rules": (),
                    "units": (),
                    "total_estimated_input_bytes": 0,
                    "knowledge_references": refs.get(ReviewAgent.SUMMARY, ()),
                    "prior_agent_results": _execution_context(ordered),
                    "review_agent": ReviewAgent.SUMMARY,
                }
            )
            try:
                summary_result = self._summary_reviewer.review(summary_input)
                if summary_result.status is ModelCallStatus.SUCCEEDED:
                    # 汇总请求为了控制体积不携带原始 review_units/rules；模型仍
                    # 可能按 prior_agent_results 返回候选。只接受能在原始计划中
                    # 由平台校验的身份，避免未知 unit/rule 让后续
                    # ``materialize_findings`` 把整次审查判为失败。
                    summary_findings = _filter_summary_candidates(
                        review_input,
                        summary_result.output.findings,
                    )
                    # 过滤结果必须回写到汇总执行对象。否则后续事件会读取模型
                    # 原始 Finding 数量，前端看到的统计就会包含已丢弃的候选。
                    if summary_findings != summary_result.output.findings:
                        summary_output = summary_result.output.model_copy(
                            update={
                                "findings": summary_findings,
                                # ``issues_found`` 要求至少有一条 Finding；
                                # 全部候选被过滤时改成一致的结论，避免把无效
                                # 模型输出继续传播到事件和持久化边界。
                                "verdict": (
                                    ModelReviewVerdict.NO_ACTIONABLE_ISSUE
                                    if (
                                        not summary_findings
                                        and summary_result.output.verdict
                                        is ModelReviewVerdict.ISSUES_FOUND
                                    )
                                    else summary_result.output.verdict
                                ),
                            }
                        )
                        summary_result = summary_result.model_copy(
                            update={"output": summary_output}
                        )
                    findings = _merge_candidates(findings, summary_findings)
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
                    summary_status = "completed"
                    aggregation_status = "completed"
                else:
                    # 汇总是增强节点；其失败不应抹掉前三路已经完成的结果。
                    summary = "本地确定性汇总已完成，模型汇总未成功"
                    summary_execution = AgentExecution(
                        ReviewAgent.SUMMARY,
                        "failed",
                        summary_result,
                        max(0, int((time.monotonic() - summary_started) * 1000)),
                        "汇总 Agent 未返回成功结果",
                        refs.get(ReviewAgent.SUMMARY, ()),
                    )
                    summary_status = "failed"
                    aggregation_status = "local"
            except Exception as exc:
                safe_error = SafeError.from_exception(exc)
                # 汇总阶段同样不能吞掉租约失效，否则前三路结果可能被旧 Worker
                # 继续写入，且会额外发起一次没有意义的模型请求。
                if safe_error.code is ErrorCode.TASK_LEASE_LOST:
                    raise
                summary = "本地确定性汇总已完成，模型汇总调用失败"
                summary_execution = AgentExecution(
                    ReviewAgent.SUMMARY,
                    "failed",
                    None,
                    max(0, int((time.monotonic() - summary_started) * 1000)),
                    safe_error.safe_message,
                    refs.get(ReviewAgent.SUMMARY, ()),
                    safe_error,
                )
                summary_status = "failed"
                aggregation_status = "local"
        elif allow_partial_aggregation and (failed or disabled):
            summary = "上游 Agent 未完成，汇总未执行；已保留部分结果"
            summary_status = "skipped"
        elif allow_partial_aggregation and not review_input.units:
            summary_status = "skipped"
            aggregation_status = "completed"
        elif not failed and not disabled:
            # 没有重复/冲突候选时不必额外调用模型，使用本地结果即可。
            summary_status = "skipped"
            aggregation_status = "local"
        completed_at = self._clock()
        return WorkflowExecution(
            status=status,
            agents=ordered,
            findings=findings,
            summary=summary,
            started_at=started_at,
            completed_at=completed_at,
            summary_execution=summary_execution,
            coverage_status=coverage_status,
            partial_result=partial_result,
            aggregation_status=aggregation_status,
            summary_status=summary_status,
            failed_agents=failed_agents,
            failed_batches=tuple(
                (item.agent.value, number)
                for item in ordered
                for number in item.failed_batches
            ),
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


def _filter_summary_candidates(
    review_input: ModelReviewInput,
    candidates: tuple[ModelFindingCandidate, ...],
) -> tuple[ModelFindingCandidate, ...]:
    """过滤汇总 Agent 无法直接验证的候选 Finding。

    汇总阶段只看到前三路 Agent 的压缩 JSON，因此不能像普通审查批次那样
    依赖模型输入中的 unit/rule 列表。这里复用 ``materialize_findings`` 的
    身份前置约束：unit_key 必须属于当前计划，规则引用必须属于当前规则集，
    有位置时文件也必须与该 unit 一致。行号是否位于 diff 由后续平台复核，不能
    在此提前丢弃合法但未命中的候选。
    """

    units_by_key = {unit.unit_key: unit for unit in review_input.units}
    known_rules = {rule.path for rule in review_input.rules}
    filtered: list[ModelFindingCandidate] = []
    for candidate in candidates:
        unit = units_by_key.get(candidate.unit_key)
        if unit is None:
            continue
        if candidate.rule_reference is not None and (
            candidate.rule_reference not in known_rules
        ):
            continue
        if (
            candidate.location is not None
            and candidate.location.file != unit.file
        ):
            continue
        filtered.append(candidate)
    return tuple(filtered)


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
        output = execution.result.output if execution.result is not None else None
        all_findings = tuple(findings[:MAX_MODEL_FINDINGS])
        findings_payload: list[dict[str, object]] = []
        payload: dict[str, object] = {
            "agent": execution.agent.value,
            "status": execution.status,
            "verdict": (
                output.verdict.value
                if output is not None and output.verdict is not None
                else None
            ),
            "summary": (
                _bounded_context_text(output.summary, _MAX_CONTEXT_SUMMARY_CHARS)
                if output is not None and output.summary is not None
                else None
            ),
            "checked_areas": (
                [
                    _bounded_context_text(area, _MAX_CONTEXT_AREA_CHARS)
                    for area in output.checked_areas[:8]
                ]
                if output is not None
                else []
            ),
            "finding_count": len(all_findings),
            "findings": findings_payload,
        }
        # 逐条加入候选，始终在序列化后检查 UTF-8 大小。这样不会像直接
        # ``encoded[:2000]`` 那样把 JSON 截断在字符串或转义序列中间。
        for item in all_findings:
            candidate: dict[str, object] = {
                "unit_key": item.unit_key,
                "severity": item.severity.value,
                "category": item.category.value,
                "location": _compact_context_location(item.location),
                "title": _bounded_context_text(
                    item.title,
                    _MAX_CONTEXT_FINDING_TITLE_CHARS,
                ),
                "evidence": _bounded_context_text(
                    item.evidence,
                    _MAX_CONTEXT_FINDING_TEXT_CHARS,
                ),
                "impact": _bounded_context_text(
                    item.impact,
                    _MAX_CONTEXT_FINDING_TEXT_CHARS,
                ),
                "suggestion": _bounded_context_text(
                    item.suggestion,
                    _MAX_CONTEXT_FINDING_TEXT_CHARS,
                ),
                "confidence": item.confidence,
                "rule_reference": (
                    _bounded_context_text(
                        item.rule_reference,
                        _MAX_CONTEXT_FINDING_RULE_CHARS,
                    )
                    if item.rule_reference is not None
                    else None
                ),
            }
            findings_payload.append(candidate)
            if len(_encode_context_payload(payload).encode("utf-8")) > (
                _MAX_EXECUTION_CONTEXT_BYTES
            ):
                findings_payload.pop()
                break
        if len(findings_payload) < len(all_findings):
            payload["findings_truncated"] = True
        encoded = _encode_context_payload(payload)
        # 标记字段本身也占少量空间；若它让边界超出，逐条回退即可，不能
        # 直接清空全部候选。固定字段已设有上限，下面的兜底只处理异常输入。
        while (
            len(encoded.encode("utf-8")) > _MAX_EXECUTION_CONTEXT_BYTES
            and findings_payload
        ):
            findings_payload.pop()
            payload["findings_truncated"] = True
            encoded = _encode_context_payload(payload)
        if len(encoded.encode("utf-8")) > _MAX_EXECUTION_CONTEXT_BYTES:
            payload["summary"] = _bounded_context_text(
                output.summary if output is not None else None,
                32,
            )
            payload["checked_areas"] = []
            encoded = _encode_context_payload(payload)
        context.append(encoded)
    return tuple(context)


def _bounded_context_text(value: str | None, max_chars: int) -> str:
    """按字符上限压缩内部候选文本；输入来自已校验的模型输出。"""

    if value is None:
        return ""
    return value[:max_chars]


def _compact_context_location(location: object) -> dict[str, object] | None:
    """保留汇总去重所需的位置元数据，避免把长 symbol/file 带入上下文。"""

    if location is None:
        return None
    file = getattr(location, "file", None)
    start_line = getattr(location, "start_line", None)
    end_line = getattr(location, "end_line", None)
    side = getattr(location, "side", None)
    symbol = getattr(location, "symbol", None)
    return {
        "file": _bounded_context_text(
            file if isinstance(file, str) else None,
            _MAX_CONTEXT_FINDING_FILE_CHARS,
        ),
        "start_line": start_line,
        "end_line": end_line,
        "side": getattr(side, "value", side),
        "symbol": _bounded_context_text(
            symbol if isinstance(symbol, str) else None,
            _MAX_CONTEXT_FINDING_SYMBOL_CHARS,
        )
        if symbol is not None
        else None,
    }


def _encode_context_payload(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
