"""审查管理 queries 存储职责。"""

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from domain.enums import ExecutionStatus
from domain.pagination import CursorPage, decode_cursor, encode_cursor
from domain.repository_policy import RepositoryPolicySnapshot
from domain.review_planning import ReviewFilePlan
from domain.review_progress import BatchSnapshot
from domain.security import redact_sensitive
from persistence.management.common import (
    _as_utc,
    _project_agent_progress,
    _required_utc,
    _review_change_token,
    _safe_payload,
)
from persistence.management.context import ManagementStorage
from persistence.management.findings import (
    _load_evaluation_gates,
    _load_finding_counts,
    _load_findings,
)
from persistence.models import (
    FindingLifecycleRecord,
    ModelCallRecord,
    ModelReviewBatchRecord,
    OutboxEventRecord,
    PullRequestCiCheckRecord,
    PullRequestVersionRecord,
    ReviewFilePlanRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
    ReviewUnitRecord,
)
from persistence.resource_scope import resource_predicate
from persistence.review_progress import load_batch_progress, load_progress_events
from services.rbac import ResourceScope
from services.review_management import (
    FindingCursor,
    ReviewIdentityTarget,
    ReviewManagementPersistenceError,
    ReviewNotFoundError,
    StoredCiCheck,
    StoredFinding,
    StoredReviewDetails,
    StoredReviewEvent,
    decode_finding_cursor,
    encode_finding_cursor,
)
from services.task_queue import ReviewTarget


def batch_page(
    self: ManagementStorage,
    review_run_id: str,
    agent: str,
    *,
    after: int = 0,
    limit: int = 10,
    scope: ResourceScope | None = None,
) -> CursorPage[BatchSnapshot]:
    change_token(self, review_run_id, scope=scope)
    batch = ModelReviewBatchRecord
    with self._sessions() as session:
        rows = (
            session.execute(
                select(
                    batch.batch_number,
                    batch.status,
                    batch.duration_ms,
                    batch.error_code,
                    batch.error_message,
                    batch.review_plan_id,
                    batch.unit_keys,
                    batch.estimated_input_tokens,
                    batch.result["output"]["findings"].label("candidates"),
                )
                .join(ReviewPlanRecord, ReviewPlanRecord.id == batch.review_plan_id)
                .where(
                    ReviewPlanRecord.review_run_id == review_run_id,
                    batch.agent == agent,
                    batch.batch_number > after,
                )
                .order_by(batch.batch_number)
                .limit(limit + 1)
            )
            .mappings()
            .all()
        )
        unit_keys = {key for row in rows[:limit] for key in row["unit_keys"]}
        files: dict[str, str] = dict(session.execute(select(ReviewUnitRecord.unit_key, ReviewUnitRecord.file).where(
            ReviewUnitRecord.review_plan_id == rows[0]["review_plan_id"],
            ReviewUnitRecord.unit_key.in_(unit_keys),
        )).tuples().all()) if rows and unit_keys else {}
    return CursorPage(
        items=tuple(
            BatchSnapshot.model_validate(redact_sensitive({**row, "candidates": row["candidates"] or [],
                "files": [files[key] for key in row["unit_keys"] if key in files]}))
            for row in rows[:limit]
        ),
        next_cursor=str(rows[limit - 1]["batch_number"]) if len(rows) > limit else None,
    )


def finding_page(
    self: ManagementStorage,
    review_run_id: str,
    *,
    limit: int = 10,
    cursor: str | None = None,
    severity: str | None = None,
    adjudication_status: str | None = None,
    query: str = "",
    scope: ResourceScope | None = None,
) -> CursorPage[StoredFinding]:
    change_token(self, review_run_id, scope=scope)
    with self._sessions() as session:
        items, has_more = _load_findings(
            session,
            review_run_id,
            limit=limit,
            cursor=decode_finding_cursor(cursor) if cursor else None,
            adjudication_status=adjudication_status,
            severity=severity,
            search=query,
        )
    return CursorPage(
        items=items,
        next_cursor=encode_finding_cursor(items[-1].created_at, items[-1].id)
        if has_more
        else None,
    )


