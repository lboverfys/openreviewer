"""真实时间与子进程租约恢复；缩短测试租约，不改生产配置、不调用模型。"""

import json
import multiprocessing
import os
import time
from datetime import timedelta
from pathlib import Path

import pytest

from domain.models import ReviewRequest
from domain.security import ErrorCode, SafeError
from persistence.database import Database
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from services.reviews import ReviewService
from services.task_queue import TaskLeaseLostError
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)


def hold_lease(url, pipe):
    database = Database.connect(url)
    try:
        queue = SqlAlchemyReviewTaskQueue(database.sessions)
        lease = queue.claim_next("timed-worker", timedelta(seconds=2))
        pipe.send(lease)
        pipe.recv()
    finally:
        database.dispose()


def test_real_clock_recovery_after_process_exit(postgres_database):
    queue = SqlAlchemyReviewTaskQueue(postgres_database.sessions, retry_base_seconds=1)
    service = ReviewService(SqlAlchemyReviewRepository(postgres_database.sessions))
    timings = []
    for number in range(3):
        service.submit(ReviewRequest(installation_id=10, repository_id=97,
            repository="timing/recovery", pull_request_number=number + 1,
            head_sha=f"{number + 1:040x}"), f"wall-clock-{number}")
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=hold_lease, args=(
            postgres_database.engine.url.render_as_string(hide_password=False), child))
        process.start()
        try:
            assert parent.poll(15), "子进程未取得任务"
            stale = parent.recv()
            assert stale is not None
            started = time.monotonic()
            process.kill()
            process.join(5)
            assert not process.is_alive()
            replacement = None
            while time.monotonic() - started < 15:
                queue.recover_expired_leases()
                replacement = queue.claim_next("replacement", timedelta(seconds=30))
                if replacement is not None:
                    break
                time.sleep(0.05)
            assert replacement is not None and replacement.task_id == stale.task_id
            timings.append(round((time.monotonic() - started) * 1000, 2))
            with pytest.raises(TaskLeaseLostError):
                queue.renew_lease(stale, timedelta(seconds=30))
            queue.retry_or_fail(replacement, SafeError(ErrorCode.TASK_LEASE_EXPIRED,
                "结束隔离恢复测试", retryable=False))
        finally:
            if process.is_alive():
                process.kill()
                process.join(5)
            parent.close()
            child.close()
        assert not process.is_alive()
    report = {"scenario": "real_clock_worker_exit_to_reclaim", "samples": timings,
        "p50_ms": sorted(timings)[1], "p95_ms": max(timings), "sample_count": 3,
        "lease_seconds": 2, "retry_base_seconds": 1, "poll_seconds": 0.05,
        "stale_writes_rejected": 3, "clock": "real", "model_requests": 0,
        "scope": "isolated PostgreSQL; shortened lease; three-sample smoke, not production RTO"}
    if directory := os.environ.get("OPENREVIEWER_FAULT_REPORT_DIR"):
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / "worker-recovery-timing.json").write_text(json.dumps(report), encoding="utf-8")
    print(json.dumps(report))
