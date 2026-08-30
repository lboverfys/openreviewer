"""管理员会话的 SQLAlchemy 持久化适配器。"""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from persistence.models import AdminSessionRecord, LoginRateLimitRecord
from services.auth import (
    AuthPersistenceError,
    LoginLimiter,
    LoginRateLimitError,
    SessionStore,
)
from services.rbac import AccessRole


class SqlAlchemySessionStore(SessionStore):
    """跨 API 副本共享的可吊销会话仓储。"""

    _EXPIRED_SESSION_DELETE_LIMIT = 500

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(
        self,
        session_hash: str,
        username: str,
        role: AccessRole,
        issued_at: datetime,
        expires_at: datetime,
    ) -> None:
        with self._sessions() as session:
            try:
                expired_sessions = (
                    select(AdminSessionRecord.session_hash)
                    .where(AdminSessionRecord.expires_at <= issued_at)
                    .order_by(AdminSessionRecord.expires_at)
                    .limit(self._EXPIRED_SESSION_DELETE_LIMIT)
                )
                # 清理量固定受限，查询使用 expires_at 索引，不会让一次登录承担全表删除。
                session.execute(
                    delete(AdminSessionRecord).where(
                        AdminSessionRecord.session_hash.in_(expired_sessions)
                    )
                )
                session.add(
                    AdminSessionRecord(
                        session_hash=session_hash,
                        username=username,
                        role=role.value,
                        issued_at=issued_at,
                        expires_at=expires_at,
                    )
                )
                session.commit()
            except SQLAlchemyError as exc:
                session.rollback()
                raise AuthPersistenceError("administrator session could not be created") from exc

    def is_active(
        self,
        session_hash: str,
        username: str,
        role: AccessRole,
        now: datetime,
    ) -> bool:
        with self._sessions() as session:
            try:
                return bool(
                    session.scalar(
                        select(AdminSessionRecord.session_hash).where(
                            AdminSessionRecord.session_hash == session_hash,
                            AdminSessionRecord.username == username,
                            AdminSessionRecord.role == role.value,
                            AdminSessionRecord.revoked_at.is_(None),
                            AdminSessionRecord.expires_at > now,
                        )
                    )
                )
            except SQLAlchemyError as exc:
                raise AuthPersistenceError("administrator session could not be verified") from exc

    def revoke(self, session_hash: str, revoked_at: datetime) -> None:
        with self._sessions() as session:
            try:
                session.execute(
                    update(AdminSessionRecord)
                    .where(
                        AdminSessionRecord.session_hash == session_hash,
                        AdminSessionRecord.revoked_at.is_(None),
                    )
                    .values(revoked_at=revoked_at)
                )
                session.commit()
            except SQLAlchemyError as exc:
                session.rollback()
                raise AuthPersistenceError("administrator session could not be revoked") from exc

    def revoke_all(self, username: str, revoked_at: datetime) -> int:
        with self._sessions() as session:
            try:
                result = session.execute(
                    update(AdminSessionRecord)
                    .where(
                        AdminSessionRecord.username == username,
                        AdminSessionRecord.revoked_at.is_(None),
                        AdminSessionRecord.expires_at > revoked_at,
                    )
                    .values(revoked_at=revoked_at)
                )
                session.commit()
                return int(getattr(result, "rowcount", 0) or 0)
            except SQLAlchemyError as exc:
                session.rollback()
                raise AuthPersistenceError(
                    "administrator sessions could not be revoked"
                ) from exc


class SqlAlchemyLoginAttemptLimiter(LoginLimiter):
    """通过原子 UPSERT 在所有 API 副本间共享登录尝试配额。"""

    _EXPIRED_DELETE_LIMIT = 100

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        maximum_attempts: int = 5,
        window: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if maximum_attempts <= 0 or window.total_seconds() <= 0:
            raise ValueError("login limiter values must be positive")
        self._sessions = sessions
        self._maximum_attempts = maximum_attempts
        self._window = window
        self._clock = clock or (lambda: datetime.now(UTC))

    def consume(self, key: str) -> None:
        now = self._clock().astimezone(UTC)
        cutoff = now - self._window
        key_hash = self._key_hash(key)
        with self._sessions() as session:
            try:
                expired_keys = (
                    select(LoginRateLimitRecord.key_hash)
                    .where(
                        LoginRateLimitRecord.updated_at
                        <= now - (self._window * 2)
                    )
                    .order_by(LoginRateLimitRecord.updated_at.asc())
                    .limit(self._EXPIRED_DELETE_LIMIT)
                )
                session.execute(
                    delete(LoginRateLimitRecord).where(
                        LoginRateLimitRecord.key_hash.in_(expired_keys)
                    )
                )

                dialect = session.get_bind().dialect.name
                window_expired = LoginRateLimitRecord.window_started_at <= cutoff
                update_values = {
                    "attempt_count": case(
                        (window_expired, 1),
                        (
                            LoginRateLimitRecord.attempt_count
                            >= self._maximum_attempts,
                            self._maximum_attempts + 1,
                        ),
                        else_=LoginRateLimitRecord.attempt_count + 1,
                    ),
                    "window_started_at": case(
                        (window_expired, now),
                        else_=LoginRateLimitRecord.window_started_at,
                    ),
                    "updated_at": now,
                }
                if dialect == "postgresql":
                    statement = (
                        postgresql_insert(LoginRateLimitRecord)
                        .values(
                            key_hash=key_hash,
                            attempt_count=1,
                            window_started_at=now,
                            updated_at=now,
                        )
                        .on_conflict_do_update(
                            index_elements=[LoginRateLimitRecord.key_hash],
                            set_=update_values,
                        )
                        .returning(
                            LoginRateLimitRecord.attempt_count,
                            LoginRateLimitRecord.window_started_at,
                        )
                    )
                    attempt_count, window_started_at = session.execute(statement).one()
                elif dialect == "sqlite":
                    sqlite_statement = (
                        sqlite_insert(LoginRateLimitRecord)
                        .values(
                            key_hash=key_hash,
                            attempt_count=1,
                            window_started_at=now,
                            updated_at=now,
                        )
                        .on_conflict_do_update(
                            index_elements=[LoginRateLimitRecord.key_hash],
                            set_=update_values,
                        )
                        .returning(
                            LoginRateLimitRecord.attempt_count,
                            LoginRateLimitRecord.window_started_at,
                        )
                    )
                    attempt_count, window_started_at = session.execute(
                        sqlite_statement
                    ).one()
                else:
                    raise AuthPersistenceError(
                        "login rate limiting requires PostgreSQL or SQLite"
                    )
                session.commit()
            except AuthPersistenceError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise AuthPersistenceError(
                    "login rate limit state could not be updated"
                ) from exc

        if attempt_count > self._maximum_attempts:
            if window_started_at.tzinfo is None:
                window_started_at = window_started_at.replace(tzinfo=UTC)
            retry_at = window_started_at.astimezone(UTC) + self._window
            remaining = max(1, int((retry_at - now).total_seconds()) + 1)
            raise LoginRateLimitError(remaining)

    def reset(self, key: str) -> None:
        with self._sessions() as session:
            try:
                session.execute(
                    delete(LoginRateLimitRecord).where(
                        LoginRateLimitRecord.key_hash == self._key_hash(key)
                    )
                )
                session.commit()
            except SQLAlchemyError as exc:
                session.rollback()
                raise AuthPersistenceError(
                    "login rate limit state could not be reset"
                ) from exc

    @staticmethod
    def _key_hash(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()
