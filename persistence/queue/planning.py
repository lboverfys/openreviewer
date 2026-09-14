"""planning 阶段的有界事务与数据访问。"""

from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Load

from domain.enums import ChangedFileStatus, CoverageStatus, ExecutionStatus, PatchState
from domain.github import PullRequestFile
from domain.identifiers import build_review_version_key
from domain.model_review import ModelReviewInput
from domain.review_planning import (
    DEFAULT_REVIEW_DOMAINS,
    RepositoryRule,
    RepositoryRulesSnapshot,
    ReviewPlan,
    ReviewUnit,
)
from persistence.models import (
    PullRequestFileRecord,
    PullRequestVersionRecord,
    ReviewFilePlanRecord,
    ReviewPlanRecord,
    ReviewPlanRuleRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
    ReviewUnitRecord,
)
from persistence.queue.common import (
    _add_event,
    _as_utc,
    _locked_owned_task_with_run,
    _set_owned_status,
    _set_workflow_status,
)
from persistence.queue.context import QueueStorage
from services.task_queue import (
    ModelReviewConflictError,
    ModelReviewInputError,
    ReviewPlanConflictError,
    ReviewPlanInputError,
    ReviewPlanningInput,
    ReviewTarget,
    ReviewTaskLease,
    StoredReviewPlan,
    TaskLeaseLostError,
    TaskQueueError,
)


def load_planning_input(
    self: QueueStorage, lease: ReviewTaskLease
) -> ReviewPlanningInput:
    """一次读取精确版本及其最多 3000 个 changed files。"""

    if lease.claimed_from_status is not ExecutionStatus.READY_FOR_REVIEW:
        raise ReviewPlanInputError("只有可审查阶段的任务才能读取规划输入")
    now = self._clock()
    statement = (
        select(
            ReviewRunRecord.installation_id.label("installation_id"),
            ReviewRunRecord.repository_id.label("repository_id"),
            ReviewRunRecord.repository.label("repository"),
            ReviewRunRecord.pull_request_number.label("pull_request_number"),
            ReviewRunRecord.head_sha.label("run_head_sha"),
            ReviewRunRecord.review_version_key.label("review_version_key"),
            PullRequestVersionRecord.id.label("version_id"),
            PullRequestVersionRecord.head_sha.label("version_head_sha"),
            PullRequestVersionRecord.context_fetched_at.label("context_fetched_at"),
            PullRequestVersionRecord.files_complete.label("files_complete"),
            PullRequestVersionRecord.changed_files_count.label("changed_files_count"),
            PullRequestFileRecord.id.label("file_id"),
            PullRequestFileRecord.path.label("file_path"),
            PullRequestFileRecord.previous_path.label("previous_path"),
            PullRequestFileRecord.status.label("file_status"),
            PullRequestFileRecord.blob_sha.label("blob_sha"),
            PullRequestFileRecord.additions.label("additions"),
            PullRequestFileRecord.deletions.label("deletions"),
            PullRequestFileRecord.changes.label("changes"),
            PullRequestFileRecord.patch_state.label("patch_state"),
            PullRequestFileRecord.patch.label("patch"),
        )
        .select_from(ReviewTaskRecord)
        .join(
            ReviewRunRecord,
            ReviewRunRecord.id == ReviewTaskRecord.review_run_id,
        )
        .join(
            PullRequestVersionRecord,
            PullRequestVersionRecord.review_version_key
            == ReviewRunRecord.review_version_key,
        )
        .outerjoin(
            PullRequestFileRecord,
            PullRequestFileRecord.pull_request_version_id
            == PullRequestVersionRecord.id,
        )
        .where(
            ReviewTaskRecord.id == lease.task_id,
            ReviewTaskRecord.review_run_id == lease.review_run_id,
            ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
            ReviewTaskRecord.lease_owner == lease.worker_id,
            ReviewTaskRecord.attempt_count == lease.attempt_count,
            ReviewTaskRecord.model_attempt_count == lease.model_attempt_count,
            ReviewTaskRecord.ci_poll_count == lease.ci_poll_count,
            ReviewTaskRecord.claimed_from_status
            == ExecutionStatus.READY_FOR_REVIEW.value,
            ReviewRunRecord.execution_status == ExecutionStatus.RUNNING.value,
            ReviewTaskRecord.lease_expires_at.is_not(None),
            ReviewTaskRecord.lease_expires_at > now,
        )
        .order_by(PullRequestFileRecord.path.asc())
        .limit(3001)
    )
    with self._sessions() as session:
        try:
            rows = session.execute(statement).all()
        except SQLAlchemyError as exc:
            raise TaskQueueError("review planning input could not be loaded") from exc
    if not rows:
        raise TaskLeaseLostError()

    first = rows[0]
    if (
        first.run_head_sha != first.version_head_sha
        or first.review_version_key
        != build_review_version_key(
            first.repository_id,
            first.pull_request_number,
            first.run_head_sha,
        )
    ):
        raise ReviewPlanConflictError("持久化 PR 版本与当前审查任务身份不一致")
    if first.context_fetched_at is None or first.files_complete is not True:
        raise ReviewPlanInputError()
    if (
        first.changed_files_count is None
        or first.changed_files_count < 0
        or first.changed_files_count > 3000
    ):
        raise ReviewPlanInputError("PR changed files 数量超出规划边界")

    file_rows = [row for row in rows if row.file_id is not None]
    if len(file_rows) > 3000 or len(file_rows) != first.changed_files_count:
        raise ReviewPlanInputError("PR 文件快照数量与 GitHub 元数据不一致")
    try:
        files = tuple(
            PullRequestFile(
                path=row.file_path,
                previous_path=row.previous_path,
                status=ChangedFileStatus(row.file_status),
                blob_sha=row.blob_sha,
                additions=row.additions,
                deletions=row.deletions,
                changes=row.changes,
                patch_state=PatchState(row.patch_state),
                patch=row.patch,
            )
            for row in file_rows
        )
    except (TypeError, ValueError) as exc:
        raise ReviewPlanInputError("PR 文件快照字段不符合规划契约") from exc
    return ReviewPlanningInput(
        target=ReviewTarget(
            installation_id=first.installation_id,
            repository_id=first.repository_id,
            repository=first.repository,
            pull_request_number=first.pull_request_number,
            head_sha=first.run_head_sha,
            review_version_key=first.review_version_key,
            context_fetched_at=_as_utc(first.context_fetched_at),
        ),
        files=files,
    )


