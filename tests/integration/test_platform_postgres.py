"""真实 PostgreSQL 行锁下验证预算与仓库并发竞争，仅使用 CI 隔离库。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from sqlalchemy import func, select, update

from domain.platform import MonthlyBudgetExceededError
from domain.repository_policy import RepositoryPolicy
from persistence.models import (
    RepositoryPolicyRecord,
    RepositoryUsageMonthRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from services.team import RepositoryWrite, TeamService
from tests.integration.test_github_context_persistence import _submit
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)
from tests.integration.test_team_platform import request, setup_ledger
from tests.support import TEST_USERNAME


def test_competing_workers_cannot_overreserve_or_exceed_repository_cap(
    postgres_database,
):
    queue, lease, _, ledger = setup_ledger(postgres_database, budget=100)
    barrier = Barrier(2)

    def reserve(_):
        barrier.wait(timeout=10)
        try:
            return ledger.reserve(lease, "logic", request())
        except MonthlyBudgetExceededError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert sum(result is not None for result in results) == 1
    with postgres_database.sessions() as session:
        month = session.scalar(select(RepositoryUsageMonthRecord))
        assert month.reserved_cost_microusd == 60 and month.request_count == 1
        policy = session.scalar(
            select(RepositoryPolicyRecord).where(
                RepositoryPolicyRecord.repository_key == "lboverfys/niuma"
            )
        )
        policy_id, revision = policy.id, policy.revision
        session.execute(
            update(ReviewTaskRecord)
            .where(ReviewTaskRecord.id == lease.task_id)
            .values(execution_status="completed")
        )
        session.execute(
            update(ReviewRunRecord)
            .where(ReviewRunRecord.id == lease.review_run_id)
            .values(execution_status="completed")
        )
        session.commit()
    TeamService(postgres_database.sessions, TEST_USERNAME).save_repository(
        RepositoryWrite(
            repository="lboverfys/NiuMa",
            expected_revision=revision,
            policy=RepositoryPolicy(max_concurrent_reviews=1),
        ),
        TEST_USERNAME,
        policy_id,
    )
    _submit(postgres_database, "concurrent-one", "b" * 40)
    _submit(postgres_database, "concurrent-two", "c" * 40)
    barrier = Barrier(2)

    def claim(number):
        barrier.wait(timeout=10)
        return queue.claim_next(f"claim-{number}", timedelta(minutes=5))

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, range(2)))
    assert sum(item is not None for item in claimed) == 1
    with postgres_database.sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ReviewTaskRecord)
                .where(ReviewTaskRecord.execution_status == "running")
            )
            == 1
        )
