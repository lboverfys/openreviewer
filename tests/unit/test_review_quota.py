from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from domain.models import ReviewRequest
from persistence.database import Database
from persistence.models import Base, ReviewQuotaBucketRecord, ReviewRunRecord
from persistence.repositories import SqlAlchemyReviewRepository
from services.review_quota import ReviewQuotaExceededError, ReviewQuotaPolicy
from services.reviews import ReviewService


def _request(pr: int = 1) -> ReviewRequest:
    return ReviewRequest(
        installation_id=10,
        repository_id=42,
        repository="owner/repository",
        pull_request_number=pr,
        head_sha="a" * 40,
    )


def test_quota_is_atomic_and_idempotent(tmp_path: Path) -> None:
    database = Database.connect(f"sqlite:///{(tmp_path / 'quota.sqlite3').as_posix()}")
    Base.metadata.create_all(database.engine)
    try:
        policy = ReviewQuotaPolicy(
            user_hourly=1,
            repository_hourly=10,
            global_hourly=10,
            user_daily=10,
            repository_daily=10,
            global_daily=10,
        )
        repository = SqlAlchemyReviewRepository(
            database.sessions,
            clock=lambda: datetime(2026, 8, 30, 12, 30, tzinfo=UTC),
            quota_policy=policy,
        )
        service = ReviewService(repository)
        first = service.submit(_request(1), "same-key", actor="Alice")
        repeated = service.submit(_request(1), "same-key", actor="Alice")
        assert first.review_run_id == repeated.review_run_id

        with pytest.raises(ReviewQuotaExceededError) as caught:
            service.submit(_request(2), "different-key", actor="Alice")
        assert caught.value.scope == "user"
        assert caught.value.retry_after_seconds > 0

        with database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(ReviewRunRecord)) == 1
            rows = session.scalars(select(ReviewQuotaBucketRecord)).all()
            # 一个成功请求应产生 user/repository/global 各自的小时和日桶。
            assert len(rows) == 6
            assert {row.request_count for row in rows} == {1}
    finally:
        database.dispose()
