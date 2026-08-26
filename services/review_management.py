"""审查任务详情、阶段投影与人工控制用例。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from domain.enums import ExecutionStatus


class ReviewManagementPersistenceError(RuntimeError):
    """任务详情或人工操作无法可靠地从数据库完成。"""


class ReviewNotFoundError(LookupError):
    """指定审查运行不存在。"""


class ReviewActionConflictError(ValueError):
    """当前任务状态不允许请求的人工操作。"""


class FindingNotFoundError(LookupError):
    """指定候选问题不属于当前审查运行。"""


class ReviewAction(str, Enum):
    EXPEDITE = "expedite"
    RETRY = "retry"
    CANCEL = "cancel"
    RERUN = "rerun"


class FindingDecision(str, Enum):
    VERIFIED = "verified"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class StoredReviewEvent:
    id: str
    event_type: str
    payload: Mapping[str, object]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class StoredCiCheck:
    name: str
    kind: str
    status: str
    conclusion: str | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class StoredFinding:
    id: str
    severity: str
    category: str
    title: str
    evidence: str
    impact: str
    suggestion: str
    required_test: str | None
    confidence: float
    verification_status: str
    location_file: str | None
    location_start_line: int | None
    location_end_line: int | None
    location_side: str | None
    location_in_diff: bool
    location_symbol: str | None
    rule_reference: str | None
    reviewed_at: datetime | None
    reviewed_by: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredReviewDetails:
    review_run_id: str
    review_task_id: str
    review_version_key: str
    installation_id: int
    repository_id: int
    repository: str
    pull_request_number: int
    head_sha: str
    execution_status: ExecutionStatus
    review_conclusion: str | None
    coverage_status: str
    priority: int
    attempt_count: int
    model_attempt_count: int
    max_attempts: int
    ci_poll_count: int
    available_at: datetime
    claimed_from_status: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error: str | None
    last_error_code: str | None
    last_error_retryable: bool | None
    last_error_details: Mapping[str, object] | None
    created_at: datetime
    updated_at: datetime
    pr_title: str | None
    pr_state: str | None
    pr_is_draft: bool | None
    changed_files_count: int | None
    files_complete: bool | None
    diff_complete: bool | None
    context_fetched_at: datetime | None
    ci_state: str | None
    ci_checks_complete: bool | None
    ci_checked_at: datetime | None
    review_plan_id: str | None
    plan_created_at: datetime | None
    plan_file_count: int | None
    plan_unit_count: int | None
    plan_rule_count: int | None
    plan_input_bytes: int | None
    plan_rules_complete: bool | None
    model_review_completed_at: datetime | None
    model_call_id: str | None
    model_provider: str | None
    model_protocol: str | None
    model_name: str | None
    model_status: str | None
    model_response_status: int | None
    model_duration_ms: int | None
    model_input_tokens: int | None
    model_output_tokens: int | None
    model_cache_read_tokens: int | None
    model_cache_write_tokens: int | None
    model_cost_microusd: int | None
    model_finding_count: int | None
    model_created_at: datetime | None
    findings: tuple[StoredFinding, ...]
    ci_checks: tuple[StoredCiCheck, ...]
    events: tuple[StoredReviewEvent, ...]


@dataclass(frozen=True, slots=True)
class ReviewStage:
    key: str
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    detail_code: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewDetails:
    stored: StoredReviewDetails
    current_stage: str
    phase: str
    stages: tuple[ReviewStage, ...]
    available_actions: tuple[ReviewAction, ...]
    verified_finding_count: int
    rejected_finding_count: int
    unverified_finding_count: int


class ReviewManagementRepository(Protocol):
    def get(self, review_run_id: str) -> StoredReviewDetails:
        """返回单条任务及其有界事件、CI 和 Finding 快照。"""

    def apply_action(
        self,
        review_run_id: str,
        action: ReviewAction,
        *,
        actor: str,
        request_id: str,
    ) -> tuple[str, str, ExecutionStatus]:
        """幂等执行任务控制动作并返回运行、任务和新状态。"""

    def review_finding(
        self,
        review_run_id: str,
        finding_id: str,
        decision: FindingDecision,
        *,
        actor: str,
        request_id: str,
    ) -> None:
        """幂等保存人工 Finding 裁决和对应审计事件。"""


class ReviewManagementService:
    def __init__(self, repository: ReviewManagementRepository) -> None:
        self._repository = repository

    def details(self, review_run_id: str) -> ReviewDetails:
        stored = self._repository.get(review_run_id)
        verified = sum(
            item.verification_status == FindingDecision.VERIFIED.value
            for item in stored.findings
        )
        rejected = sum(
            item.verification_status == FindingDecision.REJECTED.value
            for item in stored.findings
        )
        unverified = len(stored.findings) - verified - rejected
        current_stage, phase = self._current_stage(stored, unverified)
        return ReviewDetails(
            stored=stored,
            current_stage=current_stage,
            phase=phase,
            stages=self._stages(stored, current_stage, phase, unverified),
            available_actions=self._available_actions(stored),
            verified_finding_count=verified,
            rejected_finding_count=rejected,
            unverified_finding_count=unverified,
        )

    def apply_action(
        self,
        review_run_id: str,
        action: ReviewAction,
        *,
        actor: str,
        request_id: str,
    ) -> tuple[str, str, ExecutionStatus]:
        return self._repository.apply_action(
            review_run_id,
            action,
            actor=actor,
            request_id=request_id,
        )

    def review_finding(
        self,
        review_run_id: str,
        finding_id: str,
        decision: FindingDecision,
        *,
        actor: str,
        request_id: str,
    ) -> ReviewDetails:
        self._repository.review_finding(
            review_run_id,
            finding_id,
            decision,
            actor=actor,
            request_id=request_id,
        )
        return self.details(review_run_id)

    @staticmethod
    def _current_stage(
        item: StoredReviewDetails,
        unverified_findings: int,
    ) -> tuple[str, str]:
        status = item.execution_status
        if status is ExecutionStatus.SUPERSEDED:
            return "intake", "superseded"
        if status is ExecutionStatus.CANCELLED:
            return "intake", "cancelled"
        if status is ExecutionStatus.TIMED_OUT:
            return "ci", "ci_timed_out"
        if status is ExecutionStatus.COMPLETED:
            return "publication", "completed"
        if status is ExecutionStatus.FAILED:
            if item.review_plan_id is not None:
                return "model", "model_failed"
            if item.context_fetched_at is not None:
                return "planning", "planning_failed"
            return "context", "context_failed"
        if item.model_review_completed_at is not None:
            if unverified_findings:
                return "verification", "awaiting_verification"
            return "publication", "awaiting_publication"
        if item.review_plan_id is not None:
            return (
                "model",
                "model_running"
                if status is ExecutionStatus.RUNNING
                else "model_queued",
            )
        if item.context_fetched_at is not None and item.ci_state in {
            "success",
            "failure",
        }:
            return (
                "planning",
                "planning_running"
                if status is ExecutionStatus.RUNNING
                else "planning_queued",
            )
        if item.context_fetched_at is not None:
            return (
                "ci",
                "ci_checking"
                if status is ExecutionStatus.RUNNING
                else "waiting_ci",
            )
        return (
            "context",
            "context_loading"
            if status is ExecutionStatus.RUNNING
            else "queued",
        )

    @staticmethod
    def _available_actions(
        item: StoredReviewDetails,
    ) -> tuple[ReviewAction, ...]:
        if item.execution_status in {
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMED_OUT,
        }:
            return (ReviewAction.RETRY, ReviewAction.RERUN)
        if item.model_review_completed_at is not None:
            return (ReviewAction.RERUN,)
        if item.execution_status in {
            ExecutionStatus.QUEUED,
            ExecutionStatus.WAITING_FOR_CI,
            ExecutionStatus.READY_FOR_REVIEW,
        }:
            return (ReviewAction.EXPEDITE, ReviewAction.CANCEL)
        return ()

    @staticmethod
    def _stages(
        item: StoredReviewDetails,
        current_stage: str,
        phase: str,
        unverified_findings: int,
    ) -> tuple[ReviewStage, ...]:
        order = ("intake", "context", "ci", "planning", "model", "verification", "publication")
        current_index = order.index(current_stage)
        failed_phase = phase.endswith("failed") or phase == "ci_timed_out"
        event_times = {event.event_type: event.occurred_at for event in item.events}
        completed_times = {
            "intake": event_times.get("review.requested", item.created_at),
            "context": item.context_fetched_at,
            "ci": (
                item.ci_checked_at
                if item.ci_state in {"success", "failure"}
                else None
            ),
            "planning": item.plan_created_at,
            "model": item.model_review_completed_at,
            "verification": (
                max(
                    (
                        finding.reviewed_at
                        for finding in item.findings
                        if finding.reviewed_at is not None
                    ),
                    default=None,
                )
                if item.findings and unverified_findings == 0
                else (item.model_review_completed_at if not item.findings and item.model_review_completed_at else None)
            ),
            "publication": (
                item.updated_at
                if item.execution_status is ExecutionStatus.COMPLETED
                else None
            ),
        }
        result: list[ReviewStage] = []
        for index, key in enumerate(order):
            if completed_times[key] is not None and (
                index < current_index or key == "intake" or key == "verification"
            ):
                stage_status = "completed"
            elif index == current_index:
                stage_status = "failed" if failed_phase else "current"
            elif index < current_index:
                stage_status = "completed"
            else:
                stage_status = "pending"
            if key == "publication" and phase == "awaiting_publication":
                stage_status = "blocked"
            result.append(
                ReviewStage(
                    key=key,
                    status=stage_status,
                    started_at=(item.created_at if key == "intake" else None),
                    completed_at=completed_times[key],
                    detail_code=(phase if index == current_index else None),
                )
            )
        return tuple(result)