def store_review_plan(
    self: QueueStorage,
    lease: ReviewTaskLease,
    rules: RepositoryRulesSnapshot,
    plan: ReviewPlan,
) -> StoredReviewPlan:
    """短事务校验版本并批量保存完整 Review Plan。"""

    if lease.claimed_from_status is not ExecutionStatus.READY_FOR_REVIEW:
        raise ReviewPlanConflictError("计划只能由可审查阶段的租约保存")
    if (
        rules.repository_id != plan.repository_id
        or rules.repository != plan.repository
        or rules.head_sha != plan.head_sha
        or tuple(rules.rules) != tuple(plan.rules)
    ):
        raise ReviewPlanConflictError("规则快照与 Review Plan 身份不一致")

    now = self._clock()
    with self._sessions() as session:
        try:
            existing = session.execute(
                select(
                    ReviewPlanRecord.id,
                    ReviewPlanRecord.plan_fingerprint,
                    ReviewPlanRecord.review_version_key,
                    ReviewPlanRecord.head_sha,
                    ReviewRunRecord.execution_status,
                )
                .join(
                    ReviewRunRecord,
                    ReviewRunRecord.id == ReviewPlanRecord.review_run_id,
                )
                .where(ReviewPlanRecord.review_run_id == lease.review_run_id)
            ).one_or_none()
            if existing is not None:
                if (
                    existing.plan_fingerprint != plan.plan_fingerprint
                    or existing.review_version_key != plan.review_version_key
                    or existing.head_sha != plan.head_sha
                ):
                    raise ReviewPlanConflictError(
                        "同一审查运行已经保存了不同指纹的计划"
                    )
                return StoredReviewPlan(
                    plan_id=existing.id,
                    created=False,
                    execution_status=ExecutionStatus(existing.execution_status),
                )

            # 只有“已经存在且指纹完全一致”的只读幂等重放允许使用已失效
            # 租约；任何新建或冲突路径都必须在下面重新校验当前所有权。
            task, run = _locked_owned_task_with_run(session, lease, now)
            if (
                run.review_version_key != plan.review_version_key
                or run.repository_id != plan.repository_id
                or run.repository != plan.repository
                or run.pull_request_number != plan.pull_request_number
                or run.head_sha != plan.head_sha
            ):
                raise ReviewPlanConflictError(
                    "Review Plan 与被锁定任务的精确版本不一致"
                )

            version = session.scalar(
                select(PullRequestVersionRecord)
                .where(
                    PullRequestVersionRecord.review_version_key
                    == run.review_version_key
                )
                .options(
                    Load(PullRequestVersionRecord).load_only(
                        PullRequestVersionRecord.id,
                        PullRequestVersionRecord.review_version_key,
                        PullRequestVersionRecord.repository_id,
                        PullRequestVersionRecord.repository,
                        PullRequestVersionRecord.pull_request_number,
                        PullRequestVersionRecord.head_sha,
                        PullRequestVersionRecord.files_complete,
                        PullRequestVersionRecord.changed_files_count,
                        raiseload=True,
                    )
                )
                .with_for_update()
            )
            if version is None:
                raise ReviewPlanInputError("当前 SHA 没有持久化 PR 版本")
            if (
                version.review_version_key != plan.review_version_key
                or version.repository_id != plan.repository_id
                or version.repository != plan.repository
                or version.pull_request_number != plan.pull_request_number
                or version.head_sha != plan.head_sha
            ):
                raise ReviewPlanConflictError("当前 PR 版本已经不再匹配 Review Plan")
            if version.files_complete is not True:
                raise ReviewPlanInputError()
            if version.changed_files_count != len(plan.files):
                raise ReviewPlanInputError("Review Plan 文件数与当前 SHA 快照不一致")

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
                _set_owned_status(
                    task,
                    run,
                    ExecutionStatus.SUPERSEDED,
                    now,
                )
                _set_workflow_status(
                    task,
                    run,
                    ExecutionStatus.SUPERSEDED,
                    now,
                )
                run.publish_attempt_token = None
                run.coverage_status = CoverageStatus.STALE.value
                _add_event(
                    self,
                    session,
                    task,
                    "review.superseded",
                    f"plan-head-stale:{task.attempt_count}",
                    now,
                )
                session.commit()
                return StoredReviewPlan(
                    plan_id=None,
                    created=False,
                    execution_status=ExecutionStatus.SUPERSEDED,
                )

            plan_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"openreviewer:plan:{run.id}:{plan.plan_fingerprint}",
                )
            )
            session.add(
                ReviewPlanRecord(
                    id=plan_id,
                    review_run_id=run.id,
                    pull_request_version_id=version.id,
                    review_version_key=plan.review_version_key,
                    head_sha=plan.head_sha,
                    plan_fingerprint=plan.plan_fingerprint,
                    planner_version=plan.planner_version,
                    rules_complete=rules.complete,
                    incomplete_files=list(rules.incomplete_files),
                    rule_issues=[
                        issue.model_dump(mode="json") for issue in rules.issues
                    ],
                    candidate_count=rules.candidate_count,
                    requested_candidate_count=rules.requested_candidate_count,
                    rule_count=len(plan.rules),
                    unit_count=len(plan.units),
                    file_count=len(plan.files),
                    total_estimated_input_bytes=(plan.total_estimated_input_bytes),
                    max_model_http_calls=plan.model_budget.max_http_calls,
                    max_model_input_tokens=plan.model_budget.max_input_tokens,
                    max_model_output_tokens=plan.model_budget.max_output_tokens,
                    max_model_cost_microusd=(
                        plan.model_budget.max_estimated_cost_microusd
                    ),
                    max_model_duration_seconds=(plan.model_budget.max_duration_seconds),
                    model_budget_mode=plan.model_budget.enforcement,
                    model_http_calls=0,
                    model_input_tokens=0,
                    model_output_tokens=0,
                    model_estimated_cost_microusd=0,
                    created_at=now,
                )
            )
            session.flush()

            rule_rows = [
                {
                    "id": str(
                        uuid5(
                            NAMESPACE_URL,
                            f"openreviewer:plan-rule:{plan_id}:{rule.path}",
                        )
                    ),
                    "review_plan_id": plan_id,
                    "ordinal": ordinal,
                    "path": rule.path,
                    "scope": rule.scope,
                    "blob_sha": rule.blob_sha,
                    "content": rule.content,
                    "content_sha256": rule.content_sha256,
                    "byte_size": rule.byte_size,
                }
                for ordinal, rule in enumerate(plan.rules)
            ]
            if rule_rows:
                session.execute(insert(ReviewPlanRuleRecord), rule_rows)

            unit_ids = {
                unit.unit_key: str(
                    uuid5(
                        NAMESPACE_URL,
                        f"openreviewer:review-unit:{plan_id}:{unit.unit_key}",
                    )
                )
                for unit in plan.units
            }
            unit_rows = [
                {
                    "id": unit_ids[unit.unit_key],
                    "review_plan_id": plan_id,
                    "ordinal": ordinal,
                    "unit_key": unit.unit_key,
                    "group_key": unit.group_key or unit.unit_key,
                    "file": unit.file,
                    "blob_sha": unit.blob_sha,
                    "language": unit.language,
                    "patch": unit.patch,
                    "patch_sha256": unit.patch_sha256,
                    "rule_paths": list(unit.rule_paths),
                    "review_domains": [agent.value for agent in unit.review_domains],
                    "estimated_input_bytes": unit.estimated_input_bytes,
                    "planner_version": unit.planner_version,
                }
                for ordinal, unit in enumerate(plan.units)
            ]
            if unit_rows:
                session.execute(insert(ReviewUnitRecord), unit_rows)

            file_rows = [
                {
                    "id": str(
                        uuid5(
                            NAMESPACE_URL,
                            f"openreviewer:file-plan:{plan_id}:{item.file}",
                        )
                    ),
                    "review_plan_id": plan_id,
                    "review_unit_id": (
                        unit_ids[item.unit_key] if item.unit_key is not None else None
                    ),
                    "ordinal": ordinal,
                    "file": item.file,
                    "decision": item.decision.value,
                }
                for ordinal, item in enumerate(plan.files)
            ]
            if file_rows:
                session.execute(insert(ReviewFilePlanRecord), file_rows)

            _set_owned_status(
                task,
                run,
                ExecutionStatus.READY_FOR_REVIEW,
                now,
            )
            _set_workflow_status(
                task,
                run,
                ExecutionStatus.AGENT_BATCHES,
                now,
            )
            _add_event(
                self,
                session,
                task,
                "review.plan.prepared",
                plan.plan_fingerprint,
                now,
                extra_payload={
                    "review_plan_id": plan_id,
                    "plan_fingerprint": plan.plan_fingerprint,
                    "rule_count": len(plan.rules),
                    "unit_count": len(plan.units),
                    "file_count": len(plan.files),
                    "rules_complete": rules.complete,
                },
            )
            session.commit()
            return StoredReviewPlan(
                plan_id=plan_id,
                created=True,
                execution_status=ExecutionStatus.READY_FOR_REVIEW,
            )
        except (ReviewPlanConflictError, ReviewPlanInputError, TaskLeaseLostError):
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise TaskQueueError("Review Plan could not be persisted") from exc


