"""固定多 Agent 编排、知识快照与结果收口。"""

from __future__ import annotations

from pathlib import PurePosixPath
from threading import Event
from typing import TYPE_CHECKING

from apps.worker.batches import _PersistentBatchedReviewer
from apps.worker.heartbeat import _LeaseCursor, _raise_if_lease_lost
from apps.worker.results import _agent_conclusion_payload, _workflow_result
from domain.enums import ExecutionStatus, ReviewAgent
from domain.model_review import materialize_findings
from domain.repository_policy import RepositoryRequestLimitError
from domain.security import SafeApplicationError
from services.ai_settings import ActiveAiRuntime
from services.model_budget import model_budget_scope
from services.model_review import ModelReviewer
from services.rag import merge_review_citations
from services.task_queue import TaskQueueError

if TYPE_CHECKING:
    from apps.worker.runtime import WorkerRuntime


def _run_fixed_agent_workflow(
    self: WorkerRuntime,
    cursor: _LeaseCursor,
    ai_runtime: ActiveAiRuntime,
) -> ExecutionStatus:
    """执行三路独立 Agent 的可恢复批次并写入统一结果。"""

    # 固定工作流在读取输入、构造引用和首个批次领取前可能耗时；先把
    # 普通领取租约升级为模型阶段租约，避免忙碌心跳在这段窗口内续成短租约。
    cursor.renew(self._settings.model_review_lease_duration)
    model_input = self._queue.load_model_review_input(cursor.lease)
    # MVP 以本次全部非文档变更为保守失效范围；依赖变化时不复用局部旧判断。
    model_input = model_input.model_copy(update={"reuse_dependencies": {
        unit.file: f"{unit.blob_sha}:{unit.patch_sha256}" for unit in model_input.units
        if not unit.file.lower().endswith((".md", ".txt", ".rst"))
    }})
    budget_exhausted = Event()
    request_guard = self._repository_request_guard(
        cursor, model_input, budget_exhausted
    )
    if self._retrieval_service is not None:
        self._queue.record_model_progress(
            cursor.lease, "retrieval_started", {"agent": "workflow"}, agent="workflow"
        )
        accountant_factory = getattr(self._queue, "monthly_accountant", None)
        accountant = (
            accountant_factory(lambda: cursor.lease, "retrieval")
            if accountant_factory
            else None
        )
        with model_budget_scope(accountant):
            if ai_runtime.retrieval_settings is not None:
                model_input = self._retrieval_service.review_context(
                    model_input,
                    lambda: cursor.renew(self._settings.model_review_lease_duration),
                    frozen_runtime=ai_runtime.retrieval_settings,
                )
            else:
                model_input = self._retrieval_service.review_context(
                    model_input,
                    lambda: cursor.renew(self._settings.model_review_lease_duration),
                )
        self._queue.record_model_progress(
            cursor.lease,
            "retrieval_completed",
            {"agent": "workflow", "context_count": len(model_input.context_evidence)},
            agent="workflow",
        )
    if cursor.lease.model_attempt_count > 1:
        self._queue.record_model_progress(
            cursor.lease,
            "retry_started",
            {
                "agent": "workflow",
                "model_attempt_count": cursor.lease.model_attempt_count,
                "retry_scope": "failed_node",
            },
            agent="workflow",
        )
    base_workflow = ai_runtime.agent_workflow
    if base_workflow is None:
        raise TaskQueueError("固定 Agent 工作流未配置")
    reviewers = base_workflow.reviewers
    settings_by_agent = base_workflow.agent_settings
    wrapped = {
        agent: _PersistentBatchedReviewer(
            self._queue,
            cursor,
            agent,
            reviewer,
            settings_by_agent[agent],
            self._settings.model_review_lease_duration,
            request_guard=request_guard,
        )
        for agent, reviewer in reviewers.items()
        if agent in {ReviewAgent.SECURITY, ReviewAgent.CONVENTION, ReviewAgent.LOGIC}
        and agent in settings_by_agent
    }
    summary_reviewer = base_workflow.summary_reviewer
    summary_settings = settings_by_agent.get(ReviewAgent.SUMMARY)
    wrapped_summary: ModelReviewer | None
    if summary_reviewer is not None and summary_settings is not None:
        wrapped_summary = _PersistentBatchedReviewer(
            self._queue,
            cursor,
            ReviewAgent.SUMMARY,
            summary_reviewer,
            summary_settings,
            self._settings.model_review_lease_duration,
            request_guard=request_guard,
        )
    else:
        wrapped_summary = None
    from services.agent_workflow import FixedAgentWorkflow

    workflow = FixedAgentWorkflow(
        wrapped,
        summary_reviewer=wrapped_summary,
        max_concurrency=base_workflow.max_concurrency,
    )
    reference_map: dict[ReviewAgent, tuple[str, ...]] = {
        agent: () for agent in ReviewAgent
    }
    reference_versions: dict[ReviewAgent, dict[str, str]] = {}
    if self._knowledge_base is not None:
        # 首次调用在循环外完成有界文件读取并缓存；下面四次检索只做内存匹配。
        knowledge_chunks = (
            ai_runtime.knowledge_chunks
            if ai_runtime.knowledge_chunks is not None
            else self._knowledge_base.chunks()
        )
        knowledge_chunks = self._knowledge_base.filter_active_chunks(knowledge_chunks)
        knowledge_chunks = tuple(
            chunk
            for chunk in knowledge_chunks
            if chunk.repository_scope is None
            or chunk.repository_scope == model_input.repository.casefold()
        )
        policy = model_input.repository_policy
        if policy is not None and policy.knowledge_sources is not None:
            allowed_sources = frozenset(policy.knowledge_sources)
            knowledge_chunks = tuple(
                chunk for chunk in knowledge_chunks if chunk.source in allowed_sources
            )
        # 仓库范围已过滤；完整包路径会让 niuma/java/service 等词挤掉业务主题。
        topic_query = " ".join(dict.fromkeys(
            PurePosixPath(unit.file).stem for unit in model_input.units[:32]
        ))
        topic_citations = self._knowledge_base.search(
            topic_query, limit=4, chunks=knowledge_chunks,
        )
        responsibilities = {
            ReviewAgent.SECURITY: (
                "security authorization authentication secrets 安全 鉴权 权限 密钥"
            ),
            ReviewAgent.CONVENTION: (
                "coding convention maintainability style 规范 编码 可维护性"
            ),
            ReviewAgent.LOGIC: (
                "logic reliability business database correctness 逻辑 可靠性 数据库"
            ),
            ReviewAgent.SUMMARY: (
                "evidence findings deduplication 证据 缺陷 误报 合并"
            ),
        }
        for agent, responsibility in responsibilities.items():
            responsibility_citations = self._knowledge_base.search(
                responsibility,
                limit=8,
                chunks=knowledge_chunks,
            )
            citations = merge_review_citations(topic_citations, responsibility_citations)
            reference_versions[agent] = {
                item.source: item.version for item in citations
            }
            reference_map[agent] = tuple(
                (f"{item.source}#{item.heading}@{item.version}: {item.excerpt}")[:2_000]
                for item in citations
            )
    model_input = model_input.model_copy(update={"knowledge_versions": {
        source: version for versions in reference_versions.values() for source, version in versions.items()
    }})
    execution = workflow.run(
        model_input,
        references=reference_map,
        reference_versions=reference_versions,
        on_aggregating=lambda: self._queue.mark_model_aggregating(cursor.lease),
        allow_partial_aggregation=True,
        local_aggregation=True,
        force_summary=getattr(cursor.lease, "force_summary", False),
    )
    if budget_exhausted.is_set():
        # 已完成批次保留；额度耗尽不能以部分结果进入批准或发布。
        raise RepositoryRequestLimitError()
    for agent_result in (*execution.agents, execution.summary_execution):
        if agent_result is not None and agent_result.safe_error is not None:
            if agent_result.safe_error.details.get("egress_reason"):
                raise SafeApplicationError(agent_result.safe_error)
            if (
                agent_result.safe_error.details.get("budget_reason")
                == "repository_monthly_budget"
            ):
                raise SafeApplicationError(agent_result.safe_error)
            if agent_result.safe_error.details.get("provider_channel"):
                raise SafeApplicationError(agent_result.safe_error)
    # 心跳线程可能在最后一个模型请求期间发现租约已被接管；即使编排器
    # 返回了完整结果，也不能让旧 Worker 覆盖新 Worker 的持久化结果。
    _raise_if_lease_lost(cursor)
    for item in execution.agents:
        _raise_if_lease_lost(cursor)
        phase = (
            "agent_completed"
            if item.status == "completed"
            else "agent_not_applicable"
            if item.status == "not_applicable"
            else "agent_failed"
        )
        self._queue.record_model_progress(
            cursor.lease,
            phase,
            {
                "agent": item.agent.value,
                "status": item.status,
                "duration_ms": item.duration_ms,
                "finding_count": item.finding_count,
                "applicable_unit_count": item.applicable_unit_count,
                "references": list(item.references),
                "error": item.error,
                **_agent_conclusion_payload(item),
            },
            agent=item.agent.value,
        )
    if execution.summary_execution is not None:
        item = execution.summary_execution
        _raise_if_lease_lost(cursor)
        self._queue.record_model_progress(
            cursor.lease,
            "agent_completed" if item.status == "completed" else "agent_failed",
            {
                "agent": item.agent.value,
                "status": item.status,
                "duration_ms": item.duration_ms,
                "finding_count": item.finding_count,
                "references": list(item.references),
                "error": item.error,
                **_agent_conclusion_payload(item),
            },
            agent=item.agent.value,
        )
    # 汇总事件必须区分真实调用、跳过和本地确定性合并；不能在上游
    # 失败时无条件写 ``summary_completed``。
    _raise_if_lease_lost(cursor)
    if execution.summary_status == "completed":
        summary_phase = "summary_completed"
    elif execution.summary_status == "failed":
        summary_phase = "summary_failed"
    else:
        summary_phase = "summary_skipped"
    self._queue.record_model_progress(
        cursor.lease,
        summary_phase,
        {
            "agent": "summary",
            "phase": "workflow_completed",
            "agent_status": execution.summary_status,
            "agent_count": len(execution.agents),
            "finding_count": len(execution.findings),
            "aggregation_status": execution.aggregation_status,
            "summary_status": execution.summary_status,
            **_agent_conclusion_payload(execution.summary_execution),
        },
        agent="summary",
    )
    if execution.aggregation_status in {"local", "completed"}:
        self._queue.record_model_progress(
            cursor.lease,
            "aggregation_completed",
            {
                "agent": "summary",
                "aggregation_status": execution.aggregation_status,
                "summary_status": execution.summary_status,
                "finding_count": len(execution.findings),
                "partial_result": execution.partial_result,
            },
            agent="summary",
        )
    if execution.partial_result:
        self._queue.record_model_progress(
            cursor.lease,
            "workflow_partial",
            {
                "agent": "workflow",
                "coverage_status": execution.coverage_status,
                "failed_agents": [item.value for item in execution.failed_agents],
                "failed_batches": [
                    {"agent": agent, "batch_number": number}
                    for agent, number in execution.failed_batches
                ],
                "finding_count": len(execution.findings),
                "summary_status": execution.summary_status,
            },
            agent="workflow",
        )
        # 成功 Agent 已经在批次表中落盘；这里再把可验证 Finding 写入
        # 详情读模型。失败节点仍保持可重试，不改变旧 status=failed 语义。
        try:
            combined = _workflow_result(
                model_input,
                execution,
                allow_partial=True,
            )
            findings = materialize_findings(model_input, combined.output)
            findings = self._verify_findings(cursor, model_input, findings)
            stored = self._queue.store_model_review(
                cursor.lease,
                model_input,
                combined,
                findings,
                configuration_revision=ai_runtime.revision,
                partial=True,
            )
            return stored.execution_status
        except TypeError:
            # 兼容外部注入的旧队列实现；没有 partial 参数时继续走
            # 旧错误路径，避免测试替身因签名不同而崩溃。
            pass
    if execution.status != "completed":
        failed_executions = tuple(
            item
            for item in (
                *execution.agents,
                execution.summary_execution,
            )
            if item is not None and item.safe_error is not None
        )
        if failed_executions:
            safe_error = failed_executions[0].safe_error
            if safe_error is not None:
                raise SafeApplicationError(safe_error)
        raise TaskQueueError(execution.summary)
    # FixedAgentWorkflow 的候选已做稳定去重；统一持久化接口仍负责 SHA、
    # blob 和 verification 状态补齐。
    combined = _workflow_result(
        model_input,
        execution,
        allow_partial=execution.summary_status == "failed",
    )
    _raise_if_lease_lost(cursor)
    findings = materialize_findings(model_input, combined.output)
    findings = self._verify_findings(cursor, model_input, findings)
    stored = self._queue.store_model_review(
        cursor.lease,
        model_input,
        combined,
        findings,
        configuration_revision=ai_runtime.revision,
    )
    return stored.execution_status
