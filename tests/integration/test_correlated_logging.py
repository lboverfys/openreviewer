"""关联日志不得泄露正文，异步请求和 Agent 线程不得串用身份。"""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import httpx
import pytest
from fastapi.responses import StreamingResponse

from apps.api.main import create_app
from apps.worker.main import WorkerRuntime, WorkerSettings
from domain.logging import (
    JsonLogFormatter,
    log_context,
    log_event,
    normalize_request_id,
)
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from services.agent_workflow import PARALLEL_AGENTS, FixedAgentWorkflow
from tests.integration.test_team_platform import request, settle, setup_ledger
from tests.integration.test_worker import database as database
from tests.integration.test_worker import submit_review
from tests.unit.test_model_review import make_model_input
from tests.unit.test_workflow import model_result


@pytest.mark.parametrize("value", [None, "", "line\ninjection", "x" * 65, "含中文", "a b"])
def test_invalid_request_ids_are_replaced(value):
    result = normalize_request_id(value)
    assert len(result) == 32 and result != value
    assert normalize_request_id("valid-Request_123.abc") == "valid-Request_123.abc"


def test_json_formatter_drops_messages_nested_bodies_and_exception_text():
    record = logging.LogRecord("openreviewer", logging.ERROR, __file__, 1,
        "private-source %s", ("password=never-print",), None)
    record.event = "safe_event"
    record.error_code = "model_timeout"
    record.request_id = "password=secret-value"
    record.details = {"response": "private-source", "Cookie": "session=private-cookie"}
    record.exc_text = "private-source exception password=never-print"
    record.agent = {"nested": {"api_key": "hidden-key"}}
    output = JsonLogFormatter().format(record)
    assert json.loads(output)["error_code"] == "model_timeout"
    for secret in ("private-source", "never-print", "secret-value", "private-cookie", "hidden-key"):
        assert secret not in output


def test_api_concurrent_requests_errors_and_stream_have_correlated_ids(caplog):
    caplog.set_level(logging.INFO, logger="openreviewer")
    app = create_app()

    @app.get("/probe/{identity}")
    async def probe(identity: str):
        await asyncio.sleep(0)
        log_event("probe", review_run_id=identity)
        return {"ok": True}

    @app.get("/failure")
    async def failure():
        raise RuntimeError("private-source exception")

    @app.get("/stream")
    async def stream():
        async def body():
            yield "data: one\n\n"
            await asyncio.sleep(0)
            yield "data: two\n\n"
        return StreamingResponse(body(), media_type="text/event-stream")

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver") as client:
            responses = await asyncio.gather(*(client.get(f"/probe/{identity}", headers={"X-Request-ID":identity}) for identity in ("first", "second")))
            assert [item.headers["x-request-id"] for item in responses] == ["first", "second"]
            failure = await client.get("/failure", headers={"X-Request-ID":"failure-id"})
            assert failure.status_code == 500 and failure.headers["x-request-id"] == "failure-id"
            assert "private-source" not in failure.text
            stream = await client.get("/stream", headers={"X-Request-ID":"stream-id"})
            assert stream.text.count("data:") == 2
            rejected = await client.get("/api/v1/reviews", headers={"X-Request-ID":"bad id"})
            assert rejected.status_code in (401, 503)
            assert len(rejected.headers["x-request-id"]) == 32

    asyncio.run(exercise())
    probes = [record for record in caplog.records if getattr(record, "event", None) == "probe"]
    assert {(record.request_id, record.review_run_id) for record in probes} == {("first", "first"), ("second", "second")}
    assert len([record for record in caplog.records if getattr(record, "request_id", None) == "stream-id"]) == 1
    log_event("after_request")
    assert not hasattr(caplog.records[-1], "request_id")


def test_agent_threads_propagate_each_workflow_context_without_cross_talk(caplog):
    caplog.set_level(logging.INFO, logger="openreviewer")
    barrier = Barrier(6)

    class Reviewer:
        def review(self, review_input):
            barrier.wait(timeout=10)
            log_event("agent_probe", agent=review_input.review_agent.value,
                review_task_id=review_input.review_run_id)
            return model_result("1")

    def run(identity):
        with log_context(review_run_id=identity):
            result = FixedAgentWorkflow({agent:Reviewer() for agent in PARALLEL_AGENTS}, max_concurrency=3).run(
                make_model_input().model_copy(update={"review_run_id":identity}))
            assert result.status == "completed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, ("run-one", "run-two")))
    probes = [record for record in caplog.records if getattr(record, "event", None) == "agent_probe"]
    assert len(probes) == 6
    assert all(record.review_run_id == record.review_task_id for record in probes)
    assert {record.review_run_id for record in probes} == {"run-one", "run-two"}


def test_submission_request_id_links_to_worker_run_and_scope_resets(database, caplog):
    caplog.set_level(logging.INFO, logger="openreviewer")
    with log_context(request_id="submission-id"):
        task_id = submit_review(database)
    runtime = WorkerRuntime(SqlAlchemyReviewTaskQueue(database.sessions), WorkerSettings(
        worker_id="logging-test", poll_interval=timedelta(seconds=1), lease_duration=timedelta(seconds=30)))
    assert runtime.run_once()
    records = {record.event:record for record in caplog.records if hasattr(record, "event")}
    submitted, claimed = records["review_submitted"], records["worker_task_claimed"]
    assert submitted.request_id == "submission-id"
    assert submitted.review_task_id == claimed.review_task_id == task_id
    assert submitted.review_run_id == claimed.review_run_id
    assert not hasattr(claimed, "request_id")
    log_event("outside_worker")
    assert not hasattr(caplog.records[-1], "review_run_id")


def test_model_attempt_and_settlement_link_to_the_same_run_and_request(database, caplog):
    caplog.set_level(logging.INFO, logger="openreviewer")
    _, lease, _, ledger = setup_ledger(database)
    permit = ledger.reserve(lease, "logic", request())
    settle(ledger, permit, status=503, cost=None, uncertain=True)
    records = {record.event:record for record in caplog.records if hasattr(record, "event")}
    reserved, settled = records["model_request_reserved"], records["model_request_settled"]
    assert reserved.review_run_id == settled.review_run_id == lease.review_run_id
    assert reserved.usage_request_id == settled.usage_request_id == permit.id
    assert settled.response_status == 503 and settled.status == "uncertain"
    assert reserved.attempt_kind == "initial" and reserved.agent == settled.agent == "logic"
