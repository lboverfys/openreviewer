"""results 阶段的有界事务与数据访问。"""

from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, insert, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Load, Session

from domain.enums import (
    CoverageStatus,
    EvidenceVerificationStatus,
    ExecutionStatus,
    FindingAdjudicationStatus,
    FindingLifecycleState,
    FindingOccurrenceStatus,
    ReviewConclusion,
    ReviewFileDecision,
)
from domain.model_review import (
    MaterializedFinding,
    ModelReviewInput,
    ModelReviewResult,
    materialize_findings,
)
from persistence.models import (
    FindingLifecycleRecord,
    ModelCallRecord,
    ReviewFilePlanRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
)
from persistence.queue.common import (
    _add_event,
    _locked_owned_task_with_run,
    _set_owned_status,
    _set_workflow_status,
)
from persistence.queue.context import QueueStorage
from services.task_queue import (
    ModelReviewConflictError,
    ModelReviewInputError,
    ReviewTaskLease,
    StoredModelReview,
    TaskLeaseLostError,
    TaskQueueError,
)


def _reconcile_finding_lifecycles(
    session: Session,
    run: ReviewRunRecord,
    findings: tuple[MaterializedFinding, ...],
    now: datetime,
    *,
    coverage_complete: bool,
) -> tuple[
    dict[str, tuple[FindingOccurrenceStatus, int, str | None]],
    int,
]:
    """批量判定当前 Finding，并在完整覆盖时消解上一轮遗留问题。"""

    fingerprints = tuple(sorted(item.finding.fingerprint for item in findings))
    predicates = [
        FindingLifecycleRecord.state == FindingLifecycleState.PRESENT.value,
        FindingLifecycleRecord.fixed_by_review_run_id == run.id,
    ]
    if fingerprints:
        predicates.append(FindingLifecycleRecord.fingerprint.in_(fingerprints))
    lifecycle_rows = list(
        session.scalars(
            select(FindingLifecycleRecord)
            .where(
                FindingLifecycleRecord.repository_id == run.repository_id,
                FindingLifecycleRecord.pull_request_number == run.pull_request_number,
                or_(*predicates),
            )
            .limit(401)
            .with_for_update()
        )
    )
    # 每轮最多 200 个 Finding；上一轮 present 集合也最多 200 个。超过
    # 400 说明持久化状态已违反边界，不能继续做不完整的生命周期判定。
    if len(lifecycle_rows) > 400:
        raise ModelReviewConflictError("Finding 生命周期集合超过安全上限")
    lifecycles = {row.fingerprint: row for row in lifecycle_rows}
    occurrence_by_fingerprint: dict[
        str, tuple[FindingOccurrenceStatus, int, str | None]
    ] = {}

    for fingerprint in fingerprints:
        lifecycle = lifecycles.get(fingerprint)
        if lifecycle is None:
            lifecycle = FindingLifecycleRecord(
                repository_id=run.repository_id,
                pull_request_number=run.pull_request_number,
                fingerprint=fingerprint,
                state=FindingLifecycleState.PRESENT.value,
                first_seen_review_run_id=run.id,
                last_seen_review_run_id=run.id,
                previous_seen_review_run_id=None,
                fixed_by_review_run_id=None,
                first_seen_head_sha=run.head_sha,
                last_seen_head_sha=run.head_sha,
                last_occurrence_status=FindingOccurrenceStatus.NEW.value,
                occurrence_count=1,
                first_seen_at=now,
                last_seen_at=now,
                fixed_at=None,
                historical_backfilled_at=now,
                updated_at=now,
            )
            session.add(lifecycle)
            lifecycles[fingerprint] = lifecycle
            status = FindingOccurrenceStatus.NEW
        elif lifecycle.last_seen_review_run_id == run.id:
            # 阶段级重审会删除并重建本轮 Finding。复用已有判定，避免同一
            # review_run 被重复计数或错误标成再次出现。
            status = FindingOccurrenceStatus(lifecycle.last_occurrence_status)
            lifecycle.state = FindingLifecycleState.PRESENT.value
            lifecycle.fixed_by_review_run_id = None
            lifecycle.fixed_at = None
            lifecycle.updated_at = now
        else:
            status = (
                FindingOccurrenceStatus.REINTRODUCED
                if lifecycle.state == FindingLifecycleState.FIXED.value
                else FindingOccurrenceStatus.STILL_PRESENT
            )
            lifecycle.previous_seen_review_run_id = lifecycle.last_seen_review_run_id
            lifecycle.last_seen_review_run_id = run.id
            lifecycle.last_seen_head_sha = run.head_sha
            lifecycle.last_occurrence_status = status.value
            lifecycle.occurrence_count += 1
            lifecycle.last_seen_at = now
            lifecycle.state = FindingLifecycleState.PRESENT.value
            lifecycle.fixed_by_review_run_id = None
            lifecycle.fixed_at = None
            lifecycle.updated_at = now
        occurrence_by_fingerprint[fingerprint] = (
            status,
            lifecycle.occurrence_count,
            lifecycle.previous_seen_review_run_id,
        )

    if coverage_complete:
        current = set(fingerprints)
        for lifecycle in lifecycle_rows:
            if (
                lifecycle.fingerprint not in current
                and lifecycle.state == FindingLifecycleState.PRESENT.value
            ):
                lifecycle.state = FindingLifecycleState.FIXED.value
                lifecycle.fixed_by_review_run_id = run.id
                lifecycle.fixed_at = now
                lifecycle.updated_at = now

    fixed_count = sum(
        lifecycle.state == FindingLifecycleState.FIXED.value
        and lifecycle.fixed_by_review_run_id == run.id
        for lifecycle in lifecycles.values()
    )
    return occurrence_by_fingerprint, fixed_count


