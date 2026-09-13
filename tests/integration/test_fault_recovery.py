"""仅 CI 隔离 PostgreSQL：真实终止子进程，验证不确定请求与旧租约处理。"""

import json
import multiprocessing
import os
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
import pytest
from sqlalchemy import select

from persistence.database import Database
from persistence.models import ModelUsageRequestRecord, RepositoryUsageMonthRecord
from persistence.usage import SqlAlchemyUsageLedger
from services.task_queue import TaskLeaseLostError
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)
from tests.integration.test_team_platform import request, setup_ledger


def _worker_after_response(url, lease, now, endpoint, pipe):
    database = Database.connect(url)
    try:
        ledger = SqlAlchemyUsageLedger(database.sessions, clock=lambda: now)
        reservation = ledger.reserve(lease, "logic", request())
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.post(endpoint, json={"fixture": "fault-recovery"})
            response.raise_for_status()
        pipe.send(reservation)
        pipe.recv()  # 父进程将在收到响应后、结算前终止此进程。
    finally:
        database.dispose()


def test_worker_killed_after_http_preserves_reservation_and_recovers_lease(postgres_database):
    queue, lease, clock, ledger = setup_ledger(postgres_database, budget=100)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append(True)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    worker = context.Process(target=_worker_after_response, args=(
        postgres_database.engine.url.render_as_string(hide_password=False), lease, clock[0],
        f"http://127.0.0.1:{server.server_port}/model", child,
    ))
    worker.start()
    try:
        assert parent.poll(20), "隔离故障进程未按时完成模拟 HTTP"
        reservation = parent.recv()
        worker.kill()
        worker.join(5)
        assert not worker.is_alive() and worker.exitcode != 0
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(5)
        parent.close()
        child.close()
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert len(received) == 1
    with postgres_database.sessions() as session:
        month = session.scalar(select(RepositoryUsageMonthRecord))
        assert month.reserved_cost_microusd == 60
        assert session.scalar(select(ModelUsageRequestRecord.status)) == "reserved"
    clock[0] += timedelta(hours=4)
    assert queue.recover_expired_leases() == 1
    with pytest.raises(TaskLeaseLostError):
        ledger.reserve(lease, "logic", request())
    clock[0] += timedelta(seconds=10)
    assert queue.claim_next("replacement-worker", timedelta(minutes=5)) is not None
    settlement = dict(input_tokens=10, output_tokens=5, estimated_cost_microusd=20,
                      response_status=200, duration_ms=10)
    ledger.settle(reservation, **settlement)
    ledger.settle(reservation, **settlement)
    with postgres_database.sessions() as session:
        month = session.scalar(select(RepositoryUsageMonthRecord))
        assert month.request_count == 1 and month.estimated_cost_microusd == 20
        assert month.reserved_cost_microusd == 0
    evidence = {"scenario": "worker_killed_after_http_before_settlement",
        "model_mode": "loopback_stub", "database": "isolated_postgresql",
        "external_requests": len(received), "retained_reservation_microusd": 60,
        "stale_worker_rejected": True, "replacement_claimed": True,
        "settlement_applied_once": True, "real_model_accuracy": None}
    output = os.environ.get("OPENREVIEWER_FAULT_REPORT_DIR")
    if output:
        directory = Path(output)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "worker-recovery.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, sort_keys=True))
