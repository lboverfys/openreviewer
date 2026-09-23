"""审查管理门面：保持 API 与权限边界，分离各项存储职责。"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ExecutionStatus
from domain.github import PullRequestSnapshot
from domain.pagination import CursorPage
from domain.review_planning import ReviewFilePlan
from domain.review_progress import BatchSnapshot
from persistence.management import actions as _store_actions
from persistence.management import findings as _store_findings
from persistence.management import identity as _store_identity
from persistence.management import publishing as _store_publishing
from persistence.management import queries as _store_queries
from persistence.management import retry as _store_retry
from persistence.management.common import (
    _PUBLISH_RECOVERY_AFTER as _PUBLISH_RECOVERY_AFTER,
)
from persistence.management.common import FindingDecisionError as FindingDecisionError
from persistence.management.common import _as_utc as _as_utc
from persistence.management.common import (
    _latest_summary_failed as _latest_summary_failed,
)
from persistence.management.common import (
    _project_agent_progress as _project_agent_progress,
)
from persistence.management.common import _required_utc as _required_utc
from persistence.management.common import _review_change_token as _review_change_token
from persistence.management.common import _safe_payload as _safe_payload
from persistence.models import ReviewPlanRecord, ReviewRunRecord, ReviewTaskRecord
from services.rbac import ResourceScope
from services.review_management import (
    FindingCursor,
    FindingDecision,
    ReviewAction,
    ReviewIdentityTarget,
    ReviewManagementRepository,
    StoredCiCheck,
    StoredEvaluationGate,
    StoredFinding,
    StoredFindingCounts,
    StoredReviewDetails,
    StoredReviewEvent,
)


class SqlAlchemyReviewManagementRepository(ReviewManagementRepository):
    """稳定管理门面，分别组合读取、裁决、重试和外部发布。"""

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

    def batch_page(self, review_run_id: str, agent: str, *, after: int=0, limit: int=10, scope: ResourceScope | None=None) -> CursorPage[BatchSnapshot]:
        return _store_queries.batch_page(self, review_run_id, agent, after=after, limit=limit, scope=scope)

    def excluded_file_page(self, review_run_id: str, *, plan_id: str, limit: int=10, cursor: str | None=None, scope: ResourceScope | None=None) -> CursorPage[ReviewFilePlan]:
        return _store_queries.excluded_file_page(self, review_run_id, plan_id=plan_id, limit=limit, cursor=cursor, scope=scope)

    def finding_page(self, review_run_id: str, *, limit: int=10, cursor: str | None=None, severity: str | None=None, adjudication_status: str | None=None, query: str='', scope: ResourceScope | None=None) -> CursorPage[StoredFinding]:
        return _store_queries.finding_page(self, review_run_id, limit=limit, cursor=cursor, severity=severity, adjudication_status=adjudication_status, query=query, scope=scope)

    def event_page(self, review_run_id: str, *, limit: int=10, cursor: str | None=None, event_filter: str='all', scope: ResourceScope | None=None) -> CursorPage[StoredReviewEvent]:
        return _store_queries.event_page(self, review_run_id, limit=limit, cursor=cursor, event_filter=event_filter, scope=scope)

    def change_token(self, review_run_id: str, *, scope: ResourceScope | None=None) -> str:
        return _store_queries.change_token(self, review_run_id, scope=scope)

    def get(self, review_run_id: str, *, finding_limit: int=50, finding_cursor: FindingCursor | None=None, finding_adjudication_status: str | None=None, scope: ResourceScope | None=None, view: str='full') -> StoredReviewDetails:
        return _store_queries.get(self, review_run_id, finding_limit=finding_limit, finding_cursor=finding_cursor, finding_adjudication_status=finding_adjudication_status, scope=scope, view=view)

    def get_identity_target(self, review_run_id: str, *, scope: ResourceScope | None=None) -> ReviewIdentityTarget:
        return _store_queries.get_identity_target(self, review_run_id, scope=scope)

    def save_pull_request_identity(self, review_run_id: str, snapshot: PullRequestSnapshot, *, actor: str, request_id: str, scope: ResourceScope | None=None) -> None:
        return _store_identity.save_pull_request_identity(self, review_run_id, snapshot, actor=actor, request_id=request_id, scope=scope)

    @staticmethod
    def _load_findings(session: Session, review_run_id: str, *, limit: int, cursor: FindingCursor | None, adjudication_status: str | None, severity: str | None=None, search: str='') -> tuple[tuple[StoredFinding, ...], bool]:
        return _store_findings._load_findings(session, review_run_id, limit=limit, cursor=cursor, adjudication_status=adjudication_status, severity=severity, search=search)

    @staticmethod
    def _load_finding_counts(session: Session, review_run_id: str) -> StoredFindingCounts:
        return _store_findings._load_finding_counts(session, review_run_id)

    @staticmethod
    def _load_evaluation_gates(session: Session, repository_id: int) -> tuple[StoredEvaluationGate, ...]:
        return _store_findings._load_evaluation_gates(session, repository_id)

    @staticmethod
    def _load_ci_checks(session: Session, version_id: str | None) -> tuple[StoredCiCheck, ...]:
        return _store_queries._load_ci_checks(session, version_id)

    @staticmethod
    def _load_plan_file_decisions(session: Session, review_plan_id: str | None) -> dict[str, int]:
        return _store_queries._load_plan_file_decisions(session, review_plan_id)

    @staticmethod
    def _load_events(session: Session, review_run_id: str) -> tuple[StoredReviewEvent, ...]:
        return _store_queries._load_events(session, review_run_id)

    def apply_action(self, review_run_id: str, action: ReviewAction, *, actor: str, request_id: str, target_stage: str | None=None, retry_scope: str | None=None, agent: str | None=None, batch_number: int | None=None, state_version: str | None=None, head_sha: str | None=None, capture_model_outputs: bool=False, acknowledge_exclusions: bool=False, review_profile_id: str | None=None, scope: ResourceScope | None=None) -> tuple[str, str, ExecutionStatus]:
        return _store_actions.apply_action(self, review_run_id, action, actor=actor, request_id=request_id, target_stage=target_stage, retry_scope=retry_scope, agent=agent, batch_number=batch_number, state_version=state_version, head_sha=head_sha, capture_model_outputs=capture_model_outputs, acknowledge_exclusions=acknowledge_exclusions, review_profile_id=review_profile_id, scope=scope)

    @staticmethod
    def _existing_action_result(session: Session, review_run_id: str, action: ReviewAction, *, event_key: str, target_stage: str | None, retry_scope: str | None=None, agent: str | None=None, batch_number: int | None=None, scope: ResourceScope | None=None) -> tuple[str, str, ExecutionStatus] | None:
        return _store_actions._existing_action_result(session, review_run_id, action, event_key=event_key, target_stage=target_stage, retry_scope=retry_scope, agent=agent, batch_number=batch_number, scope=scope)

    @staticmethod
    def _prepare_stage_retry(session: Session, run: ReviewRunRecord, task: ReviewTaskRecord, plan: ReviewPlanRecord | None, target_stage: ExecutionStatus) -> None:
        return _store_retry._prepare_stage_retry(session, run, task, plan, target_stage)

    @staticmethod
    def _prepare_failed_node_retry(session: Session, run: ReviewRunRecord, task: ReviewTaskRecord, plan: ReviewPlanRecord | None, *, agent: str | None, batch_number: int | None, now: datetime) -> None:
        return _store_retry._prepare_failed_node_retry(session, run, task, plan, agent=agent, batch_number=batch_number, now=now)

    @staticmethod
    def _paused_execution_status(workflow_status: ExecutionStatus, current_execution: str) -> ExecutionStatus:
        return _store_retry._paused_execution_status(workflow_status, current_execution)

    @staticmethod
    def _resumed_execution_status(workflow_status: ExecutionStatus, current_execution: str) -> ExecutionStatus:
        return _store_retry._resumed_execution_status(workflow_status, current_execution)

    def publish(self, review_run_id: str, *, actor: str, request_id: str, state_version: str | None=None, head_sha: str | None=None, scope: ResourceScope | None=None) -> tuple[str, str, ExecutionStatus]:
        return _store_publishing.publish(self, review_run_id, actor=actor, request_id=request_id, state_version=state_version, head_sha=head_sha, scope=scope)

    def _mark_publish_failed(self, review_run_id: str, event_key: str, attempt_token: str, actor: str, safe_reason: str, *, scope: ResourceScope | None=None) -> None:
        return _store_publishing._mark_publish_failed(self, review_run_id, event_key, attempt_token, actor, safe_reason, scope=scope)

    def review_finding(self, review_run_id: str, finding_id: str, decision: FindingDecision, *, actor: str, request_id: str, scope: ResourceScope | None=None) -> None:
        return _store_findings.review_finding(self, review_run_id, finding_id, decision, actor=actor, request_id=request_id, scope=scope)
