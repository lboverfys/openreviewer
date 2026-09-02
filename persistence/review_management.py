"""审查任务详情、事件日志和人工控制动作的 SQLAlchemy 适配器。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import and_, case, delete, func, or_, select, union_all, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import (
    ExecutionStatus,
    FindingAdjudicationStatus,
    FindingCategory,
    FindingEvaluationVerdict,
    ModelBatchStatus,
    ReviewAgent,
    Severity,
)
from domain.evaluation import (
    EvaluationGatePolicy,
    EvaluationMetrics,
    evaluate_inline_gate,
)
from domain.github import PullRequestSnapshot
from domain.identifiers import build_review_version_key, normalize_sha
from domain.security import redact_sensitive
from domain.workflow import (
    WorkflowAction,
    WorkflowTransitionError,
    next_automatic_stage,
    transition,
)
from persistence.models import (
    FindingEvaluationRecord,
    FindingLifecycleRecord,
    GitHubInstallationRecord,
    ModelCallRecord,
    ModelReviewBatchRecord,
    OutboxEventRecord,
    PullRequestCiCheckRecord,
    PullRequestVersionRecord,
    ReviewFilePlanRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewPlanRuleRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
    ReviewUnitRecord,
)
from persistence.resource_scope import resource_predicate
from services.rbac import ResourceScope
from services.review_management import (
    FindingCursor,
    FindingDecision,
    FindingNotFoundError,
    ReviewAction,
    ReviewActionConflictError,
    ReviewIdentitySyncConflictError,
    ReviewIdentityTarget,
    ReviewManagementPersistenceError,
    ReviewManagementRepository,
    ReviewNotFoundError,
    ReviewPublishUnavailableError,
    StoredCiCheck,
    StoredEvaluationGate,
    StoredFinding,
    StoredFindingCounts,
    StoredReviewDetails,
    StoredReviewEvent,
)
from services.task_queue import ReviewTarget

_PUBLISH_RECOVERY_AFTER = timedelta(minutes=5)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _required_utc(value: datetime | None, field_name: str) -> datetime:
    normalized = _as_utc(value)
    if normalized is None:
        raise ValueError(f"{field_name} must not be null")
    return normalized


def _review_change_token(
    run_updated_at: datetime | None,
    task_updated_at: datetime | None,
    latest_event_id: str | None,
) -> str:
    raw = "|".join(
        (
            _required_utc(
                run_updated_at,
                "review_run.updated_at",
            ).isoformat(timespec="microseconds"),
            _required_utc(
                task_updated_at,
                "review_task.updated_at",
            ).isoformat(timespec="microseconds"),
            latest_event_id or "",
        )
    )
    return sha256(raw.encode("utf-8")).hexdigest()[:24]


def _safe_payload(value: object) -> object:
    """递归脱敏事件 JSON，保证日志面板不会回显凭据。"""

    return redact_sensitive(value)


def _latest_summary_failed(session: Session, review_run_id: str) -> bool:
    """读取最近一次汇总终态，判断汇总节点是否仍可人工重试。"""

    row = session.execute(
        select(OutboxEventRecord.event_type, OutboxEventRecord.payload)
        .where(
            OutboxEventRecord.aggregate_type == "review_run",
            OutboxEventRecord.aggregate_id == review_run_id,
            OutboxEventRecord.event_type.in_(
                (
                    "review.model.summary_completed",
                    "review.model.summary_skipped",
                )
            ),
        )
        .order_by(
            OutboxEventRecord.occurred_at.desc(),
            OutboxEventRecord.id.desc(),
        )
        .limit(1)
    ).one_or_none()
    if row is None:
        return False
    event_type, payload = row
    return (
        event_type == "review.model.summary_completed"
        and isinstance(payload, dict)
        and payload.get("agent_status") == "failed"
    )


def _project_agent_progress(
    events: tuple[StoredReviewEvent, ...],
    *,
    coverage_status: str,
    model_completed: bool,
) -> tuple[
    dict[str, str],
    dict[str, dict[str, object]],
    str,
    str,
    bool,
    tuple[str, ...],
    tuple[dict[str, object], ...],
]:
    """从已加载事件在内存中投影固定 Agent 的可公开状态。

    详情读取只额外扫描一次有界事件列表，不在 Agent/批次循环中查询数据库；
    因此查询次数仍为 O(1)，而投影成本为 O(事件数)。
    """

    statuses: dict[str, str] = {
        "security": "waiting",
        "convention": "waiting",
        "logic": "waiting",
        "summary": "not_executed",
    }
    summaries: dict[str, dict[str, object]] = {}
    failed_batches: dict[tuple[str, int], dict[str, object]] = {}
    aggregation_status = "not_started"
    summary_status = "not_executed"
    partial_result = coverage_status == "partial"
    failed_agents: set[str] = set()
    # 新版事件会把模型代次写入 payload；旧版事件可能没有该字段。只要
    # 事件集中出现了任一明确代次，就把缺少代次的旧事件视为历史数据并
    # 忽略，避免第一次重试时旧的 completed/failed 状态覆盖当前代次。
    # 只有整组事件都没有代次信息时，才按旧版兼容策略全部纳入。
    attempts = [
        value
        for event in events
        for value in (event.payload.get("model_attempt_count"),)
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    latest_attempt = max(attempts) if attempts else None
    for event in events:
        if not event.event_type.startswith("review.model."):
            continue
        payload = event.payload
        event_attempt = payload.get("model_attempt_count")
        if latest_attempt is not None:
            if not isinstance(event_attempt, int) or isinstance(event_attempt, bool):
                continue
            if event_attempt != latest_attempt:
                continue
        raw_agent = payload.get("agent")
        agent = raw_agent if isinstance(raw_agent, str) else None
        if event.event_type.endswith("workflow_partial"):
            partial_result = True
            values = payload.get("failed_agents")
            if isinstance(values, list):
                failed_agents.update(
                    value for value in values if isinstance(value, str)
                )
            continue
        if event.event_type.endswith("aggregation_completed"):
            aggregation_status = str(payload.get("aggregation_status") or "completed")
            continue
        if event.event_type.endswith("summary_skipped"):
            summary_status = "skipped"
            statuses["summary"] = "not_executed"
            continue
        if event.event_type.endswith("summary_completed") or event.event_type.endswith(
            "summary_failed"
        ):
            raw_status = payload.get("agent_status")
            summary_status = (
                "completed" if raw_status == "completed" else "failed"
            )
            statuses["summary"] = summary_status
            if summary_status == "failed":
                failed_agents.add("summary")
            summaries["summary"] = {
                key: payload[key]
                for key in ("verdict", "summary", "checked_areas", "finding_count")
                if key in payload
            }
            continue
        if agent is None or agent == "summary":
            continue
        if event.event_type.endswith("agent_completed"):
            statuses[agent] = (
                "not_applicable"
                if payload.get("status") == "not_applicable"
                else "completed"
            )
            summaries[agent] = {
                key: payload[ key ]
                for key in ("verdict", "summary", "checked_areas", "finding_count")
                if key in payload
            }
        elif event.event_type.endswith("agent_not_applicable"):
            statuses[agent] = "not_applicable"
            failed_agents.discard(agent)
        elif event.event_type.endswith("agent_failed"):
            if payload.get("status") == "disabled":
                statuses[agent] = "disabled"
                failed_agents.discard(agent)
            else:
                statuses[agent] = "failed"
                failed_agents.add(agent)
        elif event.event_type.endswith("batch_failed"):
            statuses[agent] = "partial"
            failed_agents.add(agent)
            number = payload.get("batch_number")
            if isinstance(number, int):
                failed_batches[(agent, number)] = {
                    "agent": agent,
                    "batch_number": number,
                    "error_code": payload.get("error_code"),
                    "error_message": payload.get("error_message"),
                    "retryable": payload.get("error_retryable"),
                }
        elif event.event_type.endswith("request_started") or event.event_type.endswith(
            "batch_started"
        ):
            if statuses.get(agent) not in {"completed", "failed"}:
                statuses[agent] = "running"
    if summary_status == "not_executed" and model_completed:
        summary_status = "skipped"
    if aggregation_status == "not_started" and model_completed:
        aggregation_status = "completed"
    return (
        statuses,
        summaries,
        aggregation_status,
        summary_status,
        partial_result,
        tuple(sorted(failed_agents)),
        tuple(failed_batches[key] for key in sorted(failed_batches)),
    )


class SqlAlchemyReviewManagementRepository(ReviewManagementRepository):
    """以固定数量的有界查询读取详情，并在短事务内执行人工动作。"""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], object] | None = None,
        publisher: Callable[[StoredReviewDetails], None] | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory or uuid4
        self._publisher = publisher

    def change_token(
        self,
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
        self,
        review_run_id: str,
        *,
        finding_limit: int = 50,
        finding_cursor: FindingCursor | None = None,
        finding_adjudication_status: str | None = None,
        scope: ResourceScope | None = None,
    ) -> StoredReviewDetails:
        """读取一条运行、计划、模型调用及其有界子资源快照。

        主记录、Finding、Finding 全量聚合、CI、文件覆盖汇总、评测门槛和事件最多
        使用七次查询；列表子查询都带 ``LIMIT``，循环只负责转换已取回的行。
        """

        if not 1 <= finding_limit <= 200:
            raise ValueError("finding limit must be between 1 and 200")

        with self._sessions() as session:
            try:
                row = session.execute(
                    select(
                        ReviewRunRecord.id.label("review_run_id"),
                        ReviewTaskRecord.id.label("review_task_id"),
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
                        PullRequestVersionRecord.author_login.label(
                            "pr_author_login"
                        ),
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
                ).mappings().one_or_none()
                if row is None:
                    raise ReviewNotFoundError("审查任务不存在")

                findings, finding_has_more = self._load_findings(
                    session,
                    review_run_id,
                    limit=finding_limit,
                    cursor=finding_cursor,
                    adjudication_status=finding_adjudication_status,
                )
                finding_counts = self._load_finding_counts(session, review_run_id)
                evaluation_gates = self._load_evaluation_gates(
                    session,
                    row["repository_id"],
                )
                version_id = row["pr_version_id"]
                ci_checks = self._load_ci_checks(session, version_id)
                plan_file_decisions = self._load_plan_file_decisions(
                    session,
                    row["review_plan_id"],
                )
                events = self._load_events(session, review_run_id)
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
                    model_review_completed_at=_as_utc(
                        row["model_review_completed_at"]
                    ),
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
                )
            except ReviewNotFoundError:
                raise
            except (SQLAlchemyError, ValueError, TypeError) as exc:
                raise ReviewManagementPersistenceError(
                    "review details could not be loaded"
                ) from exc

    def get_identity_target(
        self,
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

    def save_pull_request_identity(
        self,
        review_run_id: str,
        snapshot: PullRequestSnapshot,
        *,
        actor: str,
        request_id: str,
        scope: ResourceScope | None = None,
    ) -> None:
        """以幂等短事务保存 GitHub PR 作者、链接和分支信息。"""

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            raise ReviewIdentitySyncConflictError("操作幂等键不能为空")
        digest = sha256(normalized_request_id.encode("utf-8")).hexdigest()
        event_key = f"review.identity.sync:{review_run_id}:{digest}"
        with self._sessions() as session:
            try:
                existing = session.scalar(
                    select(OutboxEventRecord.id)
                    .join(
                        ReviewRunRecord,
                        and_(
                            OutboxEventRecord.aggregate_type == "review_run",
                            OutboxEventRecord.aggregate_id == ReviewRunRecord.id,
                        ),
                    )
                    .where(
                        OutboxEventRecord.event_key == event_key,
                        resource_predicate(
                            scope,
                            installation_column=ReviewRunRecord.installation_id,
                            repository_column=ReviewRunRecord.repository,
                            repository_key_column=ReviewRunRecord.repository_key,
                        ),
                    )
                )
                if existing is not None:
                    return
                row = session.execute(
                    select(ReviewRunRecord, ReviewTaskRecord)
                    .join(
                        ReviewTaskRecord,
                        ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
                    )
                    .where(ReviewRunRecord.id == review_run_id)
                    .where(
                        resource_predicate(
                            scope,
                            installation_column=ReviewRunRecord.installation_id,
                            repository_column=ReviewRunRecord.repository,
                            repository_key_column=ReviewRunRecord.repository_key,
                        )
                    )
                    .with_for_update()
                ).one_or_none()
                if row is None:
                    raise ReviewNotFoundError("审查任务不存在")
                run, _task = row
                if (
                    snapshot.repository_id != run.repository_id
                    or snapshot.repository != run.repository
                    or snapshot.pull_request_number != run.pull_request_number
                ):
                    raise ReviewIdentitySyncConflictError(
                        "GitHub PR 身份与任务不一致"
                    )
                now = self._clock()
                version = session.scalar(
                    select(PullRequestVersionRecord)
                    .where(
                        PullRequestVersionRecord.review_version_key
                        == run.review_version_key
                    )
                    .with_for_update()
                )
                if version is not None and version.identity_fetched_at is not None:
                    return
                if version is None:
                    installation = session.get(
                        GitHubInstallationRecord,
                        run.installation_id,
                    )
                    if installation is None:
                        session.add(
                            GitHubInstallationRecord(
                                id=run.installation_id,
                                created_at=now,
                                last_seen_at=now,
                            )
                        )
                        session.flush()
                    else:
                        installation.last_seen_at = now
                    version = PullRequestVersionRecord(
                        id=str(self._uuid_factory()),
                        review_version_key=run.review_version_key,
                        installation_id=run.installation_id,
                        repository_id=run.repository_id,
                        repository=run.repository,
                        pull_request_number=run.pull_request_number,
                        head_sha=run.head_sha,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                    session.add(version)
                    session.flush()
                version.author_login = snapshot.author_login
                version.html_url = snapshot.html_url
                version.head_repository = snapshot.head_repository
                version.head_ref = snapshot.head_ref
                version.base_repository = snapshot.base_repository
                version.base_ref = snapshot.base_ref
                version.identity_fetched_at = now
                version.last_seen_at = now
                session.add(
                    OutboxEventRecord(
                        id=str(self._uuid_factory()),
                        event_key=event_key,
                        aggregate_type="review_run",
                        aggregate_id=review_run_id,
                        event_type="review.github.identity_synced",
                        payload={
                            "actor": actor,
                            "author_login": snapshot.author_login,
                            "html_url": snapshot.html_url,
                            "head_repository": snapshot.head_repository,
                            "head_ref": snapshot.head_ref,
                            "base_repository": snapshot.base_repository,
                            "base_ref": snapshot.base_ref,
                        },
                        occurred_at=now,
                        publish_attempts=0,
                    )
                )
                session.commit()
            except (ReviewNotFoundError, ReviewIdentitySyncConflictError):
                session.rollback()
                raise
            except IntegrityError as exc:
                session.rollback()
                # 同一版本的另一个运行可能并发创建版本行；只要对方已经完成身份
                # 同步，本次请求的业务结果也已达成。其他约束冲突仍作为持久化
                # 故障返回，不能把未知 IntegrityError 伪装成成功。
                recovered = session.execute(
                    select(
                        OutboxEventRecord.id,
                        PullRequestVersionRecord.identity_fetched_at,
                    )
                    .select_from(ReviewRunRecord)
                    .outerjoin(
                        PullRequestVersionRecord,
                        PullRequestVersionRecord.review_version_key
                        == ReviewRunRecord.review_version_key,
                    )
                    .outerjoin(
                        OutboxEventRecord,
                        OutboxEventRecord.event_key == event_key,
                    )
                    .where(ReviewRunRecord.id == review_run_id)
                ).one_or_none()
                if recovered is not None and (
                    recovered.id is not None
                    or recovered.identity_fetched_at is not None
                ):
                    return
                raise ReviewManagementPersistenceError(
                    "review identity could not be saved"
                ) from exc
            except (SQLAlchemyError, ValueError, TypeError) as exc:
                session.rollback()
                raise ReviewManagementPersistenceError(
                    "review identity could not be saved"
                ) from exc

    @staticmethod
    def _load_findings(
        session: Session,
        review_run_id: str,
        *,
        limit: int,
        cursor: FindingCursor | None,
        adjudication_status: str | None,
    ) -> tuple[tuple[StoredFinding, ...], bool]:
        query = select(
                ReviewFindingRecord.id,
                ReviewFindingRecord.fingerprint,
                ReviewFindingRecord.head_sha,
                ReviewFindingRecord.severity,
                ReviewFindingRecord.category,
                ReviewFindingRecord.title,
                ReviewFindingRecord.evidence,
                ReviewFindingRecord.impact,
                ReviewFindingRecord.suggestion,
                ReviewFindingRecord.required_test,
                ReviewFindingRecord.confidence,
                ReviewFindingRecord.verification_status,
                ReviewFindingRecord.evidence_verification_status,
                ReviewFindingRecord.evidence_verification_reason,
                ReviewFindingRecord.evidence_verified_at,
                ReviewFindingRecord.adjudication_status,
                ReviewFindingRecord.lifecycle_status,
                ReviewFindingRecord.occurrence_count,
                ReviewFindingRecord.previous_review_run_id,
                ReviewFindingRecord.location_file,
                ReviewFindingRecord.location_start_line,
                ReviewFindingRecord.location_end_line,
                ReviewFindingRecord.location_side,
                ReviewFindingRecord.location_in_diff,
                ReviewFindingRecord.location_symbol,
                ReviewFindingRecord.rule_reference,
                ReviewFindingRecord.reviewed_at,
                ReviewFindingRecord.reviewed_by,
                ReviewFindingRecord.created_at,
            )
        query = query.where(ReviewFindingRecord.review_run_id == review_run_id)
        if adjudication_status is not None:
            query = query.where(
                ReviewFindingRecord.adjudication_status == adjudication_status
            )
        if cursor is not None:
            query = query.where(
                or_(
                    ReviewFindingRecord.created_at > cursor.created_at,
                    and_(
                        ReviewFindingRecord.created_at == cursor.created_at,
                        ReviewFindingRecord.id > cursor.finding_id,
                    ),
                )
            )
        raw_rows = session.execute(
            query.order_by(
                ReviewFindingRecord.created_at.asc(),
                ReviewFindingRecord.id.asc(),
            ).limit(limit + 1)
        ).mappings().all()
        has_more = len(raw_rows) > limit
        rows = raw_rows[:limit]
        findings = tuple(
            StoredFinding(
                id=row["id"],
                fingerprint=row["fingerprint"],
                head_sha=row["head_sha"],
                severity=row["severity"],
                category=row["category"],
                title=row["title"],
                evidence=row["evidence"],
                impact=row["impact"],
                suggestion=row["suggestion"],
                required_test=row["required_test"],
                confidence=float(row["confidence"]),
                verification_status=row["verification_status"],
                evidence_verification_status=row["evidence_verification_status"],
                evidence_verification_reason=row["evidence_verification_reason"],
                evidence_verified_at=_as_utc(row["evidence_verified_at"]),
                adjudication_status=row["adjudication_status"],
                lifecycle_status=row["lifecycle_status"],
                occurrence_count=row["occurrence_count"],
                previous_review_run_id=row["previous_review_run_id"],
                location_file=row["location_file"],
                location_start_line=row["location_start_line"],
                location_end_line=row["location_end_line"],
                location_side=row["location_side"],
                location_in_diff=bool(row["location_in_diff"]),
                location_symbol=row["location_symbol"],
                rule_reference=row["rule_reference"],
                reviewed_at=_as_utc(row["reviewed_at"]),
                reviewed_by=row["reviewed_by"],
                created_at=_required_utc(
                    row["created_at"],
                    "review_finding.created_at",
                ),
            )
            for row in rows
        )
        return findings, has_more

    @staticmethod
    def _load_finding_counts(
        session: Session,
        review_run_id: str,
    ) -> StoredFindingCounts:
        """用一次条件聚合计算整次审查的 Finding 统计，不依赖当前页。"""

        def tally(condition):
            return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

        row = session.execute(
            select(
                func.count(ReviewFindingRecord.id).label("total"),
                tally(
                    ReviewFindingRecord.verification_status == "verified"
                ).label("location_verified"),
                tally(
                    ReviewFindingRecord.verification_status == "rejected"
                ).label("location_rejected"),
                tally(
                    ReviewFindingRecord.verification_status == "unverified"
                ).label("location_unverified"),
                tally(ReviewFindingRecord.adjudication_status == "valid").label(
                    "valid"
                ),
                tally(
                    ReviewFindingRecord.adjudication_status == "false_positive"
                ).label("false_positive"),
                tally(
                    ReviewFindingRecord.adjudication_status == "duplicate"
                ).label("duplicate"),
                tally(
                    ReviewFindingRecord.adjudication_status == "out_of_scope"
                ).label("out_of_scope"),
                tally(
                    ReviewFindingRecord.adjudication_status == "known_issue"
                ).label("known_issue"),
                tally(
                    ReviewFindingRecord.adjudication_status == "unreviewed"
                ).label("unreviewed"),
                tally(ReviewFindingRecord.lifecycle_status == "new").label("new"),
                tally(
                    ReviewFindingRecord.lifecycle_status == "still_present"
                ).label("still_present"),
                tally(
                    ReviewFindingRecord.lifecycle_status == "reintroduced"
                ).label("reintroduced"),
            ).where(ReviewFindingRecord.review_run_id == review_run_id)
        ).mappings().one()
        return StoredFindingCounts(
            total=int(row["total"] or 0),
            location_verified=int(row["location_verified"] or 0),
            location_rejected=int(row["location_rejected"] or 0),
            location_unverified=int(row["location_unverified"] or 0),
            valid=int(row["valid"] or 0),
            false_positive=int(row["false_positive"] or 0),
            duplicate=int(row["duplicate"] or 0),
            out_of_scope=int(row["out_of_scope"] or 0),
            known_issue=int(row["known_issue"] or 0),
            unreviewed=int(row["unreviewed"] or 0),
            new=int(row["new"] or 0),
            still_present=int(row["still_present"] or 0),
            reintroduced=int(row["reintroduced"] or 0),
        )

    @staticmethod
    def _load_evaluation_gates(
        session: Session,
        repository_id: int,
    ) -> tuple[StoredEvaluationGate, ...]:
        """用一次有界索引查询计算各风险域最近样本的发布准入。

        不能先对整个仓库做窗口排序再截断：评测表会随人工裁决持续增长，
        那种写法的扫描量会变成无界。每个固定风险域先在数据库内取最近
        ``recent_sample_limit`` 行，再 ``UNION ALL`` 成一条语句；因此查询次数
        始终为 O(1)，返回行数最多为风险域数量乘以样本上限。
        """

        policy = EvaluationGatePolicy()
        bounded_by_category = []
        for category in FindingCategory:
            # 先物化每个类别的 LIMIT 子查询，再拼成一条 SQL；循环只构造语句，
            # 不在循环内访问数据库，避免 N+1 查询。
            recent = (
                select(
                    FindingEvaluationRecord.category,
                    FindingEvaluationRecord.severity,
                    FindingEvaluationRecord.verdict,
                    FindingEvaluationRecord.adjudicated_at,
                    FindingEvaluationRecord.finding_id,
                )
                .where(
                    FindingEvaluationRecord.repository_id == repository_id,
                    FindingEvaluationRecord.category == category.value,
                )
                .order_by(
                    FindingEvaluationRecord.adjudicated_at.desc(),
                    FindingEvaluationRecord.finding_id.desc(),
                )
                .limit(policy.recent_sample_limit)
                .subquery()
            )
            bounded_by_category.append(
                select(
                    recent.c.category,
                    recent.c.severity,
                    recent.c.verdict,
                )
            )
        rows = session.execute(union_all(*bounded_by_category)).mappings()

        counters: dict[str, dict[str, int]] = {
            category.value: {
                "sample_count": 0,
                "valid_count": 0,
                "false_positive_count": 0,
                "duplicate_count": 0,
                "out_of_scope_count": 0,
                "known_issue_count": 0,
                "high_severity_sample_count": 0,
                "high_severity_false_positive_count": 0,
                "high_severity_duplicate_count": 0,
                "high_severity_out_of_scope_count": 0,
                "high_severity_known_issue_count": 0,
            }
            for category in FindingCategory
        }
        high_severities = {Severity.CRITICAL.value, Severity.HIGH.value}
        for row in rows:
            row_category = row["category"]
            values = counters.get(row_category)
            if values is None:
                continue
            values["sample_count"] += 1
            verdict = row["verdict"]
            is_valid = verdict == FindingEvaluationVerdict.VALID.value
            if is_valid:
                values["valid_count"] += 1
            else:
                values[f"{verdict}_count"] += 1
            if row["severity"] in high_severities:
                values["high_severity_sample_count"] += 1
                if not is_valid:
                    values[f"high_severity_{verdict}_count"] += 1

        gates: list[StoredEvaluationGate] = []
        for category_value in sorted(counters):
            values = counters[category_value]
            metrics = EvaluationMetrics(
                sample_count=values["sample_count"],
                valid_count=values["valid_count"],
                false_positive_count=values["false_positive_count"],
                duplicate_count=values["duplicate_count"],
                out_of_scope_count=values["out_of_scope_count"],
                known_issue_count=values["known_issue_count"],
                high_severity_sample_count=values[
                    "high_severity_sample_count"
                ],
                high_severity_false_positive_count=values[
                    "high_severity_false_positive_count"
                ],
                high_severity_duplicate_count=values[
                    "high_severity_duplicate_count"
                ],
                high_severity_out_of_scope_count=values[
                    "high_severity_out_of_scope_count"
                ],
                high_severity_known_issue_count=values[
                    "high_severity_known_issue_count"
                ],
            )
            result = evaluate_inline_gate(metrics, policy)
            gates.append(
                StoredEvaluationGate(
                    category=category_value,
                    sample_count=metrics.sample_count,
                    valid_count=metrics.valid_count,
                    false_positive_count=metrics.false_positive_count,
                    duplicate_count=metrics.duplicate_count,
                    out_of_scope_count=metrics.out_of_scope_count,
                    known_issue_count=metrics.known_issue_count,
                    rejected_count=metrics.rejected_count,
                    high_severity_sample_count=(
                        metrics.high_severity_sample_count
                    ),
                    high_severity_false_positive_count=(
                        metrics.high_severity_false_positive_count
                    ),
                    high_severity_rejected_count=(
                        metrics.high_severity_rejected_count
                    ),
                    precision=result.precision,
                    high_severity_false_positive_rate=(
                        result.high_severity_false_positive_rate
                    ),
                    admitted=result.admitted,
                    reason=result.reason,
                )
            )
        return tuple(gates)

    @staticmethod
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

    @staticmethod
    def _load_plan_file_decisions(
        session: Session,
        review_plan_id: str | None,
    ) -> dict[str, int]:
        if review_plan_id is None:
            return {}
        rows = session.execute(
            select(
                ReviewFilePlanRecord.decision,
                func.count().label("count"),
            )
            .where(ReviewFilePlanRecord.review_plan_id == review_plan_id)
            .group_by(ReviewFilePlanRecord.decision)
            .limit(16)
        ).tuples().all()
        return {decision: int(count) for decision, count in rows}

    @staticmethod
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

    def apply_action(
        self,
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
        scope: ResourceScope | None = None,
    ) -> tuple[str, str, ExecutionStatus]:
        """在一个短事务内执行加速、重试、取消或重新审查。"""

        normalized_request_id = request_id.strip()
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
            raise ReviewActionConflictError(
                "新建最新提交审查必须提供当前 head_sha"
            )
        action_key = sha256(
            f"{review_run_id}:{action.value}:{normalized_request_id}".encode()
        ).hexdigest()
        event_key = f"review.action:{review_run_id}:{action.value}:{action_key}"
        with self._sessions() as session:
            try:
                idempotent_result = self._existing_action_result(
                    session,
                    review_run_id,
                    action,
                    event_key=event_key,
                    target_stage=target_stage,
                    retry_scope=retry_scope,
                    agent=agent,
                    batch_number=batch_number,
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
                # 第一次事件查询和业务行加锁之间可能有并发请求已经提交；
                # 锁定后必须再次检查，避免重复插入唯一 event_key 并误报 503。
                idempotent_result = self._existing_action_result(
                    session,
                    review_run_id,
                    action,
                    event_key=event_key,
                    target_stage=target_stage,
                    retry_scope=retry_scope,
                    agent=agent,
                    batch_number=batch_number,
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
                            raise ReviewActionConflictError(
                                "审查版本已变化，请刷新后重试"
                            )
                else:
                    normalized_head_sha = run.head_sha
                if state_version is not None:
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
                        raise ReviewActionConflictError(
                            "审查状态已变化，请刷新后重试"
                        )
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

                if failed_node_action:
                    self._prepare_failed_node_retry(
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
                                    ReviewFindingRecord.review_run_id
                                    == review_run_id,
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
                        task.execution_status = self._paused_execution_status(
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
                        task.execution_status = self._resumed_execution_status(
                            result.after,
                            task.execution_status,
                        ).value
                        run.execution_status = task.execution_status
                    elif action is ReviewAction.RETRY_STAGE:
                        if target is None:
                            target = result.after
                        self._prepare_stage_retry(
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

                if action is ReviewAction.RERUN or new_review_action:
                    if current is ExecutionStatus.RUNNING:
                        raise ReviewActionConflictError("任务正在处理中，暂时不能重新审查")
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
                        f"manual-rerun:{review_run_id}:"
                        f"{sha256(normalized_request_id.encode('utf-8')).hexdigest()}"
                    )
                    legacy_rerun_key = (
                        f"manual-rerun:{review_run_id}:{request_id}"
                    )[:200]
                    existing_rerun = session.scalar(
                        select(ReviewRunRecord.id).where(
                            ReviewRunRecord.idempotency_key.in_(
                                (rerun_key, legacy_rerun_key)
                            )
                        )
                    )
                    if existing_rerun is not None:
                        existing_task = session.scalar(
                            select(ReviewTaskRecord.id).where(
                                ReviewTaskRecord.review_run_id == existing_rerun
                            )
                        )
                        if existing_task is None:
                            raise ReviewManagementPersistenceError(
                                "重新审查任务记录不完整"
                            )
                        return existing_rerun, existing_task, ExecutionStatus.QUEUED
                    session.add_all(
                        [
                            ReviewRunRecord(
                                id=new_run_id,
                                review_version_key=new_review_version_key,
                                installation_id=run.installation_id,
                                repository_id=run.repository_id,
                                repository=run.repository,
                                pull_request_number=run.pull_request_number,
                                head_sha=normalized_head_sha,
                                execution_status=ExecutionStatus.QUEUED.value,
                                workflow_status=ExecutionStatus.QUEUED.value,
                                review_conclusion=None,
                                coverage_status="unknown",
                                idempotency_key=rerun_key,
                                request_fingerprint=sha256(
                                    f"{run.request_fingerprint}:{normalized_head_sha}".encode()
                                ).hexdigest(),
                                created_at=now,
                                updated_at=now,
                            ),
                            ReviewTaskRecord(
                                id=new_task_id,
                                review_run_id=new_run_id,
                                execution_status=ExecutionStatus.QUEUED.value,
                                workflow_status=ExecutionStatus.QUEUED.value,
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
                                    "trigger": "manual_rerun",
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
                    return new_run_id, new_task_id, ExecutionStatus.QUEUED

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
                    }:
                        raise ReviewActionConflictError("当前状态不能取消")
                    task.execution_status = ExecutionStatus.CANCELLED.value
                    task.lease_owner = None
                    task.lease_expires_at = None
                    task.claimed_from_status = None
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
                        self._prepare_stage_retry(
                            session,
                            run,
                            task,
                            plan,
                            ExecutionStatus.AGENT_BATCHES,
                        )
                    new_status = (
                        ExecutionStatus.READY_FOR_REVIEW
                        if plan is not None
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

    @staticmethod
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
            action in {ReviewAction.RERUN, ReviewAction.NEW_REVIEW}
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
                ExecutionStatus.QUEUED,
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

    @staticmethod
    def _prepare_stage_retry(
        session: Session,
        run: ReviewRunRecord,
        task: ReviewTaskRecord,
        plan: ReviewPlanRecord | None,
        target_stage: ExecutionStatus,
    ) -> None:
        """清除目标阶段及其后续产物，同时保留可安全复用的前序结果。"""

        if plan is None:
            raise ReviewActionConflictError("指定阶段重审需要已有审查计划")

        plan_id = plan.id
        session.execute(
            delete(ReviewFindingRecord).where(
                ReviewFindingRecord.review_plan_id == plan_id
            )
        )
        session.execute(
            delete(ModelCallRecord).where(ModelCallRecord.review_plan_id == plan_id)
        )

        if target_stage in {ExecutionStatus.CI, ExecutionStatus.PLANNING}:
            # 规划是不可变快照。回到 CI 或规划时必须删除旧计划及全部子资源，
            # 让 Worker 基于当前 PR 快照重新生成，而不是误用旧计划。
            session.execute(
                delete(ModelReviewBatchRecord).where(
                    ModelReviewBatchRecord.review_plan_id == plan_id
                )
            )
            session.execute(
                delete(ReviewFilePlanRecord).where(
                    ReviewFilePlanRecord.review_plan_id == plan_id
                )
            )
            session.execute(
                delete(ReviewUnitRecord).where(
                    ReviewUnitRecord.review_plan_id == plan_id
                )
            )
            session.execute(
                delete(ReviewPlanRuleRecord).where(
                    ReviewPlanRuleRecord.review_plan_id == plan_id
                )
            )
            session.delete(plan)
            run.coverage_status = "unknown"
        elif target_stage is ExecutionStatus.AGENT_BATCHES:
            session.execute(
                delete(ModelReviewBatchRecord).where(
                    ModelReviewBatchRecord.review_plan_id == plan_id
                )
            )
            plan.model_review_completed_at = None
        elif target_stage is ExecutionStatus.AGGREGATING:
            reusable_agents = tuple(
                agent.value
                for agent in (
                    ReviewAgent.SECURITY,
                    ReviewAgent.CONVENTION,
                    ReviewAgent.LOGIC,
                )
            )
            session.execute(
                delete(ModelReviewBatchRecord).where(
                    ModelReviewBatchRecord.review_plan_id == plan_id,
                    ModelReviewBatchRecord.agent.not_in(reusable_agents),
                )
            )
            plan.model_review_completed_at = None
        else:
            raise ReviewActionConflictError("重审目标阶段无效")

        run.review_conclusion = None
        if target_stage is ExecutionStatus.CI:
            task.ci_poll_count = 0
            task.ci_wait_started_at = None
            task.ci_deadline_at = None

    @staticmethod
    def _prepare_failed_node_retry(
        session: Session,
        run: ReviewRunRecord,
        task: ReviewTaskRecord,
        plan: ReviewPlanRecord | None,
        *,
        agent: str | None,
        batch_number: int | None,
        now: datetime,
    ) -> None:
        """只恢复失败/未完成批次，保留所有成功批次和 Finding。

        失败节点重试必须与阶段重试区分：阶段重试会清理后续产物，而这里仅
        对目标 Agent/批次做一次有界读取和一次批量 UPDATE。没有已持久化批次
        时也允许继续，让 Worker 首次规划该 Agent；这覆盖了请求在批次表写入
        前失败的情况。
        """

        if plan is None:
            raise ReviewActionConflictError("失败节点重试需要已有审查计划")
        if run.coverage_status == "stale":
            raise ReviewActionConflictError("该任务已被新提交替代")
        summary_retry = agent == ReviewAgent.SUMMARY.value and batch_number is None
        summary_failed = _latest_summary_failed(session, run.id)
        if agent == ReviewAgent.SUMMARY.value and batch_number is not None:
            raise ReviewActionConflictError("汇总 Agent 不支持批次号")
        if summary_retry and not summary_failed:
            raise ReviewActionConflictError("汇总 Agent 当前没有可重试的失败节点")
        rows = list(
            session.scalars(
                select(ModelReviewBatchRecord)
                .where(ModelReviewBatchRecord.review_plan_id == plan.id)
                .order_by(
                    ModelReviewBatchRecord.agent.asc(),
                    ModelReviewBatchRecord.batch_number.asc(),
                )
                .limit(3001)
                .with_for_update()
            )
        )
        if len(rows) > 3000:
            raise ReviewActionConflictError("模型批次数量超过安全上限")
        selected = [
            row
            for row in rows
            if (agent is None or row.agent == agent)
            and (batch_number is None or row.batch_number == batch_number)
            and row.status != ModelBatchStatus.SUCCEEDED.value
        ]
        if batch_number is not None and not any(
            row.batch_number == batch_number
            and (agent is None or row.agent == agent)
            for row in rows
        ):
            raise ReviewActionConflictError("指定模型批次不存在")
        # 汇总 Agent 不建立普通批次行；它的失败状态由 summary_completed
        # 事件表达。只有明确存在该失败事件时，才允许在没有可更新批次的
        # 情况下继续，并把计划重新打开给 Worker 执行强制汇总。
        summary_only_retry = (
            summary_failed
            and batch_number is None
            and (agent is None or agent == ReviewAgent.SUMMARY.value)
            and not selected
        )
        if not selected and rows and not summary_only_retry:
            raise ReviewActionConflictError("指定节点没有可重试的失败批次")
        live_running = []
        now_utc = _as_utc(now)
        for row in selected:
            lease_expires_at = row.lease_expires_at
            normalized_expires_at = (
                _as_utc(lease_expires_at) if lease_expires_at is not None else None
            )
            if (
                row.status == ModelBatchStatus.RUNNING.value
                and normalized_expires_at is not None
                and now_utc is not None
                and normalized_expires_at > now_utc
            ):
                live_running.append(row)
        if live_running:
            raise ReviewActionConflictError("指定批次正在执行，请等待其完成")
        selected_ids = tuple(row.id for row in selected)
        if selected_ids:
            session.execute(
                update(ModelReviewBatchRecord)
                .where(ModelReviewBatchRecord.id.in_(selected_ids))
                .values(
                    status=ModelBatchStatus.PENDING.value,
                    # 人工重试是一次新的批次尝试窗口。若保留自动重试已
                    # 累积的计数，Worker 会在重新领取前立即判定“达到上限”，
                    # 用户点击重试却仍然不会发出请求。截断拆分检查点位于
                    # error_details 中，未被清除，因此成功子批次仍可复用。
                    attempt_count=0,
                    available_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    error_code=None,
                    error_message=None,
                    response_status=None,
                    duration_ms=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
        if summary_only_retry:
            plan.model_review_completed_at = None
        task.execution_status = ExecutionStatus.READY_FOR_REVIEW.value
        task.workflow_status = ExecutionStatus.AGENT_BATCHES.value
        task.workflow_paused_from = None
        task.available_at = now
        task.last_error = None
        task.last_error_code = None
        task.last_error_retryable = None
        task.last_error_details = None
        task.lease_owner = None
        task.lease_expires_at = None
        task.claimed_from_status = None
        task.updated_at = now
        run.execution_status = ExecutionStatus.READY_FOR_REVIEW.value
        run.workflow_status = ExecutionStatus.AGENT_BATCHES.value
        run.workflow_paused_from = None
        run.review_conclusion = None
        # 汇总失败不代表三路审查覆盖不完整；重试期间保留完整覆盖标记，
        # 只有确实存在失败/未完成批次时才显示部分覆盖。
        run.coverage_status = "complete" if summary_only_retry else "partial"
        run.updated_at = now

    @staticmethod
    def _paused_execution_status(
        workflow_status: ExecutionStatus,
        current_execution: str,
    ) -> ExecutionStatus:
        """把暂停中的 DAG 节点映射到不会被旧 Worker 继续执行的队列状态。"""

        current = ExecutionStatus(current_execution)
        if workflow_status is ExecutionStatus.CI:
            # 上下文可能尚未读取，也可能已经进入 CI 轮询；保守地重新排队，
            # 继续时会重新校验 GitHub 当前快照。
            return (
                current
                if current in {
                    ExecutionStatus.QUEUED,
                    ExecutionStatus.WAITING_FOR_CI,
                }
                else ExecutionStatus.QUEUED
            )
        if workflow_status in {
            ExecutionStatus.PLANNING,
            ExecutionStatus.AGENT_BATCHES,
            ExecutionStatus.AGGREGATING,
        }:
            return ExecutionStatus.READY_FOR_REVIEW
        if current is ExecutionStatus.RUNNING:
            # 人工阶段没有可领取的队列状态；清除运行标记后以 completed 作为
            # 兼容旧读模型的静态承载状态，真正节点仍由 workflow_status 表示。
            return ExecutionStatus.COMPLETED
        return current

    @staticmethod
    def _resumed_execution_status(
        workflow_status: ExecutionStatus,
        current_execution: str,
    ) -> ExecutionStatus:
        """将暂停节点恢复为对应的旧队列状态。"""

        if workflow_status is ExecutionStatus.QUEUED:
            return ExecutionStatus.QUEUED
        if workflow_status is ExecutionStatus.CI:
            return ExecutionStatus.QUEUED
        if workflow_status in {
            ExecutionStatus.PLANNING,
            ExecutionStatus.AGENT_BATCHES,
            ExecutionStatus.AGGREGATING,
        }:
            return ExecutionStatus.READY_FOR_REVIEW
        # awaiting_approval/awaiting_publish 都是人工节点，不应重新进入 Worker
        # 队列；保留原静态状态，若旧值是 running 则改成 completed。
        current = ExecutionStatus(current_execution)
        return ExecutionStatus.COMPLETED if current is ExecutionStatus.RUNNING else current

    def publish(
        self,
        review_run_id: str,
        *,
        actor: str,
        request_id: str,
        state_version: str | None = None,
        head_sha: str | None = None,
        scope: ResourceScope | None = None,
    ) -> tuple[str, str, ExecutionStatus]:
        """批准后才允许的人工发布；外部 GitHub 调用永远在事务之外。"""

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            raise ReviewActionConflictError("操作幂等键不能为空")
        request_digest = sha256(normalized_request_id.encode("utf-8")).hexdigest()
        event_key = f"review.publish:{review_run_id}:{request_digest}"
        completed_event_key = f"{event_key}:completed"
        with self._sessions() as session:
            try:
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
                if head_sha is not None:
                    try:
                        if normalize_sha(head_sha) != run.head_sha:
                            raise ReviewActionConflictError(
                                "审查版本已变化，请刷新后重试"
                            )
                    except ValueError as exc:
                        raise ReviewActionConflictError("head_sha 格式无效") from exc
                if state_version is not None:
                    latest_event_id = session.scalar(
                        select(OutboxEventRecord.id)
                        .where(OutboxEventRecord.aggregate_id == review_run_id)
                        .order_by(
                            OutboxEventRecord.occurred_at.desc(),
                            OutboxEventRecord.id.desc(),
                        )
                        .limit(1)
                    )
                    if state_version != _review_change_token(
                        run.updated_at,
                        task.updated_at,
                        latest_event_id,
                    ):
                        raise ReviewActionConflictError(
                            "审查状态已变化，请刷新后重试"
                        )
                if self._publisher is None:
                    raise ReviewPublishUnavailableError("GitHub 人工发布器尚未配置")
                if run.coverage_status in {"partial", "stale"}:
                    raise ReviewActionConflictError(
                        "当前审查覆盖不完整，完成失败节点后才能发布"
                    )
                existing_started = session.execute(
                    select(OutboxEventRecord.payload).where(
                        OutboxEventRecord.event_key == event_key
                    )
                ).scalar_one_or_none()
                existing_completed = session.execute(
                    select(OutboxEventRecord.payload).where(
                        OutboxEventRecord.event_key == completed_event_key
                    )
                ).scalar_one_or_none()
                if (
                    isinstance(existing_completed, dict)
                    and existing_completed.get("published") is True
                ):
                    return run.id, task.id, ExecutionStatus.COMPLETED
                current = ExecutionStatus(run.workflow_status or run.execution_status)
                now = self._clock()
                if current is ExecutionStatus.PUBLISHING:
                    updated_at = _as_utc(run.updated_at)
                    if (
                        updated_at is not None
                        and now - updated_at < _PUBLISH_RECOVERY_AFTER
                    ):
                        raise ReviewActionConflictError("GitHub 发布正在进行中")
                elif current is not ExecutionStatus.AWAITING_PUBLISH:
                    raise ReviewActionConflictError("只有批准后的审查可以发布")
                attempt_token = str(self._uuid_factory())
                run.workflow_status = ExecutionStatus.PUBLISHING.value
                run.publish_attempt_token = attempt_token
                task.workflow_status = ExecutionStatus.PUBLISHING.value
                run.updated_at = now
                task.updated_at = now
                attempt_event_key = (
                    event_key
                    if existing_started is None
                    else f"{event_key}:retry:{attempt_token}"
                )
                session.add(
                    OutboxEventRecord(
                        id=str(self._uuid_factory()),
                        event_key=attempt_event_key,
                        aggregate_type="review_run",
                        aggregate_id=review_run_id,
                        event_type="review.manual.publish_started",
                        payload={
                            "actor": actor,
                            "request_id_hash": request_digest,
                            "attempt_token": attempt_token,
                            "recovered": current is ExecutionStatus.PUBLISHING,
                        },
                        occurred_at=now,
                        publish_attempts=0,
                    )
                )
                session.commit()
            except (ReviewNotFoundError, ReviewActionConflictError):
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise ReviewManagementPersistenceError(
                    "review publish state could not be persisted"
                ) from exc

        try:
            details = self.get(
                review_run_id,
                finding_limit=200,
                finding_adjudication_status=FindingDecision.VALID.value,
                scope=scope,
            )
            if details.finding_has_more:
                raise ReviewActionConflictError(
                    "有效 Finding 超过 200 条，请先减少发布范围"
                )
            self._publisher(details)
        except ReviewActionConflictError as exc:
            self._mark_publish_failed(
                review_run_id,
                event_key,
                attempt_token,
                actor,
                str(exc),
                scope=scope,
            )
            raise
        except Exception as exc:
            self._mark_publish_failed(
                review_run_id,
                event_key,
                attempt_token,
                actor,
                str(exc),
                scope=scope,
            )
            raise ReviewPublishUnavailableError("GitHub 发布失败，请稍后重试") from exc

        with self._sessions() as session:
            try:
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
                completed_event_id = session.scalar(
                    select(OutboxEventRecord.id).where(
                        OutboxEventRecord.event_key == completed_event_key
                    )
                )
                if completed_event_id is not None:
                    return run.id, task.id, ExecutionStatus.COMPLETED
                now = self._clock()
                current_workflow_status = ExecutionStatus(
                    run.workflow_status or run.execution_status
                )
                if (
                    current_workflow_status is not ExecutionStatus.PUBLISHING
                    or run.publish_attempt_token != attempt_token
                ):
                    # 外部调用已经返回，但本地状态在调用期间被其他尝试或
                    # 新提交替换；绝不能把旧结果写成 completed。令牌匹配时
                    # 清掉残留标记，避免管理端误判仍有发布在途。
                    if run.publish_attempt_token == attempt_token:
                        run.publish_attempt_token = None
                        session.add(
                            OutboxEventRecord(
                                id=str(self._uuid_factory()),
                                event_key=(
                                    f"{completed_event_key}:conflict:{attempt_token}"
                                )[:200],
                                aggregate_type="review_run",
                                aggregate_id=review_run_id,
                                event_type="review.manual.publish_result_conflict",
                                payload={
                                    "actor": actor,
                                    "attempt_token": attempt_token,
                                    "published": True,
                                    "current_workflow_status": (
                                        current_workflow_status.value
                                    ),
                                },
                                occurred_at=now,
                                publish_attempts=0,
                            )
                        )
                        session.commit()
                    else:
                        session.rollback()
                    raise ReviewActionConflictError(
                        "发布结果与当前任务状态冲突，请先核对 GitHub 后再处理"
                    )
                run.publish_attempt_token = None
                run.workflow_status = ExecutionStatus.COMPLETED.value
                task.workflow_status = ExecutionStatus.COMPLETED.value
                run.updated_at = now
                task.updated_at = now
                session.add(
                    OutboxEventRecord(
                        id=str(self._uuid_factory()),
                        event_key=completed_event_key,
                        aggregate_type="review_run",
                        aggregate_id=review_run_id,
                        event_type="review.manual.publish_completed",
                        payload={
                            "actor": actor,
                            "published": True,
                            "attempt_token": attempt_token,
                        },
                        occurred_at=now,
                        publish_attempts=0,
                    )
                )
                session.commit()
                return run.id, task.id, ExecutionStatus.COMPLETED
            except ReviewNotFoundError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise ReviewManagementPersistenceError(
                    "review publish result could not be persisted"
                ) from exc

    def _mark_publish_failed(
        self,
        review_run_id: str,
        event_key: str,
        attempt_token: str,
        actor: str,
        safe_reason: str,
        *,
        scope: ResourceScope | None = None,
    ) -> None:
        """发布异常只回到待发布，不把外部错误文本原样暴露给客户端。"""

        with self._sessions() as session:
            try:
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
                    return
                run, task = row
                now = self._clock()
                if ExecutionStatus(
                    run.workflow_status or run.execution_status
                ) is not ExecutionStatus.PUBLISHING or run.publish_attempt_token != attempt_token:
                    return
                run.publish_attempt_token = None
                run.workflow_status = ExecutionStatus.AWAITING_PUBLISH.value
                task.workflow_status = ExecutionStatus.AWAITING_PUBLISH.value
                run.updated_at = now
                task.updated_at = now
                session.add(
                    OutboxEventRecord(
                        id=str(self._uuid_factory()),
                        event_key=f"{event_key}:failed:{attempt_token}"[:200],
                        aggregate_type="review_run",
                        aggregate_id=review_run_id,
                        event_type="review.manual.publish_failed",
                        payload={
                            "actor": actor,
                            "attempt_token": attempt_token,
                            "error_message": redact_sensitive(safe_reason)[:300],
                        },
                        occurred_at=now,
                        publish_attempts=0,
                    )
                )
                session.commit()
            except SQLAlchemyError:
                session.rollback()

    def review_finding(
        self,
        review_run_id: str,
        finding_id: str,
        decision: FindingDecision,
        *,
        actor: str,
        request_id: str,
        scope: ResourceScope | None = None,
    ) -> None:
        """保存一次人工裁决，并追加可追踪的事件。"""

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            raise ValueError("操作幂等键不能为空")
        decision_key = sha256(
            f"{review_run_id}:{finding_id}:{decision.value}:{normalized_request_id}".encode()
        ).hexdigest()
        event_key = f"review.finding.decision:{finding_id}:{decision_key}"
        with self._sessions() as session:
            try:
                # 先验证 Finding 所属运行和资源范围，再读取幂等事件；否则越权
                # 请求可能通过已存在的 event_key 观察到其他仓库的操作结果。
                scoped_finding = session.scalar(
                    select(ReviewFindingRecord.id)
                    .join(
                        ReviewRunRecord,
                        ReviewRunRecord.id == ReviewFindingRecord.review_run_id,
                    )
                    .where(
                        ReviewFindingRecord.id == finding_id,
                        ReviewFindingRecord.review_run_id == review_run_id,
                        resource_predicate(
                            scope,
                            installation_column=ReviewRunRecord.installation_id,
                            repository_column=ReviewRunRecord.repository,
                            repository_key_column=ReviewRunRecord.repository_key,
                        ),
                    )
                    .limit(1)
                )
                if scoped_finding is None:
                    raise FindingNotFoundError("候选问题不存在")
                if session.scalar(
                    select(OutboxEventRecord.id).where(
                        OutboxEventRecord.event_key == event_key
                    )
                ) is not None:
                    return
                finding_row = session.execute(
                    select(ReviewFindingRecord, ReviewRunRecord.repository_id)
                    .join(
                        ReviewRunRecord,
                        ReviewRunRecord.id == ReviewFindingRecord.review_run_id,
                    )
                    .where(
                        ReviewFindingRecord.id == finding_id,
                        ReviewFindingRecord.review_run_id == review_run_id,
                        resource_predicate(
                            scope,
                            installation_column=ReviewRunRecord.installation_id,
                            repository_column=ReviewRunRecord.repository,
                            repository_key_column=ReviewRunRecord.repository_key,
                        ),
                    )
                    .with_for_update()
                ).one_or_none()
                if finding_row is None:
                    raise FindingDecisionError("候选问题不存在")
                finding, repository_id = finding_row
                # 与任务动作相同，第一次事件查询可能早于并发事务提交；
                # Finding 行锁之后复查才能把重复请求当作成功处理。
                if session.scalar(
                    select(OutboxEventRecord.id).where(
                        OutboxEventRecord.event_key == event_key
                    )
                ) is not None:
                    return
                now = self._clock()
                finding.adjudication_status = decision.value
                finding.reviewed_at = now
                finding.reviewed_by = actor[:100]
                verdict = FindingEvaluationVerdict(decision.value)
                evaluation = session.get(FindingEvaluationRecord, finding_id)
                if evaluation is None:
                    evaluation = FindingEvaluationRecord(
                        finding_id=finding_id,
                        repository_id=repository_id,
                        category=finding.category,
                        severity=finding.severity,
                        verdict=verdict.value,
                        adjudicated_at=now,
                        adjudicated_by=actor[:100],
                        updated_at=now,
                    )
                    session.add(evaluation)
                else:
                    evaluation.verdict = verdict.value
                    evaluation.adjudicated_at = now
                    evaluation.adjudicated_by = actor[:100]
                    evaluation.updated_at = now
                session.add(
                    OutboxEventRecord(
                        id=str(self._uuid_factory()),
                        event_key=event_key,
                        aggregate_type="review_run",
                        aggregate_id=review_run_id,
                        event_type="review.finding.decided",
                        payload={
                            "finding_id": finding_id,
                            "decision": decision.value,
                            "actor": actor,
                        },
                        occurred_at=now,
                        publish_attempts=0,
                    )
                )
                session.commit()
            except FindingNotFoundError:
                session.rollback()
                raise
            except FindingDecisionError as exc:
                session.rollback()
                raise FindingNotFoundError("候选问题不存在") from exc
            except SQLAlchemyError as exc:
                session.rollback()
                raise ReviewManagementPersistenceError(
                    "finding decision could not be persisted"
                ) from exc


class FindingDecisionError(LookupError):
    """内部转换用的 Finding 不存在异常。"""