def event_page(
    self: ManagementStorage,
    review_run_id: str,
    *,
    limit: int = 10,
    cursor: str | None = None,
    event_filter: str = "all",
    scope: ResourceScope | None = None,
) -> CursorPage[StoredReviewEvent]:
    change_token(self, review_run_id, scope=scope)
    query = select(
        OutboxEventRecord.id,
        OutboxEventRecord.event_type,
        OutboxEventRecord.payload,
        OutboxEventRecord.occurred_at,
    ).where(
        OutboxEventRecord.aggregate_type == "review_run",
        OutboxEventRecord.aggregate_id == review_run_id,
        OutboxEventRecord.event_type.not_in(
            ("review.model.budget_exhausted", "review.model.budget_observed")
        ),
    )
    if cursor:
        date, identifier = decode_cursor(cursor)
        query = query.where(
            or_(
                OutboxEventRecord.occurred_at < date,
                and_(
                    OutboxEventRecord.occurred_at == date,
                    OutboxEventRecord.id < identifier,
                ),
            )
        )
    if event_filter == "model":
        query = query.where(OutboxEventRecord.event_type.startswith("review.model."))
    elif event_filter == "workflow":
        query = query.where(~OutboxEventRecord.event_type.startswith("review.model."))
    elif event_filter == "errors":
        query = query.where(
            or_(
                OutboxEventRecord.event_type.contains("failed"),
                OutboxEventRecord.event_type.contains("timed_out"),
            )
        )
    with self._sessions() as session:
        rows = session.execute(
            query.order_by(
                OutboxEventRecord.occurred_at.desc(), OutboxEventRecord.id.desc()
            ).limit(limit + 1)
        ).all()
    events = []
    for row in rows[:limit]:
        payload = _safe_payload(row.payload)
        events.append(
            StoredReviewEvent(
                id=row.id,
                event_type=row.event_type,
                payload=payload if isinstance(payload, dict) else {},
                occurred_at=_required_utc(row.occurred_at, "outbox_event.occurred_at"),
            )
        )
    items = tuple(events)
    return CursorPage(
        items=items,
        next_cursor=encode_cursor(items[-1].occurred_at, items[-1].id)
        if len(rows) > limit
        else None,
    )


def change_token(
    self: ManagementStorage,
    review_run_id: str,
    *,
    scope: ResourceScope | None = None,
) -> str:
    """用一条索引查询生成任务详情变化令牌。"""

    latest_event_id = (
        select(OutboxEventRecord.id)
        .where(
            OutboxEventRecord.aggregate_type == "review_run",
            OutboxEventRecord.aggregate_id == review_run_id,
        )
        .order_by(
            OutboxEventRecord.occurred_at.desc(),
            OutboxEventRecord.id.desc(),
        )
        .limit(1)
        .scalar_subquery()
    )
    with self._sessions() as session:
        try:
            row = session.execute(
                select(
                    ReviewRunRecord.updated_at,
                    ReviewTaskRecord.updated_at,
                    latest_event_id.label("latest_event_id"),
                )
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
                .limit(1)
            ).one_or_none()
            if row is None:
                raise ReviewNotFoundError("审查任务不存在")
            return _review_change_token(row[0], row[1], row[2])
        except ReviewNotFoundError:
            raise
        except (SQLAlchemyError, ValueError) as exc:
            raise ReviewManagementPersistenceError(
                "review change token could not be loaded"
            ) from exc


