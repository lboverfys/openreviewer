"""审查任务详情、事件日志和人工控制动作的 SQLAlchemy 适配器。"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ExecutionStatus
from domain.security import redact_sensitive
from persistence.models import (
    ModelCallRecord,
    OutboxEventRecord,
    PullRequestCiCheckRecord,
    PullRequestVersionRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from services.review_management import (
    FindingDecision,
    ReviewAction,
    ReviewActionConflictError,
    ReviewManagementRepository,
    ReviewManagementPersistenceError,
    ReviewNotFoundError,
    StoredCiCheck,
    StoredFinding,
    StoredReviewDetails,
    StoredReviewEvent,
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _safe_payload(value: object) -> object:
    """递归脱敏事件 JSON，保证日志面板不会回显凭据。"""

    redacted = redact_sensitive(value)
    if isinstance(redacted, dict):
        return {str(key): _safe_payload(item) for key, item in redacted.items()}
    if isinstance(redacted, list):
        return [_safe_payload(item) for item in redacted]
    if isinstance(redacted, tuple):
        return [_safe_payload(item) for item in redacted]
    return redacted


class SqlAlchemyReviewManagementRepository(ReviewManagementRepository):
    """以固定数量的有界查询读取详情，并在短事务内执行人工动作。"""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], object] | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory or uuid4

    def get(self, review_run_id: str) -> StoredReviewDetails:
        """读取一条运行、计划、模型调用及其有界子资源快照。

        主记录、Finding、CI 和事件分别使用最多四次查询；所有子查询都带
        ``LIMIT``，循环只负责把已取回的行转成不可变读模型。
        """

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
                        PullRequestVersionRecord.id.label("pr_version_id"),
                        PullRequestVersionRecord.title.label("pr_title"),
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
                        ModelCallRecord.estimated_cost_microusd.label(
                            "model_cost_microusd"
                        ),
                        ModelCallRecord.finding_count.label("model_finding_count"),
                        ModelCallRecord.created_at.label("model_created_at"),
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
                    .where(ReviewRunRecord.id == review_run_id)
                ).mappings().one_or_none()
                if row is None:
                    raise ReviewNotFoundError("审查任务不存在")

                findings = self._load_findings(session, review_run_id)
                version_id = row["pr_version_id"]
                ci_checks = self._load_ci_checks(session, version_id)
                events = self._load_events(session, review_run_id)
                safe_error = redact_sensitive(row["last_error"])
                safe_code = redact_sensitive(row["last_error_code"])
                safe_details = _safe_payload(row["last_error_details"])
                return StoredReviewDetails(
                    review_run_id=row["review_run_id"],
                    review_task_id=row["review_task_id"],
                    review_version_key=row["review_version_key"],
                    installation_id=row["installation_id"],
                    repository_id=row["repository_id"],
                    repository=row["repository"],
                    pull_request_number=row["pull_request_number"],
                    head_sha=row["head_sha"],
                    execution_status=ExecutionStatus(row["execution_status"]),
                    review_conclusion=row["review_conclusion"],
                    coverage_status=row["coverage_status"],
                    priority=row["priority"],
                    attempt_count=row["attempt_count"],
                    model_attempt_count=row["model_attempt_count"],
                    max_attempts=row["max_attempts"],
                    ci_poll_count=row["ci_poll_count"],
                    available_at=_as_utc(row["available_at"]),
                    claimed_from_status=row["claimed_from_status"],
                    lease_owner=row["lease_owner"],
                    lease_expires_at=_as_utc(row["lease_expires_at"]),
                    last_error=(safe_error if isinstance(safe_error, str) else None),
                    last_error_code=(safe_code if isinstance(safe_code, str) else None),
                    last_error_retryable=row["last_error_retryable"],
                    last_error_details=(
                        safe_details if isinstance(safe_details, dict) else None
                    ),
                    created_at=_as_utc(row["created_at"]),
                    updated_at=_as_utc(row["updated_at"]),
                    pr_title=row["pr_title"],
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
                    model_cost_microusd=row["model_cost_microusd"],
                    model_finding_count=row["model_finding_count"],
                    model_created_at=_as_utc(row["model_created_at"]),
                    findings=findings,
                    ci_checks=ci_checks,
                    events=events,
                )
            except ReviewNotFoundError:
                raise
            except (SQLAlchemyError, ValueError, TypeError) as exc:
                raise ReviewManagementPersistenceError(
                    "review details could not be loaded"
                ) from exc

    @staticmethod
    def _load_findings(
        session: Session,
        review_run_id: str,
    ) -> tuple[StoredFinding, ...]:
        rows = session.execute(
            select(
                ReviewFindingRecord.id,
                ReviewFindingRecord.severity,
                ReviewFindingRecord.category,
                ReviewFindingRecord.title,
                ReviewFindingRecord.evidence,
                ReviewFindingRecord.impact,
                ReviewFindingRecord.suggestion,
                ReviewFindingRecord.required_test,
                ReviewFindingRecord.confidence,
                ReviewFindingRecord.verification_status,
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
            .where(ReviewFindingRecord.review_run_id == review_run_id)
            .order_by(ReviewFindingRecord.created_at.asc(), ReviewFindingRecord.id.asc())
            .limit(200)
        ).mappings()
        return tuple(
            StoredFinding(
                id=row["id"],
                severity=row["severity"],
                category=row["category"],
                title=row["title"],
                evidence=row["evidence"],
                impact=row["impact"],
                suggestion=row["suggestion"],
                required_test=row["required_test"],
                confidence=float(row["confidence"]),
                verification_status=row["verification_status"],
                location_file=row["location_file"],
                location_start_line=row["location_start_line"],
                location_end_line=row["location_end_line"],
                location_side=row["location_side"],
                location_in_diff=bool(row["location_in_diff"]),
                location_symbol=row["location_symbol"],
                rule_reference=row["rule_reference"],
                reviewed_at=_as_utc(row["reviewed_at"]),
                reviewed_by=row["reviewed_by"],
                created_at=_as_utc(row["created_at"]),
            )
            for row in rows
        )

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
                observed_at=_as_utc(row["observed_at"]),
            )
            for row in rows
        )

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
        events = [
            StoredReviewEvent(
                id=row["id"],
                event_type=row["event_type"],
                payload=(
                    _safe_payload(row["payload"])
                    if isinstance(_safe_payload(row["payload"]), dict)
                    else {}
                ),
                occurred_at=_as_utc(row["occurred_at"]),
            )
            for row in rows
        ]
        events.reverse()
        return tuple(events)

    def apply_action(
        self,
        review_run_id: str,
        action: ReviewAction,
        *,
        actor: str,
        request_id: str,
    ) -> tuple[str, str, ExecutionStatus]:
        """在一个短事务内执行加速、重试、取消或重新审查。"""

        if not request_id.strip():
            raise ReviewActionConflictError("操作幂等键不能为空")
        action_key = sha256(
            f"{review_run_id}:{action.value}:{request_id}".encode("utf-8")
        ).hexdigest()
        event_key = f"review.action:{review_run_id}:{action.value}:{action_key}"
        with self._sessions() as session:
            try:
                existing_event = session.execute(
                    select(OutboxEventRecord.payload).where(
                        OutboxEventRecord.event_key == event_key
                    )
                ).scalar_one_or_none()
                if existing_event is not None:
                    if (
                        action is ReviewAction.RERUN
                        and isinstance(existing_event, dict)
                        and isinstance(existing_event.get("new_review_run_id"), str)
                        and isinstance(existing_event.get("new_review_task_id"), str)
                    ):
                        return (
                            existing_event["new_review_run_id"],
                            existing_event["new_review_task_id"],
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
                        .where(ReviewRunRecord.id == review_run_id)
                    ).one_or_none()
                    if existing is None:
                        raise ReviewNotFoundError("审查任务不存在")
                    return existing[1], existing[0], ExecutionStatus(existing[2])

                # PostgreSQL 不允许 FOR UPDATE 锁定外连接的可空一侧。
                # 运行记录和任务记录是必需的，先用内连接一起锁定；计划记录
                # 是可选的，单独查询并锁定，既保留并发保护也兼容没有计划的早期阶段。
                row = session.execute(
                    select(ReviewRunRecord, ReviewTaskRecord)
                    .join(
                        ReviewTaskRecord,
                        ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
                    )
                    .where(ReviewRunRecord.id == review_run_id)
                    .with_for_update()
                ).one_or_none()
                if row is None:
                    raise ReviewNotFoundError("审查任务不存在")
                run, task = row
                plan = session.scalar(
                    select(ReviewPlanRecord)
                    .where(ReviewPlanRecord.review_run_id == review_run_id)
                    .with_for_update()
                )
                now = self._clock()
                current = ExecutionStatus(run.execution_status)

                if action is ReviewAction.RERUN:
                    if current is ExecutionStatus.RUNNING:
                        raise ReviewActionConflictError("任务正在处理中，暂时不能重新审查")
                    new_run_id = str(self._uuid_factory())
                    new_task_id = str(self._uuid_factory())
                    rerun_key = f"manual-rerun:{review_run_id}:{request_id}"[:200]
                    existing_rerun = session.scalar(
                        select(ReviewRunRecord.id).where(
                            ReviewRunRecord.idempotency_key == rerun_key
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
                                review_version_key=run.review_version_key,
                                installation_id=run.installation_id,
                                repository_id=run.repository_id,
                                repository=run.repository,
                                pull_request_number=run.pull_request_number,
                                head_sha=run.head_sha,
                                execution_status=ExecutionStatus.QUEUED.value,
                                review_conclusion=None,
                                coverage_status="unknown",
                                idempotency_key=rerun_key,
                                request_fingerprint=run.request_fingerprint,
                                created_at=now,
                                updated_at=now,
                            ),
                            ReviewTaskRecord(
                                id=new_task_id,
                                review_run_id=new_run_id,
                                execution_status=ExecutionStatus.QUEUED.value,
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
                                    "review_version_key": run.review_version_key,
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
                            event_type="review.manual.rerun_requested",
                            payload={
                                "action": action.value,
                                "actor": actor,
                                "new_review_run_id": new_run_id,
                                "new_review_task_id": new_task_id,
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

    def review_finding(
        self,
        review_run_id: str,
        finding_id: str,
        decision: FindingDecision,
        *,
        actor: str,
        request_id: str,
    ) -> None:
        """保存一次人工裁决，并追加可追踪的事件。"""

        decision_key = sha256(
            f"{review_run_id}:{finding_id}:{decision.value}:{request_id}".encode(
                "utf-8"
            )
        ).hexdigest()
        event_key = f"review.finding.decision:{finding_id}:{decision_key}"
        with self._sessions() as session:
            try:
                if session.scalar(
                    select(OutboxEventRecord.id).where(
                        OutboxEventRecord.event_key == event_key
                    )
                ) is not None:
                    return
                finding = session.scalar(
                    select(ReviewFindingRecord)
                    .where(
                        ReviewFindingRecord.id == finding_id,
                        ReviewFindingRecord.review_run_id == review_run_id,
                    )
                    .with_for_update()
                )
                if finding is None:
                    raise FindingDecisionError("候选问题不存在")
                now = self._clock()
                finding.verification_status = decision.value
                finding.reviewed_at = now
                finding.reviewed_by = actor[:100]
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
            except FindingDecisionError:
                session.rollback()
                raise FindingNotFoundError("候选问题不存在")
            except SQLAlchemyError as exc:
                session.rollback()
                raise ReviewManagementPersistenceError(
                    "finding decision could not be persisted"
                ) from exc


class FindingDecisionError(LookupError):
    """内部转换用的 Finding 不存在异常。"""