def store_model_review(
    self: QueueStorage,
    lease: ReviewTaskLease,
    review_input: ModelReviewInput,
    result: ModelReviewResult,
    findings: tuple[MaterializedFinding, ...],
    *,
    configuration_revision: int | None = None,
    partial: bool = False,
) -> StoredModelReview:
    """短事务保存模型结果。

    ``partial`` 用于固定 Agent DAG：成功节点的 Finding 先落库，但不把
    Review Plan 标记为完成。后续完整重试会复用同一 ``model_call`` 并只
    插入尚未存在的指纹，避免重复结果和重复计费。
    """

    if (
        lease.claimed_from_status is not ExecutionStatus.READY_FOR_REVIEW
        or lease.review_plan_id is None
        or lease.review_plan_id != review_input.review_plan_id
        or lease.review_run_id != review_input.review_run_id
    ):
        raise ModelReviewConflictError("模型结果不属于当前租约的 Review Plan")
    try:
        expected_findings = materialize_findings(review_input, result.output)
    except ValueError as exc:
        raise ModelReviewConflictError(
            "模型 Finding 引用了计划外的 Unit、文件或规则"
        ) from exc
    if len(findings) != len(expected_findings):
        raise ModelReviewConflictError("模型 Finding 没有按平台契约完成身份补齐")
    for actual, expected in zip(findings, expected_findings, strict=True):
        if actual.source_unit_key != expected.source_unit_key:
            raise ModelReviewConflictError("模型 Finding 没有按平台契约完成身份补齐")
        actual_finding = actual.finding.model_copy(
            update={
                "evidence_verification_status": (
                    expected.finding.evidence_verification_status
                ),
                "evidence_verification_reason": (
                    expected.finding.evidence_verification_reason
                ),
            }
        )
        if actual_finding != expected.finding:
            raise ModelReviewConflictError("模型 Finding 没有按平台契约完成身份补齐")
    if not review_input.units and result.status.value != "skipped":
        raise ModelReviewConflictError("空 Review Plan 不应调用模型")
    if review_input.units and result.status.value != "succeeded":
        raise ModelReviewConflictError("非空 Review Plan 缺少成功模型调用")
    if partial and not review_input.units:
        raise ModelReviewConflictError("空 Review Plan 不应保存部分结果")

    now = self._clock()
    with self._sessions() as session:
        try:
            existing = session.execute(
                select(
                    ModelCallRecord.id,
                    ModelCallRecord.provider,
                    ModelCallRecord.model,
                    ModelCallRecord.request_fingerprint,
                    ModelCallRecord.finding_count,
                    ReviewPlanRecord.model_review_completed_at,
                    ReviewRunRecord.execution_status,
                )
                .join(
                    ReviewPlanRecord,
                    ReviewPlanRecord.id == ModelCallRecord.review_plan_id,
                )
                .join(
                    ReviewRunRecord,
                    ReviewRunRecord.id == ReviewPlanRecord.review_run_id,
                )
                .where(ModelCallRecord.review_plan_id == review_input.review_plan_id)
                # 该表对 review_plan_id 有唯一约束；LIMIT 仍作为数据损坏
                # 或旧迁移不完整时的有界保护，避免详情请求无界读取。
                .limit(1)
            ).one_or_none()
            if existing is not None and existing.model_review_completed_at is not None:
                if (
                    existing.provider != result.provider.value
                    or existing.model != result.model
                    or existing.request_fingerprint != result.request_fingerprint
                ):
                    raise ModelReviewConflictError(
                        "同一 Review Plan 已保存不同模型请求"
                    )
                return StoredModelReview(
                    model_call_id=existing.id,
                    created=False,
                    finding_count=existing.finding_count,
                    execution_status=ExecutionStatus(existing.execution_status),
                    coverage_status=CoverageStatus.COMPLETE.value,
                    partial=False,
                )

            # 未完成的 partial 快照也必须经过当前租约所有权检查。旧实现
            # 在这里直接返回，导致调用方的任务仍停留在 RUNNING，租约无法
            # 释放，后续 Worker 只能等待超时恢复。
            task, run = _locked_owned_task_with_run(session, lease, now)
            if existing is not None:
                same_partial = (
                    partial
                    and existing.model_review_completed_at is None
                    and existing.request_fingerprint == result.request_fingerprint
                )
                if same_partial:
                    if (
                        existing.provider != result.provider.value
                        or existing.model != result.model
                    ):
                        raise ModelReviewConflictError(
                            "同一 Review Plan 已保存不同模型请求"
                        )
                    # 结果快照已经写入；本次重入不再插入 Finding，但要把
                    # 当前任务收口为可重试的 partial 状态并清除租约。
                    _set_owned_status(task, run, ExecutionStatus.FAILED, now)
                    _set_workflow_status(
                        task,
                        run,
                        ExecutionStatus.AGENT_BATCHES,
                        now,
                    )
                    run.coverage_status = CoverageStatus.PARTIAL.value
                    run.review_conclusion = (
                        ReviewConclusion.FINDINGS_PRESENT.value
                        if existing.finding_count
                        else ReviewConclusion.NO_CONFIRMED_FINDINGS.value
                    )
                    session.commit()
                    return StoredModelReview(
                        model_call_id=existing.id,
                        created=False,
                        finding_count=existing.finding_count,
                        execution_status=ExecutionStatus.FAILED,
                        coverage_status=CoverageStatus.PARTIAL.value,
                        partial=True,
                    )

            plan = session.scalar(
                select(ReviewPlanRecord)
                .where(
                    ReviewPlanRecord.id == review_input.review_plan_id,
                    ReviewPlanRecord.review_run_id == run.id,
                )
                .options(
                    Load(ReviewPlanRecord).load_only(
                        ReviewPlanRecord.id,
                        ReviewPlanRecord.review_run_id,
                        ReviewPlanRecord.review_version_key,
                        ReviewPlanRecord.head_sha,
                        ReviewPlanRecord.plan_fingerprint,
                        ReviewPlanRecord.rules_complete,
                        ReviewPlanRecord.model_review_completed_at,
                        raiseload=True,
                    )
                )
                .with_for_update()
            )
            if plan is None:
                raise ModelReviewInputError("当前任务没有对应 Review Plan")
            if plan.model_review_completed_at is not None:
                raise ModelReviewConflictError("Review Plan 模型阶段已经完成")
            if (
                run.review_version_key != review_input.review_version_key
                or run.repository_id != review_input.repository_id
                or run.repository != review_input.repository
                or run.pull_request_number != review_input.pull_request_number
                or run.head_sha != review_input.head_sha
                or plan.review_version_key != review_input.review_version_key
                or plan.head_sha != review_input.head_sha
                or plan.plan_fingerprint != review_input.plan_fingerprint
            ):
                raise ModelReviewConflictError("模型结果与被锁定任务的计划身份不一致")

            newer_run_id = session.scalar(
                select(ReviewRunRecord.id)
                .where(
                    ReviewRunRecord.installation_id == run.installation_id,
                    ReviewRunRecord.repository_id == run.repository_id,
                    ReviewRunRecord.pull_request_number == run.pull_request_number,
                    ReviewRunRecord.id != run.id,
                    ReviewRunRecord.head_sha != run.head_sha,
                    ReviewRunRecord.snapshot_review.is_(False),
                    ReviewRunRecord.created_at >= run.created_at,
                )
                .order_by(ReviewRunRecord.created_at.desc())
                .limit(1)
            )
            if newer_run_id is not None and not run.snapshot_review:
                # 模型已完成时保留报告，转为历史结果；不写当前 PR 的问题状态。
                run.snapshot_review = True
                run.publish_attempt_token = None
                _add_event(self, session, task, "review.saved_as_history",
                    f"model-head-changed:{task.model_attempt_count}", now)

            incomplete_file_count = session.scalar(
                select(func.count())
                .select_from(ReviewFilePlanRecord)
                .where(
                    ReviewFilePlanRecord.review_plan_id == plan.id,
                    ReviewFilePlanRecord.decision != ReviewFileDecision.PLANNED.value,
                )
            )
            coverage_complete = bool(
                plan.rules_complete and not incomplete_file_count and not partial
            )
            lifecycle_occurrences: dict[str, tuple[FindingOccurrenceStatus, int, str | None]]
            if run.snapshot_review:
                # 历史检查保留自己的问题，不改变当前 PR 的缺陷生命周期。
                lifecycle_occurrences = {
                    item.finding.fingerprint: (FindingOccurrenceStatus.NEW, 1, None) for item in findings
                }
                fixed_finding_count = 0
            else:
                lifecycle_occurrences, fixed_finding_count = _reconcile_finding_lifecycles(
                    session, run, findings, now, coverage_complete=coverage_complete,
                )
            model_call_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"openreviewer:model-call:{plan.id}:{result.request_fingerprint}",
                )
            )
            # 计划只有一个兼容的 ModelCall 行。部分结果已经存在时复用
            # 该行并更新为本次聚合快照，避免唯一键冲突；完整结果随后
            # 可以在同一行上完成收口。
            model_call = (
                session.scalar(
                    select(ModelCallRecord)
                    .where(ModelCallRecord.id == existing.id)
                    .with_for_update()
                )
                if existing is not None
                else None
            )
            if model_call is None:
                model_call = ModelCallRecord(
                    id=model_call_id,
                    review_plan_id=plan.id,
                    configuration_revision=configuration_revision,
                    provider=result.provider.value,
                    api_protocol=result.api_protocol.value,
                    model=result.model,
                    status=result.status.value,
                    prompt_version=result.prompt_version,
                    request_fingerprint=result.request_fingerprint,
                    provider_response_id=result.provider_response_id,
                    provider_request_id=result.provider_request_id,
                    response_status=result.response_status,
                    duration_ms=result.duration_ms,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    cache_read_input_tokens=result.usage.cache_read_input_tokens,
                    cache_write_input_tokens=result.usage.cache_write_input_tokens,
                    reasoning_output_tokens=result.usage.reasoning_output_tokens,
                    estimated_cost_microusd=result.estimated_cost_microusd,
                    finding_count=0,
                    created_at=now,
                )
                session.add(model_call)
            else:
                model_call_id = model_call.id
                model_call.configuration_revision = configuration_revision
                model_call.provider = result.provider.value
                model_call.api_protocol = result.api_protocol.value
                model_call.model = result.model
                model_call.status = result.status.value
                model_call.prompt_version = result.prompt_version
                model_call.request_fingerprint = result.request_fingerprint
                model_call.provider_response_id = result.provider_response_id
                model_call.provider_request_id = result.provider_request_id
                model_call.response_status = result.response_status
                model_call.duration_ms = result.duration_ms
                model_call.input_tokens = result.usage.input_tokens
                model_call.output_tokens = result.usage.output_tokens
                model_call.cache_read_input_tokens = (
                    result.usage.cache_read_input_tokens
                )
                model_call.cache_write_input_tokens = (
                    result.usage.cache_write_input_tokens
                )
                model_call.reasoning_output_tokens = (
                    result.usage.reasoning_output_tokens
                )
                model_call.estimated_cost_microusd = result.estimated_cost_microusd
            session.flush()
            existing_fingerprints = set(
                session.scalars(
                    select(ReviewFindingRecord.fingerprint).where(
                        ReviewFindingRecord.review_run_id == run.id
                    )
                )
            )
            new_findings = tuple(
                item
                for item in findings
                if item.finding.fingerprint not in existing_fingerprints
            )
            finding_rows: list[dict[str, object]] = []
            for item in new_findings:
                finding = item.finding
                evidence_status = (
                    finding.evidence_verification_status
                    or EvidenceVerificationStatus.UNVERIFIED
                )
                location = finding.location
                lifecycle_status, occurrence_count, previous_run_id = (
                    lifecycle_occurrences[finding.fingerprint]
                )
                if finding.head_sha != review_input.head_sha or (
                    finding.verification_status.value == "verified"
                    and (location is None or not location.in_diff)
                ):
                    raise ModelReviewConflictError(
                        "模型 Finding 的 SHA 或机器定位状态无效"
                    )
                finding_rows.append(
                    {
                        "id": str(
                            uuid5(
                                NAMESPACE_URL,
                                f"openreviewer:finding:{run.id}:{finding.fingerprint}",
                            )
                        ),
                        "review_run_id": run.id,
                        "review_plan_id": plan.id,
                        "model_call_id": model_call_id,
                        "source_unit_key": item.source_unit_key,
                        "fingerprint": finding.fingerprint,
                        "head_sha": finding.head_sha,
                        "severity": finding.severity.value,
                        "category": finding.category.value,
                        "location_file": location.file if location else None,
                        "location_blob_sha": (location.blob_sha if location else None),
                        "location_start_line": (
                            location.start_line if location else None
                        ),
                        "location_end_line": (location.end_line if location else None),
                        "location_side": (location.side.value if location else None),
                        "location_in_diff": (location.in_diff if location else False),
                        "location_symbol": (location.symbol if location else None),
                        "title": finding.title,
                        "evidence": finding.evidence,
                        "impact": finding.impact,
                        "suggestion": finding.suggestion,
                        "required_test": finding.required_test,
                        "confidence": finding.confidence,
                        "verification_status": (finding.verification_status.value),
                        "evidence_verification_status": (evidence_status.value),
                        "evidence_verification_reason": (
                            finding.evidence_verification_reason
                        ),
                        "evidence_verified_at": (
                            now
                            if evidence_status is EvidenceVerificationStatus.VERIFIED
                            else None
                        ),
                        "adjudication_status": (
                            FindingAdjudicationStatus.UNREVIEWED.value
                        ),
                        "lifecycle_status": lifecycle_status.value,
                        "occurrence_count": occurrence_count,
                        "previous_review_run_id": previous_run_id,
                        "lifecycle_backfilled_at": now,
                        "rule_reference": finding.rule_reference,
                        "context_references": list(finding.context_references),
                        "created_at": now,
                    }
                )
            if finding_rows:
                session.execute(insert(ReviewFindingRecord), finding_rows)

            total_finding_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(ReviewFindingRecord)
                    .where(ReviewFindingRecord.review_run_id == run.id)
                )
                or 0
            )
            model_call.finding_count = total_finding_count

            run.review_conclusion = (
                ReviewConclusion.FINDINGS_PRESENT.value
                if total_finding_count
                else ReviewConclusion.NO_CONFIRMED_FINDINGS.value
            )
            run.coverage_status = (
                CoverageStatus.PARTIAL.value
                if partial or not coverage_complete
                else CoverageStatus.COMPLETE.value
            )
            if partial:
                # 兼容旧队列的 failed 状态，同时把真实 DAG 停在可重试的
                # Agent 节点。成功批次/Finding 已保存，租约被安全释放。
                _set_owned_status(task, run, ExecutionStatus.FAILED, now)
                _set_workflow_status(
                    task,
                    run,
                    ExecutionStatus.AGENT_BATCHES,
                    now,
                )
                event_type = "review.model.partial"
            else:
                plan.model_review_completed_at = now
                _set_owned_status(
                    task,
                    run,
                    ExecutionStatus.COMPLETED,
                    now,
                )
                # 旧 execution_status 保持 completed 以兼容现有队列；新的
                # 工作流必须停在人工批准门，不得把结果误显示为已发布。
                _set_workflow_status(
                    task,
                    run,
                    ExecutionStatus.COMPLETED if run.snapshot_review else ExecutionStatus.AWAITING_APPROVAL,
                    now,
                )
                event_type = "review.model.completed"
                approval_policy = review_input.repository_policy
                run.approval_assignee = (
                    approval_policy.approver.casefold()
                    if not run.snapshot_review and approval_policy and approval_policy.approver
                    else None
                )
                run.approval_requested_at = None if run.snapshot_review else now
                run.approval_due_at = None if run.snapshot_review else now + timedelta(
                    hours=approval_policy.approval_timeout_hours
                    if approval_policy
                    else 24
                )
            _add_event(
                self,
                session,
                task,
                event_type,
                result.request_fingerprint,
                now,
                extra_payload={
                    "review_plan_id": plan.id,
                    "model_call_id": model_call_id,
                    "provider": result.provider.value,
                    "model": result.model,
                    "model_call_status": result.status.value,
                    "finding_count": total_finding_count,
                    "new_finding_count": sum(
                        status is FindingOccurrenceStatus.NEW
                        for status, _count, _previous in lifecycle_occurrences.values()
                    ),
                    "partial": partial,
                    "coverage_status": run.coverage_status,
                    "still_present_finding_count": sum(
                        status is FindingOccurrenceStatus.STILL_PRESENT
                        for status, _count, _previous in lifecycle_occurrences.values()
                    ),
                    "reintroduced_finding_count": sum(
                        status is FindingOccurrenceStatus.REINTRODUCED
                        for status, _count, _previous in lifecycle_occurrences.values()
                    ),
                    "fixed_finding_count": fixed_finding_count,
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "cache_read_input_tokens": (result.usage.cache_read_input_tokens),
                    "cache_write_input_tokens": (result.usage.cache_write_input_tokens),
                    "estimated_cost_microusd": (result.estimated_cost_microusd),
                },
            )
            session.commit()
            return StoredModelReview(
                model_call_id=model_call_id,
                created=existing is None,
                finding_count=total_finding_count,
                execution_status=(
                    ExecutionStatus.FAILED if partial else ExecutionStatus.COMPLETED
                ),
                coverage_status=run.coverage_status,
                partial=partial,
            )
        except (
            ModelReviewConflictError,
            ModelReviewInputError,
            TaskLeaseLostError,
        ):
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError("model review could not be persisted") from exc
