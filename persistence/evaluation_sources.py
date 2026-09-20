"""从现有审查表批量提取可追溯快照，不触发 GitHub 或模型请求。"""

import json
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from domain.evaluation_workbench import (
    MAX_EVALUATION_FINDINGS,
    EvaluationChange,
    EvaluationFinding,
    EvaluationNotFoundError,
    EvaluationSource,
    ModelVersion,
    OutputEvidenceSummary,
    RetrievalVersion,
)
from domain.security import redact_sensitive, redact_text
from persistence.evaluation_outputs import output_summary_query
from persistence.models import (
    CodeIndexRecord,
    ModelCallRecord,
    ModelReviewBatchRecord,
    PullRequestVersionRecord,
    RetrievalTraceRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewPlanRuleRecord,
    ReviewProfileRecord,
    ReviewRunRecord,
    ReviewUnitRecord,
)
from persistence.resource_scope import resource_predicate
from services.model_review import StructuredReviewPromptBuilder
from services.rbac import ResourceScope

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_CAPTURE_BYTES = 8 * 1024 * 1024


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def capture_review_sources(
    session: Session, run_ids: tuple[str, ...], scope: ResourceScope, captured_at: datetime,
) -> dict[str, EvaluationSource]:
    """最多 30 个运行用六条批量查询收录，正文流式读取并限制总字节。"""
    if not run_ids or len(run_ids) > 30 or len(set(run_ids)) != len(run_ids):
        raise ValueError("每次请选择 1 至 30 条不同的审查运行")
    outputs = output_summary_query(run_ids, captured_at)
    statement = (
        select(
            ReviewRunRecord.id, ReviewRunRecord.installation_id,
            ReviewRunRecord.repository_id, ReviewRunRecord.repository,
            ReviewRunRecord.repository_key, ReviewRunRecord.pull_request_number,
            ReviewRunRecord.head_sha, ReviewRunRecord.coverage_status,
            ReviewRunRecord.repository_policy, ReviewRunRecord.created_at,
            ReviewRunRecord.capture_model_outputs,
            outputs.c.request_count.label("output_request_count"),
            outputs.c.captured_count.label("output_captured_count"),
            outputs.c.expires_at.label("output_expires_at"),
            ReviewPlanRecord.id.label("plan_id"), ReviewPlanRecord.plan_fingerprint,
            ReviewPlanRecord.unit_count,
            ReviewPlanRecord.planner_version, ReviewPlanRecord.model_review_completed_at,
            ModelCallRecord.configuration_revision, ModelCallRecord.status,
            ModelCallRecord.provider, ModelCallRecord.api_protocol, ModelCallRecord.model,
            ModelCallRecord.prompt_version, ModelCallRecord.input_tokens,
            ModelCallRecord.output_tokens, ModelCallRecord.cache_read_input_tokens,
            ModelCallRecord.cache_write_input_tokens, ModelCallRecord.duration_ms,
            ModelCallRecord.estimated_cost_microusd, ModelCallRecord.finding_count,
            PullRequestVersionRecord.title.label("pr_title"),
            ReviewProfileRecord.fingerprint.label("profile_fingerprint"),
            ReviewProfileRecord.snapshot["agents"].label("profile_agents"),
            ReviewProfileRecord.snapshot["prompt"].label("profile_prompt"),
            ReviewProfileRecord.summary["knowledge_versions"].label("profile_knowledge_versions"),
        )
        .select_from(ReviewRunRecord)
        .outerjoin(outputs, outputs.c.review_run_id == ReviewRunRecord.id)
        .outerjoin(ReviewPlanRecord, ReviewPlanRecord.review_run_id == ReviewRunRecord.id)
        .outerjoin(ModelCallRecord, ModelCallRecord.review_plan_id == ReviewPlanRecord.id)
        .outerjoin(PullRequestVersionRecord,
                   PullRequestVersionRecord.id == ReviewPlanRecord.pull_request_version_id)
        .outerjoin(ReviewProfileRecord, and_(
            ReviewProfileRecord.id == ReviewRunRecord.repository_policy["review_profile_id"].as_string(),
            ReviewProfileRecord.repository_key == ReviewRunRecord.repository_key,
        ))
        .where(
            ReviewRunRecord.id.in_(run_ids),
            resource_predicate(
                scope, installation_column=ReviewRunRecord.installation_id,
                repository_column=ReviewRunRecord.repository,
                repository_key_column=ReviewRunRecord.repository_key,
            ),
        ).order_by(ReviewRunRecord.id).limit(31)
        # 原任务的重试/清理也会锁运行行；短共享锁确保跨表快照一致。
        .with_for_update(read=True, of=ReviewRunRecord)
    )
    headers = {row.id: row for row in session.execute(statement)}
    if set(headers) != set(run_ids):
        raise EvaluationNotFoundError("部分审查运行不存在或无权访问")
    for row in headers.values():
        if (
            row.plan_id is None or row.model_review_completed_at is None
            or row.status != "succeeded" or row.coverage_status != "complete"
        ):
            raise ValueError("只能收录已完成 AI 审查且覆盖完整的运行")
        if row.unit_count > 500:
            raise ValueError("评测快照最多保存 500 个审查文件，请选择更小的 PR")
    plan_ids = tuple(row.plan_id for row in headers.values())
    plan_runs = {row.plan_id: row.id for row in headers.values()}
    findings: dict[str, list[EvaluationFinding]] = defaultdict(list)
    size = 0
    finding_rows = session.execute(select(
        ReviewFindingRecord.review_run_id, ReviewFindingRecord.id,
        ReviewFindingRecord.fingerprint, ReviewFindingRecord.title,
        ReviewFindingRecord.severity, ReviewFindingRecord.category,
        ReviewFindingRecord.location_file.label("file"),
        ReviewFindingRecord.location_start_line.label("start_line"),
        ReviewFindingRecord.location_end_line.label("end_line"),
        ReviewFindingRecord.evidence, ReviewFindingRecord.impact,
        ReviewFindingRecord.suggestion, ReviewFindingRecord.confidence,
        ReviewFindingRecord.verification_status.label("location_status"),
        ReviewFindingRecord.evidence_verification_status.label("evidence_status"),
        ReviewFindingRecord.evidence_verification_reason.label("evidence_reason"),
        ReviewFindingRecord.context_references,
    ).where(ReviewFindingRecord.review_run_id.in_(run_ids))
      .order_by(ReviewFindingRecord.review_run_id, ReviewFindingRecord.created_at, ReviewFindingRecord.id)
      .limit(len(run_ids) * (MAX_EVALUATION_FINDINGS + 1))
      .execution_options(yield_per=100)).mappings()
    for finding_row in finding_rows:
        run_id = finding_row["review_run_id"]
        payload = {key: value for key, value in finding_row.items() if key != "review_run_id"}
        finding = EvaluationFinding.model_validate(redact_sensitive(payload))
        size += len(finding.model_dump_json().encode())
        if size > MAX_CAPTURE_BYTES or len(findings[run_id]) >= MAX_EVALUATION_FINDINGS:
            raise ValueError("评测快照过大，请减少本次收录数量或选择更小的 PR")
        findings[run_id].append(finding)
    changes: dict[str, list[EvaluationChange]] = defaultdict(list)
    for row in session.execute(select(
        ReviewUnitRecord.review_plan_id, ReviewUnitRecord.file,
        ReviewUnitRecord.blob_sha, ReviewUnitRecord.patch,
    ).where(ReviewUnitRecord.review_plan_id.in_(plan_ids))
      .order_by(ReviewUnitRecord.review_plan_id, ReviewUnitRecord.ordinal)
      .limit(len(run_ids) * 501)
      .execution_options(yield_per=50)):
        run_id = plan_runs[row.review_plan_id]
        change = EvaluationChange(
            file=redact_text(row.file), blob_sha=row.blob_sha, patch=redact_text(row.patch),
        )
        size += len(change.model_dump_json().encode())
        if size > MAX_CAPTURE_BYTES or len(changes[run_id]) >= 500:
            raise ValueError("变更代码超过评测快照边界，请减少收录数量或选择更小的 PR")
        changes[run_id].append(change)
    versions: dict[str, list[ModelVersion]] = defaultdict(list)
    batch_rows = session.execute(select(
        ModelReviewBatchRecord.review_plan_id, ModelReviewBatchRecord.agent,
        ModelReviewBatchRecord.result["provider"].as_string().label("provider"),
        ModelReviewBatchRecord.result["api_protocol"].as_string().label("protocol"),
        ModelReviewBatchRecord.result["model"].as_string().label("model"),
        ModelReviewBatchRecord.result["prompt_version"].as_string().label("prompt_version"),
        ModelReviewBatchRecord.result["provenance"].as_string().label("provenance"),
        ModelReviewBatchRecord.result["reused_from_run_id"].as_string().label("reused_from_run_id"),
    ).where(
        ModelReviewBatchRecord.review_plan_id.in_(plan_ids),
        ModelReviewBatchRecord.status == "succeeded",
    ).distinct().limit(len(run_ids) * 65 + 1))
    for row in batch_rows:
        context = json.loads(row.provenance) if row.provenance else {}
        run_id = plan_runs[row.review_plan_id]
        if len(versions[run_id]) >= 64:
            raise ValueError("运行包含过多模型配置版本，无法作为稳定评测样本")
        versions[run_id].append(ModelVersion(
            agent=row.agent, provider=row.provider or "unknown",
            protocol=row.protocol or "unknown", model=row.model or "unknown",
            prompt_version=row.prompt_version or "unknown",
            prompt_protocol_version=context.get("prompt_protocol_version"),
            prompt_content_sha256=context.get("prompt_content_sha256"),
            application_revision=context.get("application_revision"),
            knowledge_versions=context.get("knowledge_versions") or {},
            context_recorded=bool(context) and context.get("knowledge_versions") is not None,
            reused_from_run_id=row.reused_from_run_id,
        ))
    rules: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in session.execute(select(
        ReviewPlanRuleRecord.review_plan_id, ReviewPlanRuleRecord.path,
        ReviewPlanRuleRecord.content_sha256,
    ).where(ReviewPlanRuleRecord.review_plan_id.in_(plan_ids))
      .order_by(ReviewPlanRuleRecord.review_plan_id, ReviewPlanRuleRecord.ordinal)
      .limit(len(run_ids) * 257 + 1)):
        run_id = plan_runs[row.review_plan_id]
        if len(rules[run_id]) >= 256:
            raise ValueError("规则快照超出评测边界")
        rules[run_id].append({"source": row.path, "sha256": row.content_sha256})
    retrieval: dict[str, list[RetrievalVersion]] = defaultdict(list)
    for row in session.execute(select(
        RetrievalTraceRecord.review_run_id, RetrievalTraceRecord.agent,
        RetrievalTraceRecord.index_id, CodeIndexRecord.head_sha,
        CodeIndexRecord.embedding_model,
        RetrievalTraceRecord.payload["strategy"].as_string().label("strategy"),
    ).join(CodeIndexRecord, CodeIndexRecord.id == RetrievalTraceRecord.index_id)
      .join(ReviewPlanRecord, and_(
          ReviewPlanRecord.review_run_id == RetrievalTraceRecord.review_run_id,
          ReviewPlanRecord.plan_fingerprint == RetrievalTraceRecord.plan_fingerprint,
      ))
      .where(RetrievalTraceRecord.review_run_id.in_(run_ids),
             RetrievalTraceRecord.agent.is_not(None))
      .order_by(RetrievalTraceRecord.id).limit(len(run_ids) * 17 + 1)):
        if len(retrieval[row.review_run_id]) >= 16:
            raise ValueError("检索版本快照超出评测边界")
        retrieval[row.review_run_id].append(RetrievalVersion(
            agent=row.agent, index_id=row.index_id, index_head_sha=row.head_sha,
            embedding_model=row.embedding_model, strategy=row.strategy,
        ))
    sources: dict[str, EvaluationSource] = {}
    total_bytes = 0
    for run_id, row in headers.items():
        if len(findings[run_id]) != row.finding_count:
            raise ValueError("审查结果快照不完整，请刷新任务后重试")
        if len(changes[run_id]) != row.unit_count:
            raise ValueError("审查代码快照不完整，请刷新任务后重试")
        if not versions[run_id]:
            versions[run_id] = [ModelVersion(
                agent="workflow", provider=row.provider, protocol=row.api_protocol,
                model=row.model, prompt_version=row.prompt_version,
            )]
        completed_at = _utc(row.model_review_completed_at)
        turnaround = int((completed_at - _utc(row.created_at)).total_seconds() * 1000)
        if turnaround < 0:
            raise ValueError("审查时间记录不完整，无法计算耗时")
        limitations = []
        if any(version.reused_from_run_id for version in versions[run_id]):
            limitations.append("含跨提交复用结果，不属于独立模型调用对照；需要关闭增量复用后重新收录")
        if any(not version.context_recorded or version.application_revision is None for version in versions[run_id]):
            limitations.append("历史批次未记录完整的程序版本或知识引用版本")
        if len({version.application_revision for version in versions[run_id]}) > 1:
            limitations.append("同一运行的批次混用了程序版本，不能用于严格方案验收")
        if row.estimated_cost_microusd is None:
            limitations.append("未配置完整价格，估算费用未知")
        if row.capture_model_outputs and (not row.output_request_count or row.output_captured_count != row.output_request_count):
            limitations.append("指定评测调用的输出捕获尚未完整，不能声称原始输出证据齐全")
        declared_agents = row.profile_agents or {}
        declared_knowledge = row.profile_knowledge_versions or {}
        declared_prompt_version = StructuredReviewPromptBuilder(row.profile_prompt).version if row.profile_prompt else None
        calls_consistent = bool(row.profile_fingerprint) and all(
            version.agent in declared_agents
            and all(getattr(version, key) == declared_agents[version.agent].get(field)
                    for key, field in (("provider", "provider"), ("model", "model")))
            and version.protocol == (declared_agents[version.agent].get("api_protocol")
                or ("responses" if version.provider == "openai" else "messages"))
            and version.prompt_version == declared_prompt_version
            and (not (row.profile_prompt or {}).get("content_sha256")
                 or version.prompt_content_sha256 == row.profile_prompt["content_sha256"])
            and all(declared_knowledge.get(source) == version_hash
                    for source, version_hash in version.knowledge_versions.items())
            for version in versions[run_id]
        )
        if (row.repository_policy or {}).get("review_profile_id") and not calls_consistent:
            limitations.append("实际模型、协议或知识版本与绑定方案不一致，或方案来源缺失")
        source = EvaluationSource(
            review_run_id=run_id, installation_id=row.installation_id,
            repository_id=row.repository_id, repository=row.repository,
            repository_key=row.repository_key, pull_request_number=row.pull_request_number,
            head_sha=row.head_sha, title=redact_text(row.pr_title or f"PR #{row.pull_request_number}")[:300],
            plan_fingerprint=row.plan_fingerprint, planner_version=row.planner_version,
            configuration_revision=row.configuration_revision, repository_policy=row.repository_policy,
            profile_fingerprint=row.profile_fingerprint, profile_calls_consistent=calls_consistent,
            model_output_evidence=OutputEvidenceSummary(
                capture_requested=row.capture_model_outputs, request_count=row.output_request_count or 0,
                captured_count=row.output_captured_count or 0,
                incomplete_count=(row.output_request_count or 0) - (row.output_captured_count or 0),
                expires_at=row.output_expires_at,
            ),
            rule_versions=tuple(rules[run_id]),
            models=tuple(sorted(versions[run_id], key=lambda item: (
                item.agent, item.model, item.prompt_version, item.protocol,
                item.application_revision or "", json.dumps(item.knowledge_versions, sort_keys=True),
            ))),
            retrieval=tuple(retrieval[run_id]), findings=tuple(findings[run_id]),
            changes=tuple(changes[run_id]),
            input_tokens=row.input_tokens + row.cache_read_input_tokens + row.cache_write_input_tokens,
            output_tokens=row.output_tokens, model_duration_ms=row.duration_ms,
            turnaround_ms=turnaround, estimated_cost_microusd=row.estimated_cost_microusd,
            completed_at=completed_at, captured_at=captured_at, limitations=tuple(limitations),
        )
        source_bytes = len(source.model_dump_json().encode())
        total_bytes += source_bytes
        if source_bytes > MAX_SOURCE_BYTES or total_bytes > MAX_CAPTURE_BYTES:
            raise ValueError("评测快照过大，请减少本次收录数量或选择更小的 PR")
        sources[run_id] = source
    return sources
