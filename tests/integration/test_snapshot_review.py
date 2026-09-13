from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from domain.enums import ExecutionStatus
from domain.model_review import materialize_findings
from persistence.models import (
    FindingEvaluationRecord,
    FindingLifecycleRecord,
    PullRequestVersionRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.review_management import SqlAlchemyReviewManagementRepository
from services.review_management import (
    FindingDecision,
    ReviewAction,
    ReviewActionConflictError,
    ReviewManagementService,
)
from services.review_planning import DeterministicReviewPlanner
from services.task_queue import TaskLeaseLostError
from tests.integration.test_review_plan_persistence import (
    MutableClock,
    StaticModelReviewer,
    _prepare_planning_lease,
    _rules,
    _submit,
)
from tests.integration.test_review_plan_persistence import (
    database as database,
)


def test_cancel_running_task_stops_lease_and_all_timeline_nodes(database):
    clock = MutableClock(datetime(2026, 9, 14, tzinfo=UTC))
    queue, lease, _, run_id = _prepare_planning_lease(database, clock, complete_context=True)
    service = ReviewManagementService(SqlAlchemyReviewManagementRepository(database.sessions, clock=clock))
    service.apply_action(run_id, ReviewAction.CANCEL, actor="tester", request_id="stop-active")
    with pytest.raises(TaskLeaseLostError):
        queue.load_planning_input(lease)
    detail = service.details(run_id)
    assert detail.phase == "cancelled" and detail.current_stage == "result"
    assert all(stage.status not in {"current", "pending"} for stage in detail.stages)
    assert ReviewAction.REVIEW_SNAPSHOT in detail.available_actions


def test_closed_pr_snapshot_finishes_without_approval_publish_or_lifecycle_changes(database):
    clock = MutableClock(datetime(2026, 9, 14, tzinfo=UTC))
    queue, _, _, source_id = _prepare_planning_lease(database, clock, complete_context=True)
    service = ReviewManagementService(SqlAlchemyReviewManagementRepository(database.sessions, clock=clock))
    service.apply_action(source_id, ReviewAction.CANCEL, actor="tester", request_id="cancel-source")
    with database.sessions() as session, session.begin():
        version = session.scalar(select(PullRequestVersionRecord).limit(1))
        version.pr_state = "closed"
    created = service.apply_action(source_id, ReviewAction.REVIEW_SNAPSHOT, actor="tester", request_id="snapshot-one")
    assert service.apply_action(source_id, ReviewAction.REVIEW_SNAPSHOT, actor="tester", request_id="snapshot-one") == created
    run_id, _, status = created
    assert run_id != source_id and status is ExecutionStatus.READY_FOR_REVIEW
    lease = queue.claim_next("snapshot-worker", timedelta(seconds=30))
    assert lease.review_run_id == run_id
    planning = queue.load_planning_input(lease)
    rules = _rules(planning.target)
    plan = DeterministicReviewPlanner().plan(planning.target, planning.files, rules)
    queue.store_review_plan(lease, rules, plan)
    model_lease = queue.claim_next("snapshot-worker", timedelta(seconds=30))
    model_input = queue.load_model_review_input(model_lease)
    result = StaticModelReviewer().review(model_input)
    queue.store_model_review(model_lease, model_input, result, materialize_findings(model_input, result.output))
    detail = service.details(run_id)
    assert detail.stored.snapshot_review and detail.phase == "completed"
    assert {stage.key for stage in detail.stages if stage.status == "skipped"} == {"ci", "approval", "publish"}
    assert ReviewAction.PUBLISH not in detail.available_actions
    reviewed = service.review_finding(run_id, detail.stored.findings[0].id, FindingDecision.VALID,
        actor="tester", request_id="history-only-decision")
    assert reviewed.stored.findings[0].adjudication_status == "valid"
    with pytest.raises(ReviewActionConflictError, match="历史版本复查"):
        service.apply_action(run_id, ReviewAction.PUBLISH, actor="tester", request_id="cannot-publish-history")
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(FindingLifecycleRecord)) == 0
        assert session.scalar(select(func.count()).select_from(FindingEvaluationRecord)) == 0
        run = session.get(ReviewRunRecord, run_id)
        assert run.approval_requested_at is None and run.approval_due_at is None
        assert session.get(ReviewRunRecord, source_id).execution_status == "cancelled"


def test_snapshot_requires_saved_code_and_does_not_create_an_empty_run(database):
    clock = MutableClock(datetime(2026, 9, 14, tzinfo=UTC))
    _, run_id = _submit(database, clock, "no-snapshot", "a" * 40)
    service = ReviewManagementService(SqlAlchemyReviewManagementRepository(database.sessions, clock=clock))
    with pytest.raises(ReviewActionConflictError, match="完整代码"):
        service.apply_action(run_id, ReviewAction.REVIEW_SNAPSHOT, actor="tester", request_id="reject-empty")
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ReviewRunRecord)) == 1


def test_failed_snapshot_retry_does_not_return_to_closed_pr_collection(database):
    clock = MutableClock(datetime(2026, 9, 14, tzinfo=UTC))
    _, _, _, source_id = _prepare_planning_lease(database, clock, complete_context=True)
    service = ReviewManagementService(SqlAlchemyReviewManagementRepository(database.sessions, clock=clock))
    service.apply_action(source_id, ReviewAction.CANCEL, actor="tester", request_id="cancel-first")
    run_id, task_id, _ = service.apply_action(source_id, ReviewAction.REVIEW_SNAPSHOT, actor="tester", request_id="create-history")
    with database.sessions() as session, session.begin():
        run, task = session.get(ReviewRunRecord, run_id), session.get(ReviewTaskRecord, task_id)
        run.execution_status = task.execution_status = "failed"
        run.workflow_status = task.workflow_status = "failed"
    with pytest.raises(ReviewActionConflictError, match="不包含 CI"):
        service.apply_action(run_id, ReviewAction.RETRY_STAGE, target_stage="ci", actor="tester", request_id="cannot-restart-ci")
    result = service.apply_action(run_id, ReviewAction.RETRY, actor="tester", request_id="retry-history")
    assert result[2] is ExecutionStatus.READY_FOR_REVIEW
    assert service.details(run_id).stored.workflow_status is ExecutionStatus.PLANNING
