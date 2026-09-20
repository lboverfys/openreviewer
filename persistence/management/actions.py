"""审查管理 actions 存储职责。"""

import json
from datetime import datetime, timedelta
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from domain.enums import ExecutionStatus, FindingAdjudicationStatus, ReviewAgent
from domain.identifiers import build_review_version_key, normalize_sha
from domain.repository_policy import RepositoryPolicySnapshot
from domain.workflow import (
    WorkflowAction,
    WorkflowTransitionError,
    next_automatic_stage,
    transition,
)
from persistence.management.common import _review_change_token
from persistence.management.context import ManagementStorage
from persistence.management.retry import (
    _paused_execution_status,
    _prepare_failed_node_retry,
    _prepare_stage_retry,
    _resumed_execution_status,
)
from persistence.models import (
    OutboxEventRecord,
    PullRequestVersionRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewProfileRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.repository_policy import repository_policy_snapshot
from persistence.resource_scope import resource_predicate
from services.rbac import ResourceScope
from services.review_management import (
    ReviewAction,
    ReviewActionConflictError,
    ReviewManagementPersistenceError,
    ReviewNotFoundError,
)


def apply_action(
    self: ManagementStorage,
    review_run_id: str,
    action: ReviewAction,
    *,
    actor: str,
    request_id: str,
    target_stage: str | None = None,
    retry_scope: str | None = None,
    agent: str | None = None,
    batch_number: int | None = None,
    state_version: str | None = None,
    head_sha: str | None = None,
    capture_model_outputs: bool = False,
    review_profile_id: str | None = None,
    scope: ResourceScope | None = None,
) -> tuple[str, str, ExecutionStatus]:
    """在一个短事务内执行加速、重试、取消或重新审查。"""

    normalized_request_id = request_id.strip()
    if capture_model_outputs and action is not ReviewAction.REVIEW_SNAPSHOT:
        raise ReviewActionConflictError("输出证据留存只适用于显式历史版本评测试跑")
    if review_profile_id is not None and action is not ReviewAction.REVIEW_SNAPSHOT:
        raise ReviewActionConflictError("候选方案只适用于历史版本评测试跑")
    if not normalized_request_id:
        raise ReviewActionConflictError("操作幂等键不能为空")
    allowed_retry_scopes = {"failed_node", "stage", "new_review"}
    if retry_scope is not None and retry_scope not in allowed_retry_scopes:
        raise ReviewActionConflictError("重试范围无效")
    if batch_number is not None and batch_number < 1:
        raise ReviewActionConflictError("批次号必须为正数")
    if agent is not None and agent not in {
        ReviewAgent.SECURITY.value,
        ReviewAgent.CONVENTION.value,
        ReviewAgent.LOGIC.value,
        ReviewAgent.SUMMARY.value,
    }:
        raise ReviewActionConflictError("Agent 标识无效")
    # ``retry_scope`` 是动作的子语义，不能被任意动作静默忽略。保留
    # RETRY/RERUN 的无范围形式兼容旧客户端，同时拒绝把阶段重试或新建
    # 审查误路由到另一条处理分支。
    retry_scope_by_action: dict[ReviewAction, frozenset[str | None]] = {
        ReviewAction.RETRY_FAILED_NODE: frozenset({None, "failed_node"}),
        ReviewAction.RETRY: frozenset({None, "failed_node"}),
        ReviewAction.RETRY_STAGE: frozenset({None, "stage"}),
        ReviewAction.NEW_REVIEW: frozenset({None, "new_review"}),
        ReviewAction.RERUN: frozenset({None, "new_review"}),
    }
    allowed_scopes = retry_scope_by_action.get(action, frozenset({None}))
    if retry_scope not in allowed_scopes:
        raise ReviewActionConflictError("重试范围与当前操作不匹配")
    failed_node_action = action is ReviewAction.RETRY_FAILED_NODE or (
        action is ReviewAction.RETRY and retry_scope == "failed_node"
    )
    new_review_action = action is ReviewAction.NEW_REVIEW or (
        action is ReviewAction.RERUN and retry_scope == "new_review"
    )
    if target_stage is not None and action not in {
        ReviewAction.RESUME,
        ReviewAction.RETRY_STAGE,
    }:
        raise ReviewActionConflictError("当前操作不接受目标阶段")
    if (agent is not None or batch_number is not None) and not failed_node_action:
        raise ReviewActionConflictError("Agent 和批次号只适用于失败节点重试")
    if batch_number is not None and agent is None:
        raise ReviewActionConflictError("指定批次时必须同时提供 Agent")
    if new_review_action and head_sha is None:
        raise ReviewActionConflictError("新建最新提交审查必须提供当前 head_sha")
    action_key = sha256(
        f"{review_run_id}:{action.value}:{normalized_request_id}".encode()
    ).hexdigest()
    event_key = f"review.action:{review_run_id}:{action.value}:{action_key}"
    with self._sessions() as session:
        try:
            idempotent_result = _existing_action_result(
                session,
                review_run_id,
                action,
                event_key=event_key,
                target_stage=target_stage,
                retry_scope=retry_scope,
                agent=agent,
                batch_number=batch_number,
                capture_model_outputs=capture_model_outputs,
                review_profile_id=review_profile_id,
                scope=scope,
            )
            if idempotent_result is not None:
                return idempotent_result
            # PostgreSQL 不允许 FOR UPDATE 锁定外连接的可空一侧。
            # 运行记录和任务记录是必需的，先用内连接一起锁定；计划记录
            # 是可选的，单独查询并锁定，既保留并发保护也兼容没有计划的早期阶段。
            row = session.execute(
                select(ReviewRunRecord, ReviewTaskRecord)
                .join(
                    ReviewTaskRecord,
                    ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
                )
                .where(
                    ReviewRunRecord.id == review_run_id,
                    resource_predicate(
                        scope,
                        installation_column=ReviewRunRecord.installation_id,
                        repository_column=ReviewRunRecord.repository,
                        repository_key_column=ReviewRunRecord.repository_key,
                    ),
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                raise ReviewNotFoundError("审查任务不存在")
            run, task = row
            if (
                action in {ReviewAction.APPROVE, ReviewAction.REJECT}
                and run.repository_policy is not None
            ):
                policy = RepositoryPolicySnapshot.model_validate(run.repository_policy)
                if policy.approver and actor != policy.approver and scope is not None:
                    raise ReviewActionConflictError("请由仓库指定的审批负责人处理")
            # 第一次事件查询和业务行加锁之间可能有并发请求已经提交；
            # 锁定后必须再次检查，避免重复插入唯一 event_key 并误报 503。
            idempotent_result = _existing_action_result(
                session,
                review_run_id,
                action,
                event_key=event_key,
                target_stage=target_stage,
                retry_scope=retry_scope,
                agent=agent,
                batch_number=batch_number,
                capture_model_outputs=capture_model_outputs,
                review_profile_id=review_profile_id,
                scope=scope,
            )
            if idempotent_result is not None:
                return idempotent_result
            if head_sha is not None:
                try:
                    normalized_head_sha = normalize_sha(head_sha)
                except ValueError as exc:
                    raise ReviewActionConflictError("head_sha 格式无效") from exc
                if normalized_head_sha != run.head_sha:
                    # 新建最新提交审查允许传入新的 SHA；其他动作必须
                    # 绑定详情页读取时的当前版本，防止旧页面覆盖新结果。
                    if not new_review_action:
                        raise ReviewActionConflictError("审查版本已变化，请刷新后重试")
            else:
                normalized_head_sha = run.head_sha
            # 停止意图针对这条任务，而不是某一次心跳/批次进度快照。
            # 已锁定任务并核对 head；下方仍校验当前节点，不能停止终态或发布过程。
            if state_version is not None and action not in {ReviewAction.PAUSE, ReviewAction.CANCEL}:
                latest_event_id = session.scalar(
                    select(OutboxEventRecord.id)
                    .where(OutboxEventRecord.aggregate_id == review_run_id)
                    .order_by(
                        OutboxEventRecord.occurred_at.desc(),
                        OutboxEventRecord.id.desc(),
                    )
                    .limit(1)
                )
                current_version = _review_change_token(
                    run.updated_at,
                    task.updated_at,
                    latest_event_id,
                )
                if state_version != current_version:
                    raise ReviewActionConflictError("审查状态已变化，请刷新后重试")
            plan = session.scalar(
                select(ReviewPlanRecord)
                .where(ReviewPlanRecord.review_run_id == review_run_id)
                .with_for_update()
            )
            now = self._clock()
            current = ExecutionStatus(run.execution_status)
            workflow_current = ExecutionStatus(
                run.workflow_status or run.execution_status
            )
            if action in {ReviewAction.RETRY, ReviewAction.RETRY_FAILED_NODE, ReviewAction.RETRY_STAGE, ReviewAction.RESUME} and not run.snapshot_review:
                newer = session.scalar(select(ReviewRunRecord.id).where(
                    ReviewRunRecord.installation_id == run.installation_id,
                    ReviewRunRecord.repository_id == run.repository_id,
                    ReviewRunRecord.pull_request_number == run.pull_request_number,
                    ReviewRunRecord.head_sha != run.head_sha,
                    ReviewRunRecord.snapshot_review.is_(False),
                    ReviewRunRecord.created_at >= run.created_at,
                ).limit(1))
                if newer is not None or run.coverage_status == "stale":
                    saved_retry = session.execute(select(PullRequestVersionRecord.files_complete,
                        PullRequestVersionRecord.diff_complete).where(
                        PullRequestVersionRecord.review_version_key == run.review_version_key)).one_or_none()
                    if saved_retry is None or not saved_retry.files_complete or not saved_retry.diff_complete:
                        raise ReviewActionConflictError("旧版本代码未完整保存，请使用检查最新提交")
                    run.snapshot_review = True
                    run.coverage_status = "partial"
                    run.approval_requested_at = run.approval_due_at = None
                    run.approval_assignee = None
                    run.publish_attempt_token = None

            if run.snapshot_review and target_stage == ExecutionStatus.CI.value:
                raise ReviewActionConflictError("历史版本复查不包含 CI，请从规划或 AI 步骤重试")

            if failed_node_action:
                _prepare_failed_node_retry(
                    session,
                    run,
                    task,
                    plan,
                    agent=agent,
                    batch_number=batch_number,
                    now=now,
                )
                # 人工请求和任务重置必须在同一事务留下模型语义事件。
                # Worker 尚未重新领取前，详情页也能明确区分“已请求”与
                # “已经开始”，并可按目标 Agent/批次展示准确的重试范围。
                session.add(
                    OutboxEventRecord(
                        id=str(self._uuid_factory()),
                        event_key=f"{event_key}:model",
                        aggregate_type="review_run",
                        aggregate_id=review_run_id,
                        event_type="review.model.retry_requested",
                        payload={
                            "action": action.value,
                            "retry_scope": "failed_node",
                            "actor": actor,
                            "agent": agent,
                            "batch_number": batch_number,
                            "head_sha": run.head_sha,
                            "previous_status": current.value,
                            "new_status": ExecutionStatus.READY_FOR_REVIEW.value,
                            # 模型代次在下一次 claim 时才原子递增；这里
                            # 提前记录目标代次，便于事件流按代次归组。
                            "model_attempt_count": task.model_attempt_count + 1,
                        },
                        occurred_at=now,
                        publish_attempts=0,
                    )
                )
                session.add(
                    OutboxEventRecord(
                        id=str(self._uuid_factory()),
                        event_key=event_key,
                        aggregate_type="review_run",
                        aggregate_id=review_run_id,
                        event_type="review.manual.retry_failed_node",
                        payload={
                            "action": action.value,
                            "retry_scope": "failed_node",
                            "actor": actor,
                            "agent": agent,
                            "batch_number": batch_number,
                            "head_sha": run.head_sha,
                            "previous_status": current.value,
                            "new_status": ExecutionStatus.READY_FOR_REVIEW.value,
                        },
                        occurred_at=now,
                        publish_attempts=0,
                    )
                )
                session.commit()
                return run.id, task.id, ExecutionStatus.READY_FOR_REVIEW

            # 新版固定 DAG 的人工节点独立保存在 workflow_status，旧队列
            # 仍按 execution_status 领取任务，因此升级期间两套状态可以并行。
            workflow_actions = {
                ReviewAction.START,
                ReviewAction.PAUSE,
                ReviewAction.RESUME,
                ReviewAction.RETRY_STAGE,
                ReviewAction.APPROVE,
                ReviewAction.REJECT,
            }
            if action in workflow_actions:
                try:
                    target = (
                        ExecutionStatus(target_stage)
                        if target_stage is not None
                        else None
                    )
                    if action is ReviewAction.RESUME and target is None:
                        # 优先使用暂停时持久化的节点；旧数据没有该字段时由
                        # 领域层使用兼容默认 CI，并仍会校验目标是否合法。
                        target = (
                            ExecutionStatus(task.workflow_paused_from)
                            if task.workflow_paused_from
                            else None
                        )
                    result = transition(
                        workflow_current,
                        WorkflowAction(action.value),
                        target_stage=target,
                    )
                except (TypeError, ValueError, WorkflowTransitionError) as exc:
                    raise ReviewActionConflictError(str(exc)) from exc
                final_workflow_status = result.after
                automatic_occurred_at: datetime | None = None
                if action is ReviewAction.APPROVE:
                    if run.coverage_status in {"partial", "stale"}:
                        raise ReviewActionConflictError(
                            "当前审查覆盖不完整，完成失败节点后才能批准"
                        )
                    unreviewed_count = int(
                        session.scalar(
                            select(func.count())
                            .select_from(ReviewFindingRecord)
                            .where(
                                ReviewFindingRecord.review_run_id == review_run_id,
                                ReviewFindingRecord.adjudication_status
                                == FindingAdjudicationStatus.UNREVIEWED.value,
                            )
                        )
                        or 0
                    )
                    if unreviewed_count:
                        raise ReviewActionConflictError(
                            f"仍有 {unreviewed_count} 个候选问题未完成人工裁决"
                        )
                    automatic_status = next_automatic_stage(result.after)
                    if automatic_status is None:
                        raise ReviewActionConflictError("批准后的工作流状态无效")
                    final_workflow_status = automatic_status
                    automatic_occurred_at = now + timedelta(microseconds=1)
                task.workflow_status = final_workflow_status.value
                run.workflow_status = final_workflow_status.value
                task.updated_at = automatic_occurred_at or now
                run.updated_at = automatic_occurred_at or now
                if action is ReviewAction.PAUSE:
                    # 先撤销租约，再让旧 Worker 的所有权检查失败；这一步和
                    # workflow_status=paused 在同一事务中提交，避免后台结果把
                    # 人工暂停覆盖掉。
                    task.workflow_paused_from = workflow_current.value
                    run.workflow_paused_from = workflow_current.value
                    task.lease_owner = None
                    task.lease_expires_at = None
                    task.claimed_from_status = None
                    task.available_at = now
                    task.execution_status = _paused_execution_status(
                        workflow_current,
                        task.execution_status,
                    ).value
                    run.execution_status = task.execution_status
                elif action is ReviewAction.RESUME:
                    # 资源预算已从生产执行链路移除。迁移未覆盖的历史任务
                    # 可能仍带有旧错误码/计划标记，恢复时一并清空，不能
                    # 再生成“追加预算”事件或把旧限制带入下一次领取。
                    if task.last_error_code == "model_budget_exceeded":
                        task.last_error = None
                        task.last_error_code = None
                        task.last_error_retryable = None
                        task.last_error_details = None
                    if plan is not None:
                        plan.model_budget_exhausted_reason = None
                        plan.model_budget_exhausted_at = None
                    task.workflow_paused_from = None
                    run.workflow_paused_from = None
                    task.lease_owner = None
                    task.lease_expires_at = None
                    task.claimed_from_status = None
                    task.available_at = now
                    task.execution_status = _resumed_execution_status(
                        result.after,
                        task.execution_status,
                    ).value
                    run.execution_status = task.execution_status
                elif action is ReviewAction.RETRY_STAGE:
                    if target is None:
                        target = result.after
                    _prepare_stage_retry(
                        session,
                        run,
                        task,
                        plan,
                        target,
                    )
                    task.workflow_paused_from = None
                    run.workflow_paused_from = None
                    task.execution_status = (
                        ExecutionStatus.QUEUED.value
                        if result.after is ExecutionStatus.CI
                        else ExecutionStatus.READY_FOR_REVIEW.value
                    )
                    run.execution_status = task.execution_status
                    task.available_at = now
                    task.last_error = None
                    task.last_error_code = None
                    task.last_error_retryable = None
                    task.last_error_details = None
                    task.lease_owner = None
                    task.lease_expires_at = None
                    task.claimed_from_status = None
                else:
                    task.workflow_paused_from = None
                    run.workflow_paused_from = None
                execution_status = ExecutionStatus(task.execution_status)
                session.add(
                    OutboxEventRecord(
                        id=str(self._uuid_factory()),
                        event_key=event_key,
                        aggregate_type="review_run",
                        aggregate_id=review_run_id,
                        event_type=f"review.workflow.{action.value}",
                        payload={
                            "action": action.value,
                            "actor": actor,
                            "previous_status": workflow_current.value,
                            "new_status": result.after.value,
                            "target_stage": target_stage,
                            "retry_scope": retry_scope,
                            "agent": agent,
                            "batch_number": batch_number,
                            "paused_from": (
                                workflow_current.value
                                if action is ReviewAction.PAUSE
                                else None
                            ),
                        },
                        occurred_at=now,
                        publish_attempts=0,
                    )
                )
                if action is ReviewAction.APPROVE:
                    session.add(
                        OutboxEventRecord(
                            id=str(self._uuid_factory()),
                            event_key=f"{event_key}:advance",
                            aggregate_type="review_run",
                            aggregate_id=review_run_id,
                            event_type="review.workflow.advance",
                            payload={
                                "action": "advance",
                                "actor": "workflow",
                                "previous_status": result.after.value,
                                "new_status": final_workflow_status.value,
                                "reason": "approval_recorded",
                                "target_stage": None,
                            },
                            occurred_at=automatic_occurred_at or now,
                            publish_attempts=0,
                        )
                    )
                session.commit()
                return run.id, task.id, execution_status

            if action in {ReviewAction.RERUN, ReviewAction.REVIEW_SNAPSHOT} or new_review_action:
                if current is ExecutionStatus.RUNNING:
                    raise ReviewActionConflictError("任务正在处理中，暂时不能重新审查")
                snapshot_review = action is ReviewAction.REVIEW_SNAPSHOT or (run.snapshot_review and not new_review_action)
                if snapshot_review:
                    saved = session.execute(select(
                        PullRequestVersionRecord.files_complete,
                        PullRequestVersionRecord.diff_complete,
                        PullRequestVersionRecord.context_fetched_at,
                    ).where(PullRequestVersionRecord.review_version_key == run.review_version_key).limit(1)).one_or_none()
                    if saved is None or not saved.files_complete or not saved.diff_complete or saved.context_fetched_at is None:
                        raise ReviewActionConflictError("尚未保存完整代码，无法复查历史版本")
                initial_status = ExecutionStatus.READY_FOR_REVIEW if snapshot_review else ExecutionStatus.QUEUED
                initial_workflow = ExecutionStatus.PLANNING if snapshot_review else ExecutionStatus.QUEUED
                new_run_id = str(self._uuid_factory())
                new_task_id = str(self._uuid_factory())
                new_review_version_key = build_review_version_key(
                    run.repository_id,
                    run.pull_request_number,
                    normalized_head_sha,
                )
                # 幂等键可能达到 API 允许的 200 字符；直接拼接后截断会让
                # 只在尾部不同的两个键发生碰撞。新记录使用固定长度摘要，
                # 同时回读旧版明文键，保证发布新版后重试仍然幂等。
                rerun_key = (
                    f"{'manual-snapshot' if snapshot_review else 'manual-rerun'}:{review_run_id}:"
                    f"{sha256(normalized_request_id.encode('utf-8')).hexdigest()}"
                )
                legacy_rerun_key = rerun_key if snapshot_review else (f"manual-rerun:{review_run_id}:{request_id}")[:200]
                request_fingerprint = sha256(json.dumps([
                    run.request_fingerprint, normalized_head_sha, review_profile_id, capture_model_outputs,
                ], separators=(",", ":")).encode()).hexdigest()
                existing_rerun = session.scalar(
                    select(ReviewRunRecord.id).where(
                        ReviewRunRecord.idempotency_key.in_(
                            (rerun_key, legacy_rerun_key)
                        )
                    )
                )
                if existing_rerun is not None:
                    previous = session.execute(select(ReviewRunRecord.capture_model_outputs, ReviewRunRecord.request_fingerprint)
                        .where(ReviewRunRecord.id == existing_rerun)).one()
                    if previous.capture_model_outputs != capture_model_outputs:
                        raise ReviewActionConflictError("同一幂等键不能用于不同的评测输出留存选项")
                    legacy_fingerprint = sha256(f"{run.request_fingerprint}:{normalized_head_sha}".encode()).hexdigest()
                    if previous.request_fingerprint != request_fingerprint and not (review_profile_id is None and previous.request_fingerprint == legacy_fingerprint):
                        raise ReviewActionConflictError("同一幂等键不能用于不同的试跑方案")
                    existing_task = session.scalar(
                        select(ReviewTaskRecord.id).where(
                            ReviewTaskRecord.review_run_id == existing_rerun
                        )
                    )
                    if existing_task is None:
                        raise ReviewManagementPersistenceError("重新审查任务记录不完整")
                    return existing_rerun, existing_task, initial_status
                effective_policy = repository_policy_snapshot(session, run.repository)
                if review_profile_id is not None:
                    profile = session.scalar(select(ReviewProfileRecord.id).where(
                        ReviewProfileRecord.id == review_profile_id,
                        ReviewProfileRecord.repository_key == run.repository_key,
                    ))
                    if profile is None or effective_policy is None:
                        raise ReviewNotFoundError("试跑方案不存在或不属于当前仓库")
                    effective_policy = {**effective_policy, "review_profile_id": profile}
                if snapshot_review and effective_policy is not None:
                    # 显式复查要产生新的模型结果，保留权限/预算，关闭本次的结果复用。
                    effective_policy = {**effective_policy, "incremental_review": False}
                session.add_all(
                    [
                        ReviewRunRecord(
                            id=new_run_id,
                            review_version_key=new_review_version_key,
                            installation_id=run.installation_id,
                            repository_id=run.repository_id,
                            repository=run.repository,
                            repository_policy=effective_policy,
                            pull_request_number=run.pull_request_number,
                            head_sha=normalized_head_sha,
                            snapshot_review=snapshot_review,
                            capture_model_outputs=capture_model_outputs,
                            execution_status=initial_status.value,
                            workflow_status=initial_workflow.value,
                            review_conclusion=None,
                            coverage_status="unknown",
                            idempotency_key=rerun_key,
                            request_fingerprint=request_fingerprint,
                            created_at=now,
                            updated_at=now,
                        ),
                        ReviewTaskRecord(
                            id=new_task_id,
                            review_run_id=new_run_id,
                            execution_status=initial_status.value,
                            workflow_status=initial_workflow.value,
                            priority=task.priority,
                            attempt_count=0,
                            model_attempt_count=0,
                            max_attempts=task.max_attempts,
                            available_at=now,
                            created_at=now,
                            updated_at=now,
                        ),
                        OutboxEventRecord(
                            id=str(self._uuid_factory()),
                            event_key=f"review.requested:{new_run_id}",
                            aggregate_type="review_run",
                            aggregate_id=new_run_id,
                            event_type="review.requested",
                            payload={
                                "review_run_id": new_run_id,
                                "review_task_id": new_task_id,
                                "review_version_key": new_review_version_key,
                                "head_sha": normalized_head_sha,
                                "source_review_run_id": review_run_id,
                                "trigger": "snapshot_review" if snapshot_review else "manual_rerun",
                            },
                            occurred_at=now,
                            publish_attempts=0,
                        ),
                    ]
                )
                session.add(
                    OutboxEventRecord(
                        id=str(self._uuid_factory()),
                        event_key=event_key,
                        aggregate_type="review_run",
                        aggregate_id=review_run_id,
                        event_type=(
                            "review.manual.new_review_requested"
                            if new_review_action
                            else "review.manual.rerun_requested"
                        ),
                        payload={
                            "action": action.value,
                            "actor": actor,
                            "new_review_run_id": new_run_id,
                            "new_review_task_id": new_task_id,
                            "snapshot_review": snapshot_review,
                            "capture_model_outputs": capture_model_outputs,
                            "review_profile_id": review_profile_id,
                            "head_sha": normalized_head_sha,
                            "retry_scope": (
                                "new_review" if new_review_action else retry_scope
                            ),
                            "agent": agent,
                            "batch_number": batch_number,
                        },
                        occurred_at=now,
                        publish_attempts=0,
                    )
                )
                session.commit()
                return new_run_id, new_task_id, initial_status

            if action is ReviewAction.EXPEDITE:
                if current not in {
                    ExecutionStatus.QUEUED,
                    ExecutionStatus.WAITING_FOR_CI,
                    ExecutionStatus.READY_FOR_REVIEW,
                }:
                    raise ReviewActionConflictError("当前状态不能加速")
                task.available_at = now
                task.updated_at = now
                new_status = current
            elif action is ReviewAction.CANCEL:
                if current not in {
                    ExecutionStatus.QUEUED,
                    ExecutionStatus.WAITING_FOR_CI,
                    ExecutionStatus.READY_FOR_REVIEW,
                    ExecutionStatus.RUNNING,
                } and not (current is ExecutionStatus.COMPLETED and workflow_current is ExecutionStatus.PAUSED):
                    raise ReviewActionConflictError("当前状态不能取消")
                task.execution_status = ExecutionStatus.CANCELLED.value
                task.lease_owner = None
                task.lease_expires_at = None
                task.claimed_from_status = None
                task.workflow_paused_from = None
                run.workflow_paused_from = None
                run.publish_attempt_token = None
                task.updated_at = now
                run.execution_status = ExecutionStatus.CANCELLED.value
                task.workflow_status = ExecutionStatus.CANCELLED.value
                run.workflow_status = ExecutionStatus.CANCELLED.value
                run.updated_at = now
                new_status = ExecutionStatus.CANCELLED
            elif action is ReviewAction.RETRY:
                if current not in {
                    ExecutionStatus.FAILED,
                    ExecutionStatus.TIMED_OUT,
                }:
                    raise ReviewActionConflictError("只有失败或超时任务可以重试")
                if plan is not None and plan.model_review_completed_at is not None:
                    raise ReviewActionConflictError(
                        "该任务已经生成审查结果，请使用重新审查"
                    )
                if plan is not None:
                    # 通用重试会把任务/模型尝试次数归零；旧批次若仍保留
                    # FAILED/attempt_count，Worker 会在领取前直接判定达到
                    # 单批上限，导致用户重试也永远无法恢复。保留不可变的
                    # Review Plan，只清理模型阶段可重建的产物。
                    _prepare_stage_retry(
                        session,
                        run,
                        task,
                        plan,
                        ExecutionStatus.AGENT_BATCHES,
                    )
                new_status = (
                    ExecutionStatus.READY_FOR_REVIEW
                    if plan is not None or run.snapshot_review
                    else ExecutionStatus.QUEUED
                )
                task.execution_status = new_status.value
                task.attempt_count = 0
                task.model_attempt_count = 0
                task.ci_poll_count = 0
                task.available_at = now
                task.last_error = None
                task.last_error_code = None
                task.last_error_retryable = None
                task.last_error_details = None
                task.lease_owner = None
                task.lease_expires_at = None
                task.claimed_from_status = None
                task.updated_at = now
                run.execution_status = new_status.value
                workflow_retry_status = (
                    ExecutionStatus.AGENT_BATCHES
                    if plan is not None
                    else ExecutionStatus.PLANNING if run.snapshot_review
                    else ExecutionStatus.QUEUED
                )
                task.workflow_status = workflow_retry_status.value
                run.workflow_status = workflow_retry_status.value
                run.updated_at = now
            else:
                raise ReviewActionConflictError("不支持的任务操作")

            session.add(
                OutboxEventRecord(
                    id=str(self._uuid_factory()),
                    event_key=event_key,
                    aggregate_type="review_run",
                    aggregate_id=review_run_id,
                    event_type=f"review.manual.{action.value}",
                    payload={
                        "action": action.value,
                        "actor": actor,
                        "previous_status": current.value,
                        "new_status": new_status.value,
                    },
                    occurred_at=now,
                    publish_attempts=0,
                )
            )
            session.commit()
            return run.id, task.id, new_status
        except (ReviewNotFoundError, ReviewActionConflictError):
            session.rollback()
            raise
        except IntegrityError as exc:
            session.rollback()
            raise ReviewManagementPersistenceError(
                "review action could not be reconciled"
            ) from exc
        except SQLAlchemyError as exc:
            session.rollback()
            raise ReviewManagementPersistenceError(
                "review action could not be persisted"
            ) from exc


def _existing_action_result(
    session: Session,
    review_run_id: str,
    action: ReviewAction,
    *,
    event_key: str,
    target_stage: str | None,
    retry_scope: str | None = None,
    agent: str | None = None,
    batch_number: int | None = None,
    capture_model_outputs: bool = False,
    review_profile_id: str | None = None,
    scope: ResourceScope | None = None,
) -> tuple[str, str, ExecutionStatus] | None:
    """读取已提交的人工动作，供加锁前后两次幂等检查复用。"""

    # 先验证资源范围，再读取幂等事件；否则越权请求可以通过已存在的
    # event_key 得到“成功”响应或冲突信息，间接确认别的仓库存在任务。
    scoped_run = session.scalar(
        select(ReviewRunRecord.id)
        .where(
            ReviewRunRecord.id == review_run_id,
            resource_predicate(
                scope,
                installation_column=ReviewRunRecord.installation_id,
                repository_column=ReviewRunRecord.repository,
                repository_key_column=ReviewRunRecord.repository_key,
            ),
        )
        .limit(1)
    )
    if scoped_run is None:
        raise ReviewNotFoundError("审查任务不存在")
    existing_event = session.execute(
        select(OutboxEventRecord.payload).where(
            OutboxEventRecord.event_key == event_key
        )
    ).scalar_one_or_none()
    if existing_event is None:
        return None
    if bool(existing_event.get("capture_model_outputs", False)) != capture_model_outputs:
        raise ReviewActionConflictError("同一幂等键不能用于不同的评测输出留存选项")
    if existing_event.get("review_profile_id") != review_profile_id:
        raise ReviewActionConflictError("同一幂等键不能用于不同的试跑方案")
    if (
        not isinstance(existing_event, dict)
        or existing_event.get("target_stage") != target_stage
    ):
        raise ReviewActionConflictError("同一幂等键不能用于不同的目标阶段")
    for key, expected in (
        ("retry_scope", retry_scope),
        ("agent", agent),
        ("batch_number", batch_number),
    ):
        if existing_event.get(key) != expected:
            raise ReviewActionConflictError("同一幂等键不能用于不同的重试目标")
    if (
        action in {ReviewAction.RERUN, ReviewAction.NEW_REVIEW, ReviewAction.REVIEW_SNAPSHOT}
        and isinstance(existing_event, dict)
        and isinstance(existing_event.get("new_review_run_id"), str)
        and isinstance(existing_event.get("new_review_task_id"), str)
    ):
        new_review_run_id = existing_event["new_review_run_id"]
        new_review_task_id = existing_event["new_review_task_id"]
        if not isinstance(new_review_run_id, str) or not isinstance(
            new_review_task_id,
            str,
        ):
            raise ReviewActionConflictError("重跑事件中的任务标识无效")
        return (
            new_review_run_id,
            new_review_task_id,
            ExecutionStatus.READY_FOR_REVIEW if existing_event.get("snapshot_review") else ExecutionStatus.QUEUED,
        )
    existing = session.execute(
        select(
            ReviewTaskRecord.id,
            ReviewRunRecord.id,
            ReviewRunRecord.execution_status,
        )
        .join(
            ReviewRunRecord,
            ReviewRunRecord.id == ReviewTaskRecord.review_run_id,
        )
        .where(
            ReviewRunRecord.id == review_run_id,
            resource_predicate(
                scope,
                installation_column=ReviewRunRecord.installation_id,
                repository_column=ReviewRunRecord.repository,
                repository_key_column=ReviewRunRecord.repository_key,
            ),
        )
    ).one_or_none()
    if existing is None:
        raise ReviewNotFoundError("审查任务不存在")
    return existing[1], existing[0], ExecutionStatus(existing[2])
