"""同一数据库下测量 1/2/4/8 个领取者，使用模拟处理，不代表模型吞吐。"""

import json
import math
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock

import pytest
from sqlalchemy import update

from domain.models import ReviewRequest
from domain.repository_policy import RepositoryPolicy
from persistence.models import ReviewRunRecord, ReviewTaskRecord
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from services.reviews import ReviewService
from services.team import RepositoryWrite, TeamService
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)
from tests.support import TEST_USERNAME


@pytest.mark.parametrize("workers", [1, 2, 4, 8])
def test_queue_claim_capacity_and_repository_fairness(postgres_database, workers):
    repositories = [f"capacity{workers}/{name}" for name in ("hot", "small-a", "small-b", "small-c")]
    for repo in repositories:
        TeamService(postgres_database.sessions, TEST_USERNAME).save_repository(
            RepositoryWrite(repository=repo, expected_revision=0,
                policy=RepositoryPolicy(max_concurrent_reviews=2)), TEST_USERNAME)
    service = ReviewService(SqlAlchemyReviewRepository(postgres_database.sessions))
    source = {}
    for number in range(40):
        repo = repositories[0] if number < 28 else repositories[1 + (number - 28) % 3]
        item = service.submit(ReviewRequest(installation_id=10, repository_id=97 + workers,
            repository=repo, pull_request_number=number + 1,
            head_sha=f"{workers * 100 + number:040x}"), f"capacity-{workers}-{number}")
        source[item.review_task_id] = repo
    queue = SqlAlchemyReviewTaskQueue(postgres_database.sessions)
    barrier, lock = Barrier(workers), Lock()
    completed, active, peaks = {}, Counter(), Counter()
    attempts = Counter()
    started = time.monotonic()

    def work(index):
        barrier.wait(timeout=10)
        while time.monotonic() - started < 30:
            with lock:
                if len(completed) == len(source):
                    return
            begin = time.monotonic()
            lease = queue.claim_next(f"capacity-{workers}-{index}", timedelta(seconds=15))
            elapsed = (time.monotonic() - begin) * 1000
            with lock:
                attempts["total"] += 1
                attempts["empty"] += int(lease is None)
            if lease is None:
                time.sleep(0.005)
                continue
            repo = source[lease.task_id]
            wait = (time.monotonic() - started) * 1000
            with lock:
                active[repo] += 1
                peaks[repo] = max(peaks[repo], active[repo])
                assert active[repo] <= 2
                assert lease.task_id not in completed
            time.sleep(0.02)
            # 模拟处理器结束；使用真实租约保护写回，不执行 GitHub/模型阶段。
            with postgres_database.sessions() as session, session.begin():
                queue._locked_owned_task(session, lease, datetime.now(UTC))
                session.execute(update(ReviewTaskRecord).where(ReviewTaskRecord.id == lease.task_id)
                    .values(execution_status="completed", lease_owner=None, lease_expires_at=None))
                session.execute(update(ReviewRunRecord).where(ReviewRunRecord.id == lease.review_run_id)
                    .values(execution_status="completed"))
                with lock:
                    active[repo] -= 1
            with lock:
                completed[lease.task_id] = (repo, elapsed, wait)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, range(workers)))
    duration = time.monotonic() - started
    assert len(completed) == len(source)
    values = sorted(row[1] for row in completed.values())
    print(json.dumps({"scope": "queue_claim_with_simulated_20ms_handler", "workers": workers,
        "tasks": len(source), "seconds": round(duration, 3), "tasks_per_second": round(len(source) / duration, 2),
        "claim_p95_ms": round(values[math.ceil(len(values) * .95) - 1], 2),
        "polls": dict(attempts), "peak_repository_concurrency": dict(peaks),
        "max_queue_wait_ms": {repo: round(max(row[2] for row in completed.values() if row[0] == repo), 2) for repo in repositories},
        "duplicate_claims": 0, "model_requests": 0}))
