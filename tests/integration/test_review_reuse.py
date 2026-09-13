"""复用存储必须遵守来源清理、七天有效期和任务租约约束。"""

from datetime import timedelta

import pytest
from sqlalchemy import delete, select, update

from persistence.models import ReviewReuseRecord, ReviewRunRecord, ReviewTaskRecord
from services.task_queue import TaskLeaseLostError
from tests.integration.test_github_context_persistence import _submit
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)
from tests.integration.test_team_platform import setup_ledger
from tests.unit.test_model_review import make_output, make_result


def test_reuse_obeys_ttl_source_cleanup_and_old_worker_fencing(postgres_database):
    database = postgres_database
    queue, lease, clock, _ = setup_ledger(database)
    key = "c" * 64
    payload = make_result(make_output(), "b" * 64).model_dump(mode="json")
    queue.store_reusable_review(lease, key, "a" * 40, payload)
    assert queue.load_reused_review(lease, key) is None
    with database.sessions() as session, session.begin():
        session.execute(update(ReviewTaskRecord).where(ReviewTaskRecord.id == lease.task_id)
                        .values(execution_status="completed"))
        session.execute(update(ReviewRunRecord).where(ReviewRunRecord.id == lease.review_run_id)
                        .values(execution_status="completed"))
    _, second_run = _submit(database, "reuse-next-commit", "d" * 40)
    current = queue.claim_next("next-worker", timedelta(minutes=5))
    assert current and current.review_run_id == second_run
    cached = queue.load_reused_review(current, key)
    assert cached[0] == lease.review_run_id and cached[1] == "a" * 40
    assert cached[2] == payload
    with pytest.raises(TaskLeaseLostError):
        queue.store_reusable_review(lease, key, "a" * 40, payload)
    with database.sessions() as session, session.begin():
        session.execute(update(ReviewReuseRecord).where(ReviewReuseRecord.key == key)
                        .values(created_at=clock[0] - timedelta(days=8)))
    assert queue.load_reused_review(current, key) is None
    with database.sessions() as session, session.begin():
        session.execute(delete(ReviewRunRecord).where(ReviewRunRecord.id == lease.review_run_id))
    with database.sessions() as session:
        assert session.scalar(select(ReviewReuseRecord.key).where(ReviewReuseRecord.key == key)) is None
