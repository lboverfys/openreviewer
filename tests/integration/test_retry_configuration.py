"""复现 PR109 的混合模型恢复、成功节点保留和阶段重试状态。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from apps.worker.runtime import WorkerRuntime
from apps.worker.settings import WorkerSettings
from domain.enums import ModelApiProtocol, ModelProvider, ReviewAgent
from domain.model_review import ModelReviewOutput
from domain.repository_policy import RepositoryPolicySnapshot
from persistence.models import (
    AiAgentConfigRecord,
    ModelReviewBatchRecord,
    OutboxEventRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.review_management import SqlAlchemyReviewManagementRepository
from services.agent_workflow import FixedAgentWorkflow
from services.ai_settings import ActiveAiRuntime
from services.model_review import (
    PROMPT_VERSION,
    ModelServiceSettings,
    model_configuration,
)
from services.review_management import ReviewAction, ReviewManagementService
from services.review_planning import DeterministicReviewPlanner
from tests.integration.test_review_management_api import application_for, login
from tests.integration.test_review_plan_persistence import (
    MutableClock,
    StaticAiRuntimeProvider,
    StaticModelReviewer,
    _prepare_planning_lease,
    _rules,
)
from tests.integration.test_review_plan_persistence import database as database


def seed_retry(database, *, mixed=False, changed=True):
    clock = MutableClock(datetime.now(UTC))
    queue, lease, task_id, run_id = _prepare_planning_lease(
        database, clock, complete_context=True
    )
    planning = queue.load_planning_input(lease)
    rules = _rules(planning.target)
    queue.store_review_plan(
        lease,
        rules,
        DeterministicReviewPlanner().plan(planning.target, planning.files, rules),
    )
    lease = queue.claim_next("worker-1", timedelta(seconds=30))
    source = queue.load_model_review_input(lease)
    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="new-model" if changed else "old-model",
        api_key="offline-only",
    )
    original = (
        StaticModelReviewer()
        .review(source)
        .model_copy(
            update={
                "model": "old-model",
                "prompt_version": PROMPT_VERSION,
                "output": ModelReviewOutput(findings=()),
                "configuration_fingerprint": model_configuration(
                    replace(settings, model="old-model")
                )["configuration_fingerprint"],
            }
        )
    )
    with database.sessions() as session, session.begin():
        for agent in ("security", "convention", "logic"):
            session.add(
                AiAgentConfigRecord(
                    agent=agent,
                    provider="openai",
                    model=settings.model,
                    api_protocol="responses",
                    enabled=True,
                    test_status="succeeded",
                    updated_by="test",
                    max_batch_input_tokens=settings.max_batch_input_tokens,
                )
            )
            for number in range(1, 8):
                succeeded = agent != "logic" or mixed or number <= 4
                result = (
                    original.model_copy(
                        update={"model": "new-model", "configuration_fingerprint": None}
                    )
                    if agent == "logic" and mixed and number > 4
                    else original
                )
                session.add(
                    ModelReviewBatchRecord(
                        id=f"{agent}-{number}",
                        review_plan_id=source.review_plan_id,
                        agent=agent,
                        batch_number=number,
                        batch_count=7,
                        unit_keys=[unit.unit_key for unit in source.units],
                        estimated_input_tokens=100,
                        status="succeeded" if succeeded else "failed",
                        attempt_count=1,
                        available_at=clock.value,
                        created_at=clock.value,
                        updated_at=clock.value,
                        result=result.model_dump(mode="json") if succeeded else None,
                    )
                )
            session.add(
                OutboxEventRecord(
                    id=f"terminal-{agent}",
                    event_key=f"terminal-{agent}",
                    aggregate_type="review_run",
                    aggregate_id=run_id,
                    event_type="review.model.agent_failed"
                    if agent == "logic"
                    else "review.model.agent_completed",
                    payload={
                        "agent": agent,
                        "status": "failed" if agent == "logic" else "completed",
                        "model_attempt_count": 1,
                    },
                    occurred_at=clock.value,
                    publish_attempts=0,
                )
            )
        task = session.get(ReviewTaskRecord, task_id)
        run = session.get(ReviewRunRecord, run_id)
        task.execution_status = run.execution_status = "failed"
        task.workflow_status = run.workflow_status = "agent_batches"
        task.lease_owner = task.lease_expires_at = None
        run.coverage_status = "partial"
    return queue, clock, task_id, run_id, source, settings, original


@pytest.mark.parametrize("agent", [None, "logic"])
@pytest.mark.parametrize("mixed,changed", [(True, True), (False, True), (False, False)])
def test_manual_retry_only_rebuilds_incompatible_failed_agent(
    database, agent, mixed, changed
):
    _, clock, task_id, run_id, source, settings, _ = seed_retry(
        database, mixed=mixed, changed=changed
    )
    manager = ReviewManagementService(
        SqlAlchemyReviewManagementRepository(database.sessions, clock=clock)
    )
    details = manager.details(run_id)
    impact = details.stored.retry_impacts["logic"]
    assert impact.model == settings.model and impact.batch_count == 7
    assert impact.restart is changed
    assert ReviewAction.RETRY_STAGE in details.available_actions
    with database.sessions() as session:
        preserved = {
            row.id: row.result
            for row in session.scalars(
                select(ModelReviewBatchRecord).where(
                    ModelReviewBatchRecord.agent != "logic"
                )
            )
        }
    for _ in range(2):
        manager.apply_action(
            run_id,
            ReviewAction.RETRY_FAILED_NODE,
            actor="test",
            request_id="retry-current",
            agent=agent,
        )
    queued = manager.details(run_id).stored
    assert queued.agent_statuses["logic"] == "planned"
    assert queued.agent_statuses["security"] == "completed"
    assert queued.agent_statuses["convention"] == "completed"
    assert queued.failed_agents == ()
    with database.sessions() as session:
        rows = session.scalars(
            select(ModelReviewBatchRecord).where(
                ModelReviewBatchRecord.review_plan_id == source.review_plan_id
            )
        ).all()
        assert {row.id: row.result for row in rows if row.agent != "logic"} == preserved
        logic = [row for row in rows if row.agent == "logic"]
        if changed:
            assert logic == []
        else:
            assert len(logic) == 7
            assert sum(row.status == "succeeded" for row in logic) == 4
            assert all(
                row.attempt_count == 0 for row in logic if row.status == "pending"
            )
        assert (
            session.get(ReviewTaskRecord, task_id).execution_status
            == "ready_for_review"
        )
        events = session.scalars(
            select(OutboxEventRecord).where(
                OutboxEventRecord.event_type == "review.model.retry_requested"
            )
        ).all()
        assert len(events) == 1
        assert events[0].payload["target_agents"] == ["logic"]
        assert events[0].payload["restarted_agents"] == (["logic"] if changed else [])


def test_recovered_worker_uses_new_model_without_calling_successful_agents(database):
    queue, clock, task_id, run_id, _, settings, original = seed_retry(
        database, mixed=True
    )
    manager = ReviewManagementService(
        SqlAlchemyReviewManagementRepository(database.sessions, clock=clock)
    )
    manager.apply_action(
        run_id,
        ReviewAction.RETRY_FAILED_NODE,
        actor="test",
        request_id="recover-mixed",
        agent="logic",
    )
    calls = []

    class Reviewer:
        def review(self, source):
            calls.append(source.review_agent)
            assert source.review_agent is ReviewAgent.LOGIC
            return original.model_copy(
                update={
                    "model": settings.model,
                    "configuration_fingerprint": model_configuration(settings)[
                        "configuration_fingerprint"
                    ],
                }
            )

        def close(self):
            pass

    agents = (ReviewAgent.SECURITY, ReviewAgent.CONVENTION, ReviewAgent.LOGIC)
    workflow = FixedAgentWorkflow(
        {agent: Reviewer() for agent in agents},
        agent_settings={agent: settings for agent in agents},
        max_concurrency=1,
    )
    runtime = WorkerRuntime(
        queue,
        WorkerSettings(
            worker_id="worker-1",
            poll_interval=timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        ),
        ai_runtime_provider=StaticAiRuntimeProvider(
            ActiveAiRuntime(
                revision=2,
                reviewer=None,
                planner=DeterministicReviewPlanner(),
                agent_workflow=workflow,
            )
        ),
    )
    assert runtime.run_once()
    assert calls == [ReviewAgent.LOGIC]
    with database.sessions() as session:
        assert session.get(ReviewTaskRecord, task_id).execution_status == "completed"
        assert all(
            row.result["model"] == "new-model"
            for row in session.scalars(
                select(ModelReviewBatchRecord).where(
                    ModelReviewBatchRecord.agent == "logic"
                )
            )
        )
        assert (
            len(
                session.scalars(
                    select(ModelReviewBatchRecord).where(
                        ModelReviewBatchRecord.agent != "logic"
                    )
                ).all()
            )
            == 14
        )


def test_stage_retry_api_accepts_failed_execution_at_agent_stage(database):
    _, _, _, run_id, _, _, _ = seed_retry(database, mixed=True)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application_for(database)),
            base_url="http://testserver",
        ) as client:
            await login(client)
            detail = (await client.get(f"/api/v1/reviews/{run_id}")).json()
            assert "retry_stage" in detail["available_actions"]
            response = await client.post(
                f"/api/v1/reviews/{run_id}/actions",
                headers={"Idempotency-Key": "advanced-current"},
                json={
                    "action": "retry_stage",
                    "target_stage": "agent_batches",
                    "state_version": detail["change_token"],
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["workflow_status"] == "agent_batches"

    asyncio.run(exercise())
    with database.sessions() as session:
        assert session.scalars(select(ModelReviewBatchRecord)).all() == []


def test_connection_only_changes_keep_configuration_fingerprint():
    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI, model="new-model", api_key="old-key"
    )
    assert model_configuration(settings) == model_configuration(
        replace(settings, api_key="new-key", read_timeout_seconds=60)
    )
    assert model_configuration(settings) != model_configuration(
        replace(settings, api_protocol=ModelApiProtocol.CHAT_COMPLETIONS)
    )


def test_summary_failure_event_can_be_retried_and_forces_summary(database):
    queue, clock, _, run_id, _, _, _ = seed_retry(database, mixed=True)
    with database.sessions() as session, session.begin():
        session.add(
            OutboxEventRecord(
                id="summary-failed",
                event_key="summary-failed",
                aggregate_type="review_run",
                aggregate_id=run_id,
                event_type="review.model.summary_failed",
                payload={
                    "agent": "summary",
                    "agent_status": "failed",
                    "model_attempt_count": 1,
                },
                occurred_at=clock.value + timedelta(seconds=1),
                publish_attempts=0,
            )
        )
    manager = ReviewManagementService(
        SqlAlchemyReviewManagementRepository(database.sessions, clock=clock)
    )
    manager.apply_action(
        run_id,
        ReviewAction.RETRY_FAILED_NODE,
        actor="test",
        request_id="summary-current",
        agent="summary",
    )
    lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert lease is not None and lease.force_summary


@pytest.mark.parametrize(
    "action",
    [ReviewAction.RETRY, ReviewAction.RETRY_FAILED_NODE, ReviewAction.RETRY_STAGE],
)
def test_manual_retry_uses_current_configuration_and_audits_previous_profile(
    database, action
):
    _, clock, _, run_id, _, _, _ = seed_retry(database, mixed=True)
    with database.sessions() as session, session.begin():
        run = session.get(ReviewRunRecord, run_id)
        run.repository_policy = RepositoryPolicySnapshot(
            repository="lboverfys/niuma", revision=1, review_profile_id="old-profile"
        ).model_dump(mode="json")
    manager = ReviewManagementService(
        SqlAlchemyReviewManagementRepository(database.sessions, clock=clock)
    )
    manager.apply_action(
        run_id,
        action,
        actor="test",
        request_id="current-profile",
        target_stage="agent_batches" if action is ReviewAction.RETRY_STAGE else None,
    )
    with database.sessions() as session:
        assert (
            session.get(ReviewRunRecord, run_id).repository_policy["review_profile_id"]
            is None
        )
        events = session.scalars(
            select(OutboxEventRecord).where(OutboxEventRecord.aggregate_id == run_id)
        ).all()
        assert any(
            event.payload.get("previous_review_profile_id") == "old-profile"
            and event.payload.get("configuration_source") == "current"
            for event in events
        )
