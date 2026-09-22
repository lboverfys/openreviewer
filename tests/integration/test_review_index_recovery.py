"""审查准备失败与向量续补的离线回归，不调用线上模型。"""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from sqlalchemy import select

from apps.worker.runtime import WorkerRuntime
from apps.worker.settings import WorkerSettings
from domain.enums import ModelProvider, ReviewAgent
from persistence.models import ModelReviewBatchRecord, ReviewRunRecord, ReviewTaskRecord
from persistence.review_management import SqlAlchemyReviewManagementRepository
from services.agent_workflow import FixedAgentWorkflow, scope_model_review_input
from services.model_review import ModelServiceSettings, plan_model_review_batches
from services.review_management import ReviewAction, ReviewManagementService
from services.review_planning import DeterministicReviewPlanner
from services.task_queue import ModelReviewInputError
from tests.integration.test_hybrid_retrieval import TARGET, FakeModels, sources
from tests.integration.test_hybrid_retrieval import retrieval as retrieval
from tests.integration.test_review_plan_persistence import (
    MutableClock,
    StaticModelReviewer,
    _prepare_planning_lease,
    _rules,
)
from tests.integration.test_review_plan_persistence import database as database
from tests.unit.test_model_review import make_model_input
from tests.unit.test_workflow import model_result


def test_filtered_related_groups_can_be_batched_without_changing_scope():
    base = make_model_input()
    units = tuple(base.units[0].model_copy(update={
        "file": name, "unit_key": key * 64, "group_key": group * 64,
        "review_domains": domains, "planner_version": "review-planner-v3",
    }) for name, key, group, domains in (
        ("a.py", "a", "1", (ReviewAgent.LOGIC,)),
        ("z.py", "b", "1", (ReviewAgent.SECURITY,)),
        ("b.py", "c", "2", (ReviewAgent.SECURITY,)),
    ))
    original = base.model_copy(update={"units": units, "planner_version": "review-planner-v3"})
    scoped = scope_model_review_input(original, ReviewAgent.SECURITY)
    assert [unit.file for unit in scoped.units] == ["b.py", "z.py"]
    settings = ModelServiceSettings(provider=ModelProvider.OPENAI, model="offline", api_key="unused")
    batches = plan_model_review_batches(scoped, settings)
    assert {unit.file for batch in batches for unit in batch.review_input.units} == {"b.py", "z.py"}
    assert original.units == units


def test_agent_failure_is_reported_before_the_next_agent_runs():
    events = []
    failure = Mock()
    failure.review.side_effect = ModelReviewInputError("输入结构失败")
    other = Mock()

    def review(_source):
        assert events[0] == (ReviewAgent.SECURITY, "failed")
        return model_result("1")

    other.review.side_effect = review
    execution = FixedAgentWorkflow({ReviewAgent.SECURITY: failure,
        ReviewAgent.CONVENTION: other, ReviewAgent.LOGIC: other}, max_concurrency=1).run(
        make_model_input(), on_agent_completed=lambda item: events.append((item.agent, item.status)))
    assert execution.agents[0].status == "failed"
    assert other.review.call_count == 2


def test_zero_batch_retry_keeps_other_agents_successful_batches(database):
    clock = MutableClock(datetime.now(UTC))
    queue, lease, task_id, run_id = _prepare_planning_lease(database, clock, complete_context=True)
    planning = queue.load_planning_input(lease)
    rules = _rules(planning.target)
    queue.store_review_plan(lease, rules, DeterministicReviewPlanner().plan(planning.target, planning.files, rules))
    lease = queue.claim_next("worker-1", timedelta(seconds=30))
    model_input = queue.load_model_review_input(lease)
    batches = plan_model_review_batches(model_input,
        ModelServiceSettings(provider=ModelProvider.OPENAI, model="offline", api_key="unused"))
    result = StaticModelReviewer().review(model_input)
    for agent in ("convention", "logic"):
        queue.ensure_model_batches(lease, batches, agent=agent)
        queue.claim_model_batch(lease, 1, agent=agent, lease_duration=timedelta(seconds=30))
        queue.complete_model_batch(lease, 1, result, agent=agent)
    queue.record_model_progress(lease, "agent_failed", {"agent": "security", "status": "failed"}, agent="security")
    with database.sessions() as session, session.begin():
        task = session.get(ReviewTaskRecord, task_id)
        task.execution_status = task.workflow_status = "failed"
        run = session.get(ReviewRunRecord, run_id)
        run.execution_status = run.workflow_status = "failed"
        run.coverage_status = "partial"
    manager = ReviewManagementService(SqlAlchemyReviewManagementRepository(database.sessions, clock=clock))
    manager.apply_action(run_id, ReviewAction.RETRY_FAILED_NODE, actor="tester", request_id="retry-unplanned-security", agent="security")
    with database.sessions() as session:
        rows = session.scalars(select(ModelReviewBatchRecord).where(ModelReviewBatchRecord.review_plan_id == model_input.review_plan_id)).all()
        assert len(rows) == 2 and all(row.status == "succeeded" for row in rows)
        assert session.get(ReviewTaskRecord, task_id).execution_status == "ready_for_review"


def test_index_rounds_keep_total_request_budget_and_update_live_progress(retrieval):
    service, _, _ = retrieval
    service.source_loader = Mock(side_effect=AssertionError("续补不得重复读取源码"))
    view = service.settings.get()
    service.settings.update(view.settings.model_copy(update={"max_new_vectors_per_index": 2, "max_requests_per_operation": 2}), view.revision, "tester")
    FakeModels.embeddings = 0
    index = service.index_sources(TARGET, sources())
    assert index.status == "queued" and index.vector_count == 2
    assert service.process_next()
    index = service.repository.get(index.id)
    assert index.vector_status == "limited" and index.vector_count == 4
    assert FakeModels.embeddings == 4
    assert not service.process_next()
    service.retry_index(index.id, None, include_vectors=True)
    assert service.process_next()
    assert service.repository.get(index.id).vector_status == "ready"
    assert FakeModels.embeddings == 5


def test_worker_roles_do_not_claim_each_others_queue(monkeypatch):
    for role in ("review", "index"):
        queue = Mock()
        queue.recover_expired_leases.return_value = 0
        queue.claim_next.return_value = None
        index = Mock()
        index.process_next.return_value = True
        runtime = WorkerRuntime(queue, WorkerSettings(worker_id=role, role=role,
            poll_interval=timedelta(seconds=1), lease_duration=timedelta(seconds=10)), retrieval_service=index)
        monkeypatch.setattr(runtime, "_ensure_heartbeat_started", lambda: None)
        monkeypatch.setattr(runtime, "_record_owned_heartbeat", lambda *_args: None)
        runtime.run_once()
        assert queue.claim_next.call_count == (1 if role == "review" else 0)
        assert index.process_next.call_count == (1 if role == "index" else 0)
        queue.reset_mock()
        index.reset_mock()
        runtime.stop_event.set()
        assert runtime.run_once() is False
        queue.claim_next.assert_not_called()
        index.process_next.assert_not_called()