def load_model_review_input(
    self: QueueStorage, lease: ReviewTaskLease
) -> ModelReviewInput:
    """用三次有界查询读取计划元数据、规则和全部 Review Unit。"""

    if (
        lease.claimed_from_status is not ExecutionStatus.READY_FOR_REVIEW
        or lease.review_plan_id is None
    ):
        raise ModelReviewInputError("当前租约不属于模型审查阶段")
    now = self._clock()
    plan_statement = (
        select(
            ReviewPlanRecord.id.label("plan_id"),
            ReviewPlanRecord.review_run_id,
            ReviewPlanRecord.plan_fingerprint,
            ReviewPlanRecord.planner_version,
            ReviewPlanRecord.review_version_key,
            ReviewPlanRecord.head_sha.label("plan_head_sha"),
            ReviewPlanRecord.rule_count,
            ReviewPlanRecord.unit_count,
            ReviewPlanRecord.total_estimated_input_bytes,
            ReviewPlanRecord.model_review_completed_at,
            ReviewRunRecord.repository_policy,
            ReviewRunRecord.repository_id,
            ReviewRunRecord.repository,
            ReviewRunRecord.pull_request_number,
            ReviewRunRecord.head_sha.label("run_head_sha"),
        )
        .select_from(ReviewTaskRecord)
        .join(
            ReviewRunRecord,
            ReviewRunRecord.id == ReviewTaskRecord.review_run_id,
        )
        .join(
            ReviewPlanRecord,
            ReviewPlanRecord.review_run_id == ReviewRunRecord.id,
        )
        .where(
            ReviewTaskRecord.id == lease.task_id,
            ReviewTaskRecord.review_run_id == lease.review_run_id,
            ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
            ReviewTaskRecord.lease_owner == lease.worker_id,
            ReviewTaskRecord.attempt_count == lease.attempt_count,
            ReviewTaskRecord.model_attempt_count == lease.model_attempt_count,
            ReviewTaskRecord.ci_poll_count == lease.ci_poll_count,
            ReviewTaskRecord.claimed_from_status
            == ExecutionStatus.READY_FOR_REVIEW.value,
            ReviewRunRecord.execution_status == ExecutionStatus.RUNNING.value,
            ReviewPlanRecord.id == lease.review_plan_id,
            ReviewPlanRecord.model_review_completed_at.is_(None),
            ReviewTaskRecord.lease_expires_at.is_not(None),
            ReviewTaskRecord.lease_expires_at > now,
        )
        .limit(1)
    )
    rules_statement = (
        select(
            ReviewPlanRuleRecord.path,
            ReviewPlanRuleRecord.scope,
            ReviewPlanRuleRecord.blob_sha,
            ReviewPlanRuleRecord.content,
            ReviewPlanRuleRecord.content_sha256,
            ReviewPlanRuleRecord.byte_size,
        )
        .where(ReviewPlanRuleRecord.review_plan_id == lease.review_plan_id)
        .order_by(ReviewPlanRuleRecord.ordinal.asc())
        .limit(257)
    )
    units_statement = (
        select(
            ReviewUnitRecord.unit_key,
            ReviewUnitRecord.group_key,
            ReviewUnitRecord.file,
            ReviewUnitRecord.blob_sha,
            ReviewUnitRecord.language,
            ReviewUnitRecord.patch,
            ReviewUnitRecord.patch_sha256,
            ReviewUnitRecord.rule_paths,
            ReviewUnitRecord.review_domains,
            ReviewUnitRecord.estimated_input_bytes,
            ReviewUnitRecord.planner_version,
        )
        .where(ReviewUnitRecord.review_plan_id == lease.review_plan_id)
        .order_by(ReviewUnitRecord.ordinal.asc())
        .limit(3001)
    )
    with self._sessions() as session:
        try:
            plan_row = session.execute(plan_statement).one_or_none()
            if plan_row is None:
                raise TaskLeaseLostError()
            rule_rows = session.execute(rules_statement).all()
            unit_rows = session.execute(units_statement).all()
        except TaskLeaseLostError:
            raise
        except SQLAlchemyError as exc:
            raise TaskQueueError("model review input could not be loaded") from exc

    if plan_row.plan_head_sha != plan_row.run_head_sha:
        raise ModelReviewConflictError("Review Plan 与运行的 head SHA 不一致")
    if len(rule_rows) > 256 or len(rule_rows) != plan_row.rule_count:
        raise ModelReviewInputError("Review Plan 规则快照数量不一致")
    if len(unit_rows) > 3000 or len(unit_rows) != plan_row.unit_count:
        raise ModelReviewInputError("Review Plan Unit 数量不一致")
    try:
        rules = tuple(
            RepositoryRule(
                path=row.path,
                scope=row.scope,
                blob_sha=row.blob_sha,
                content=row.content,
                content_sha256=row.content_sha256,
                byte_size=row.byte_size,
            )
            for row in rule_rows
        )
        units = tuple(
            ReviewUnit(
                unit_key=row.unit_key,
                group_key=row.group_key,
                review_version_key=plan_row.review_version_key,
                head_sha=plan_row.plan_head_sha,
                file=row.file,
                blob_sha=row.blob_sha,
                language=row.language,
                patch=row.patch,
                patch_sha256=row.patch_sha256,
                rule_paths=tuple(row.rule_paths),
                review_domains=tuple(row.review_domains or DEFAULT_REVIEW_DOMAINS),
                estimated_input_bytes=row.estimated_input_bytes,
                planner_version=row.planner_version,
            )
            for row in unit_rows
        )
        return ModelReviewInput(
            review_plan_id=plan_row.plan_id,
            review_run_id=plan_row.review_run_id,
            plan_fingerprint=plan_row.plan_fingerprint,
            planner_version=plan_row.planner_version,
            review_version_key=plan_row.review_version_key,
            repository_id=plan_row.repository_id,
            repository=plan_row.repository,
            repository_policy=plan_row.repository_policy,
            pull_request_number=plan_row.pull_request_number,
            head_sha=plan_row.plan_head_sha,
            rules=rules,
            units=units,
            total_estimated_input_bytes=plan_row.total_estimated_input_bytes,
        )
    except (TypeError, ValueError) as exc:
        raise ModelReviewInputError("持久化 Review Plan 不符合模型输入契约") from exc
