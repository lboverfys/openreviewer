from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from sqlalchemy import select

from persistence.auth import SqlAlchemyLoginAttemptLimiter
from persistence.database import Database
from persistence.models import Base, LoginRateLimitRecord
from services.auth import LoginRateLimitError


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def test_login_limit_is_shared_atomic_hashed_and_expires(tmp_path) -> None:
    path = (tmp_path / "login-limits.sqlite3").as_posix()
    database = Database.connect(f"sqlite:///{path}")
    Base.metadata.create_all(database.engine)
    now = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
    clock = MutableClock(now)
    key = "203.0.113.10|administrator"
    try:
        first_replica = SqlAlchemyLoginAttemptLimiter(
            database.sessions,
            maximum_attempts=3,
            window=timedelta(minutes=10),
            clock=clock,
        )
        second_replica = SqlAlchemyLoginAttemptLimiter(
            database.sessions,
            maximum_attempts=3,
            window=timedelta(minutes=10),
            clock=clock,
        )

        first_replica.consume(key)
        second_replica.consume(key)
        first_replica.consume(key)
        with pytest.raises(LoginRateLimitError) as blocked:
            second_replica.consume(key)
        assert 599 <= blocked.value.retry_after_seconds <= 601

        with database.sessions() as session:
            row = session.scalar(select(LoginRateLimitRecord))
            assert row is not None
            assert row.key_hash == sha256(key.encode("utf-8")).hexdigest()
            assert key not in repr(row)
            assert row.attempt_count == 4

        clock.value = now + timedelta(minutes=11)
        second_replica.consume(key)
        first_replica.reset(key)
        with database.sessions() as session:
            assert session.scalar(select(LoginRateLimitRecord)) is None
    finally:
        database.dispose()