def get(
    self: ManagementStorage,
    review_run_id: str,
    *,
    finding_limit: int = 50,
    finding_cursor: FindingCursor | None = None,
    finding_adjudication_status: str | None = None,
    scope: ResourceScope | None = None,
    view: str = "full",
) -> StoredReviewDetails:
    """读取一条运行、计划、模型调用及其有界子资源快照。

    主记录、Finding、Finding 全量聚合、CI、文件覆盖汇总、评测门槛和事件最多
    使用七次查询；列表子查询都带 ``LIMIT``，循环只负责转换已取回的行。
    """

    if not 1 <= finding_limit <= 200:
        raise ValueError("finding limit must be between 1 and 200")

    with self._sessions() as session:
        try:
            row = (
                session.execute(
                    select(
                        ReviewRunRecord.id.label("review_run_id"),
                        ReviewTaskRecord.id.label("review_task_id"),
                        ReviewRunRecord.repository_policy,
                        ReviewRunRecord.model_request_count,
                        ReviewRunRecord.snapshot_review,
                        ReviewRunRecord.review_version_key,
                        ReviewRunRecord.installation_id,
                        ReviewRunRecord.repository_id,
                        ReviewRunRecord.repository,
                        ReviewRunRecord.pull_request_number,
                        ReviewRunRecord.head_sha,
                        ReviewRunRecord.execution_status,
                        ReviewRunRecord.workflow_status,
                        ReviewRunRecord.review_conclusion,
                        ReviewRunRecord.coverage_status,
                        ReviewTaskRecord.priority,
                        ReviewTaskRecord.attempt_count,
                        ReviewTaskRecord.model_attempt_count,
                        ReviewTaskRecord.max_attempts,
                        ReviewTaskRecord.ci_poll_count,
                        ReviewTaskRecord.available_at,
                        ReviewTaskRecord.claimed_from_status,
                        ReviewTaskRecord.lease_owner,
                        ReviewTaskRecord.lease_expires_at,
                        ReviewTaskRecord.last_error,
                        ReviewTaskRecord.last_error_code,
                        ReviewTaskRecord.last_error_retryable,
                        ReviewTaskRecord.last_error_details,
                        ReviewRunRecord.created_at,
                        ReviewRunRecord.updated_at,
                        ReviewTaskRecord.updated_at.label("task_updated_at"),
                        PullRequestVersionRecord.id.label("pr_version_id"),
                        PullRequestVersionRecord.title.label("pr_title"),
                        PullRequestVersionRecord.author_login.label("pr_author_login"),
                        PullRequestVersionRecord.html_url.label("pr_html_url"),
                        PullRequestVersionRecord.head_repository,
                        PullRequestVersionRecord.head_ref,
                        PullRequestVersionRecord.base_repository,
                        PullRequestVersionRecord.base_ref,
                        PullRequestVersionRecord.identity_fetched_at,
                        PullRequestVersionRecord.pr_state,
                        PullRequestVersionRecord.is_draft,
                        PullRequestVersionRecord.changed_files_count,
                        PullRequestVersionRecord.files_complete,
                        PullRequestVersionRecord.diff_complete,
                        PullRequestVersionRecord.context_fetched_at,
                        PullRequestVersionRecord.ci_state,
                        PullRequestVersionRecord.ci_checks_complete,
                        PullRequestVersionRecord.ci_checked_at,
                        ReviewPlanRecord.id.label("review_plan_id"),
                        ReviewPlanRecord.created_at.label("plan_created_at"),
                        ReviewPlanRecord.file_count.label("plan_file_count"),
                        ReviewPlanRecord.unit_count.label("plan_unit_count"),
                        ReviewPlanRecord.rule_count.label("plan_rule_count"),
                        ReviewPlanRecord.total_estimated_input_bytes.label(
                            "plan_input_bytes"
                        ),
                        ReviewPlanRecord.rules_complete.label("plan_rules_complete"),
                        ReviewPlanRecord.model_review_completed_at,
                        ModelCallRecord.id.label("model_call_id"),
                        ModelCallRecord.provider.label("model_provider"),
                        ModelCallRecord.api_protocol.label("model_protocol"),
                        ModelCallRecord.model.label("model_name"),
                        ModelCallRecord.status.label("model_status"),
                        ModelCallRecord.response_status.label("model_response_status"),
                        ModelCallRecord.duration_ms.label("model_duration_ms"),
                        ModelCallRecord.input_tokens.label("model_input_tokens"),
                        ModelCallRecord.output_tokens.label("model_output_tokens"),
                        ModelCallRecord.cache_read_input_tokens.label(
                            "model_cache_read_tokens"
                        ),
                        ModelCallRecord.cache_write_input_tokens.label(
                            "model_cache_write_tokens"
                        ),
                        ModelCallRecord.reasoning_output_tokens.label(
                            "model_reasoning_tokens"
                        ),
                        ModelCallRecord.estimated_cost_microusd.label(
                            "model_cost_microusd"
                        ),
                        ModelCallRecord.finding_count.label("model_finding_count"),
                        ModelCallRecord.created_at.label("model_created_at"),
                        select(func.count())
                        .select_from(FindingLifecycleRecord)
                        .where(
                            FindingLifecycleRecord.fixed_by_review_run_id
                            == ReviewRunRecord.id
                        )
                        .correlate(ReviewRunRecord)
                        .scalar_subquery()
                        .label("fixed_finding_count"),
                    )
                    .join(
                        ReviewTaskRecord,
                        ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
                    )
                    .outerjoin(
                        PullRequestVersionRecord,
                        PullRequestVersionRecord.review_version_key
                        == ReviewRunRecord.review_version_key,
                    )
                    .outerjoin(
                        ReviewPlanRecord,
                        ReviewPlanRecord.review_run_id == ReviewRunRecord.id,
                    )
                    .outerjoin(
                        ModelCallRecord,
                        ModelCallRecord.review_plan_id == ReviewPlanRecord.id,
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
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ReviewNotFoundError("审查任务不存在")

            findings, finding_has_more = (
                _load_findings(
                    session,
                    review_run_id,
                    limit=finding_limit,
                    cursor=finding_cursor,
                    adjudication_status=finding_adjudication_status,
                )
                if view == "full"
                else ((), False)
            )
            finding_counts = _load_finding_counts(session, review_run_id)
            evaluation_gates = (
                _load_evaluation_gates(
                    session,
                    row["repository_id"],
                )
                if view in {"full", "findings"}
                else ()
            )
            version_id = row["pr_version_id"]
            ci_checks = _load_ci_checks(session, version_id)
            plan_file_decisions = _load_plan_file_decisions(
                session,
                row["review_plan_id"],
            )
            excluded_file_examples: tuple[ReviewFilePlan, ...] = ()
            if any(count for decision, count in plan_file_decisions.items() if decision != "planned"):
                excluded_file_examples = tuple(ReviewFilePlan.model_validate(dict(item)) for item in session.execute(
                    select(ReviewFilePlanRecord.file, ReviewFilePlanRecord.decision).where(
                        ReviewFilePlanRecord.review_plan_id == row["review_plan_id"],
                        ReviewFilePlanRecord.decision != "planned",
                    ).order_by(ReviewFilePlanRecord.ordinal).limit(10)
                ).mappings())
            events = (
                _load_events(session, review_run_id)
                if view == "full"
                else load_progress_events(session, review_run_id)
            )
            batch_progress = (
                load_batch_progress(session, row["review_plan_id"])
                if view != "full"
                else {}
            )
            safe_error = redact_sensitive(row["last_error"])
            safe_code = redact_sensitive(row["last_error_code"])
            safe_details = _safe_payload(row["last_error_details"])
            (
                agent_statuses,
                agent_summaries,
                aggregation_status,
                summary_status,
                partial_result,
                failed_agents,
                failed_batches,
            ) = _project_agent_progress(
                events,
                coverage_status=row["coverage_status"],
                model_completed=row["model_review_completed_at"] is not None,
            )
            return StoredReviewDetails(
                excluded_file_examples=excluded_file_examples,
                snapshot_review=row["snapshot_review"],
                repository_policy=(
                    RepositoryPolicySnapshot.model_validate(row["repository_policy"])
                    if row["repository_policy"] is not None
                    else None
                ),
                model_request_count=(
                    row["model_request_count"]
                    if row["repository_policy"] is not None
                    and row["repository_policy"].get("max_model_requests") is not None
                    else None
                ),
                review_run_id=row["review_run_id"],
                review_task_id=row["review_task_id"],
                change_token=_review_change_token(
                    row["updated_at"],
                    row["task_updated_at"],
                    events[-1].id if events else None,
                ),
                review_version_key=row["review_version_key"],
                installation_id=row["installation_id"],
                repository_id=row["repository_id"],
                repository=row["repository"],
                pull_request_number=row["pull_request_number"],
                head_sha=row["head_sha"],
                execution_status=ExecutionStatus(row["execution_status"]),
                workflow_status=ExecutionStatus(
                    row["workflow_status"] or row["execution_status"]
                ),
                review_conclusion=row["review_conclusion"],
                coverage_status=row["coverage_status"],
                priority=row["priority"],
                attempt_count=row["attempt_count"],
                model_attempt_count=row["model_attempt_count"],
                max_attempts=row["max_attempts"],
                ci_poll_count=row["ci_poll_count"],
                available_at=_required_utc(
                    row["available_at"],
                    "review_task.available_at",
                ),
                claimed_from_status=row["claimed_from_status"],
                lease_owner=row["lease_owner"],
                lease_expires_at=_as_utc(row["lease_expires_at"]),
                last_error=(safe_error if isinstance(safe_error, str) else None),
                last_error_code=(safe_code if isinstance(safe_code, str) else None),
                last_error_retryable=row["last_error_retryable"],
                last_error_details=(
                    safe_details if isinstance(safe_details, dict) else None
                ),
                created_at=_required_utc(
                    row["created_at"],
                    "review_run.created_at",
                ),
                updated_at=_required_utc(
                    row["updated_at"],
                    "review_run.updated_at",
                ),
                pr_title=row["pr_title"],
                pr_author_login=row["pr_author_login"],
                pr_html_url=row["pr_html_url"],
                head_repository=row["head_repository"],
                head_ref=row["head_ref"],
                base_repository=row["base_repository"],
                base_ref=row["base_ref"],
                identity_fetched_at=_as_utc(row["identity_fetched_at"]),
                pr_state=row["pr_state"],
                pr_is_draft=row["is_draft"],
                changed_files_count=row["changed_files_count"],
                files_complete=row["files_complete"],
                diff_complete=row["diff_complete"],
                context_fetched_at=_as_utc(row["context_fetched_at"]),
                ci_state=row["ci_state"],
                ci_checks_complete=row["ci_checks_complete"],
                ci_checked_at=_as_utc(row["ci_checked_at"]),
                review_plan_id=row["review_plan_id"],
                plan_created_at=_as_utc(row["plan_created_at"]),
                plan_file_count=row["plan_file_count"],
                plan_unit_count=row["plan_unit_count"],
                plan_rule_count=row["plan_rule_count"],
                plan_input_bytes=row["plan_input_bytes"],
                plan_rules_complete=row["plan_rules_complete"],
                plan_file_decisions=plan_file_decisions,
                model_review_completed_at=_as_utc(row["model_review_completed_at"]),
                model_call_id=row["model_call_id"],
                model_provider=row["model_provider"],
                model_protocol=row["model_protocol"],
                model_name=row["model_name"],
                model_status=row["model_status"],
                model_response_status=row["model_response_status"],
                model_duration_ms=row["model_duration_ms"],
                model_input_tokens=row["model_input_tokens"],
                model_output_tokens=row["model_output_tokens"],
                model_cache_read_tokens=row["model_cache_read_tokens"],
                model_cache_write_tokens=row["model_cache_write_tokens"],
                model_reasoning_tokens=row["model_reasoning_tokens"],
                model_cost_microusd=row["model_cost_microusd"],
                model_finding_count=row["model_finding_count"],
                model_created_at=_as_utc(row["model_created_at"]),
                fixed_finding_count=int(row["fixed_finding_count"] or 0),
                finding_counts=finding_counts,
                finding_has_more=finding_has_more,
                evaluation_gates=evaluation_gates,
                findings=findings,
                ci_checks=ci_checks,
                events=events,
                agent_statuses=agent_statuses,
                agent_summaries=agent_summaries,
                aggregation_status=aggregation_status,
                summary_status=summary_status,
                partial_result=partial_result,
                failed_agents=failed_agents,
                failed_batches=failed_batches,
                batch_progress=batch_progress,
            )
        except ReviewNotFoundError:
            raise
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise ReviewManagementPersistenceError(
                "review details could not be loaded"
            ) from exc


def get_identity_target(
    self: ManagementStorage,
    review_run_id: str,
    *,
    scope: ResourceScope | None = None,
) -> ReviewIdentityTarget:
    """读取历史任务的稳定 GitHub 目标，不持有事务访问外部 API。"""

    with self._sessions() as session:
        try:
            row = session.execute(
                select(
                    ReviewRunRecord.installation_id,
                    ReviewRunRecord.repository_id,
                    ReviewRunRecord.repository,
                    ReviewRunRecord.pull_request_number,
                    ReviewRunRecord.head_sha,
                    ReviewRunRecord.review_version_key,
                    PullRequestVersionRecord.context_fetched_at,
                    PullRequestVersionRecord.identity_fetched_at,
                )
                .outerjoin(
                    PullRequestVersionRecord,
                    PullRequestVersionRecord.review_version_key
                    == ReviewRunRecord.review_version_key,
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
            if row is None:
                raise ReviewNotFoundError("审查任务不存在")
            return ReviewIdentityTarget(
                target=ReviewTarget(
                    installation_id=row.installation_id,
                    repository_id=row.repository_id,
                    repository=row.repository,
                    pull_request_number=row.pull_request_number,
                    head_sha=row.head_sha,
                    review_version_key=row.review_version_key,
                    context_fetched_at=_as_utc(row.context_fetched_at),
                ),
                fetched_at=_as_utc(row.identity_fetched_at),
            )
        except ReviewNotFoundError:
            raise
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise ReviewManagementPersistenceError(
                "review identity target could not be loaded"
            ) from exc


def _load_ci_checks(
    session: Session,
    version_id: str | None,
) -> tuple[StoredCiCheck, ...]:
    if version_id is None:
        return ()
    rows = session.execute(
        select(
            PullRequestCiCheckRecord.name,
            PullRequestCiCheckRecord.kind,
            PullRequestCiCheckRecord.status,
            PullRequestCiCheckRecord.conclusion,
            PullRequestCiCheckRecord.observed_at,
        )
        .where(PullRequestCiCheckRecord.pull_request_version_id == version_id)
        .order_by(
            PullRequestCiCheckRecord.observed_at.desc(),
            PullRequestCiCheckRecord.name.asc(),
        )
        .limit(200)
    ).mappings()
    return tuple(
        StoredCiCheck(
            name=row["name"],
            kind=row["kind"],
            status=row["status"],
            conclusion=row["conclusion"],
            observed_at=_required_utc(
                row["observed_at"],
                "pull_request_ci_check.observed_at",
            ),
        )
        for row in rows
    )


def _load_plan_file_decisions(
    session: Session,
    review_plan_id: str | None,
) -> dict[str, int]:
    if review_plan_id is None:
        return {}
    rows = (
        session.execute(
            select(
                ReviewFilePlanRecord.decision,
                func.count().label("count"),
            )
            .where(ReviewFilePlanRecord.review_plan_id == review_plan_id)
            .group_by(ReviewFilePlanRecord.decision)
            .limit(16)
        )
        .tuples()
        .all()
    )
    return {decision: int(count) for decision, count in rows}


def _load_events(
    session: Session,
    review_run_id: str,
) -> tuple[StoredReviewEvent, ...]:
    rows = session.execute(
        select(
            OutboxEventRecord.id,
            OutboxEventRecord.event_type,
            OutboxEventRecord.payload,
            OutboxEventRecord.occurred_at,
        )
        .where(
            OutboxEventRecord.aggregate_type == "review_run",
            OutboxEventRecord.aggregate_id == review_run_id,
        )
        .order_by(
            OutboxEventRecord.occurred_at.desc(),
            OutboxEventRecord.id.desc(),
        )
        .limit(500)
    ).mappings()
    events = []
    for row in rows:
        safe_payload = _safe_payload(row["payload"])
        events.append(
            StoredReviewEvent(
                id=row["id"],
                event_type=row["event_type"],
                payload=safe_payload if isinstance(safe_payload, dict) else {},
                occurred_at=_required_utc(
                    row["occurred_at"],
                    "outbox_event.occurred_at",
                ),
            )
        )
    events.reverse()
    return tuple(events)
