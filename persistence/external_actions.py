"""ExternalActionRecord 的短事务存储实现。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ExternalActionState
from domain.security import redact_sensitive
from persistence.models import ExternalActionRecord
from services.external_actions import ExternalActionBusyError, ExternalActionStore
from services.github import GitHubCallAudit


class SqlAlchemyExternalActionStore(ExternalActionStore):
    """用行锁和短租约串行化同一个外部副作用。"""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], object] | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("external action lease duration must be positive")
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory or uuid4
        self._lease_duration = lease_duration

    def acquire(
        self,
        *,
        action_key: str,
        review_run_id: str,
        action_type: str,
        owner: str,
        request_method: str,
        request_path: str,
    ) -> str | None:
        now = self._now()
        with self._sessions() as session:
            try:
                row = session.scalar(
                    select(ExternalActionRecord)
                    .where(ExternalActionRecord.action_key == action_key)
                    .with_for_update()
                )
                if row is None:
                    row = ExternalActionRecord(
                        id=str(self._uuid_factory()),
                        action_key=action_key,
                        review_run_id=review_run_id,
                        action_type=action_type,
                        state=ExternalActionState.RUNNING.value,
                        attempt_count=1,
                        lease_owner=owner,
                        lease_expires_at=now + self._lease_duration,
                        request_method=request_method,
                        request_path=request_path,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                else:
                    if row.review_run_id != review_run_id or row.action_type != action_type:
                        raise ValueError("external action key is bound to another action")
                    if (
                        row.state == ExternalActionState.SUCCEEDED.value
                        and row.remote_id
                    ):
                        session.commit()
                        return row.remote_id
                    lease_expires_at = _as_utc(row.lease_expires_at)
                    if (
                        row.state == ExternalActionState.RUNNING.value
                        and row.lease_owner not in {None, owner}
                        and lease_expires_at is not None
                        and lease_expires_at > now
                    ):
                        raise ExternalActionBusyError("external action is already running")
                    row.state = ExternalActionState.RUNNING.value
                    row.attempt_count += 1
                    row.lease_owner = owner
                    row.lease_expires_at = now + self._lease_duration
                    row.request_method = request_method
                    row.request_path = request_path
                    row.updated_at = now
                    row.completed_at = None
                session.commit()
                return None
            except ExternalActionBusyError:
                session.rollback()
                raise
            except (SQLAlchemyError, ValueError):
                session.rollback()
                raise

    def succeed(
        self,
        *,
        action_key: str,
        owner: str,
        remote_id: str,
        audit: GitHubCallAudit,
    ) -> None:
        now = self._now()
        with self._sessions() as session:
            try:
                row = self._locked_owned_row(session, action_key, owner)
                if row is None:
                    raise RuntimeError("external action lease was lost")
                row.state = ExternalActionState.SUCCEEDED.value
                row.remote_id = remote_id[:200]
                _copy_audit(row, audit)
                row.last_error_code = None
                row.last_error = None
                row.last_error_retryable = None
                row.last_error_details = None
                row.lease_owner = None
                row.lease_expires_at = None
                row.completed_at = now
                row.updated_at = now
                session.commit()
            except (SQLAlchemyError, RuntimeError):
                session.rollback()
                raise

    def fail(
        self,
        *,
        action_key: str,
        owner: str,
        error_code: str,
        error_message: str,
        retryable: bool,
        details: Mapping[str, object] | None = None,
        audit: GitHubCallAudit | None = None,
    ) -> None:
        now = self._now()
        with self._sessions() as session:
            try:
                row = self._locked_owned_row(session, action_key, owner)
                if row is None:
                    return
                row.state = ExternalActionState.FAILED.value
                if audit is not None:
                    _copy_audit(row, audit)
                row.last_error_code = error_code[:64]
                row.last_error = redact_sensitive(error_message)[:4000]
                row.last_error_retryable = retryable
                row.last_error_details = (
                    redact_sensitive(dict(details)) if details is not None else None
                )
                row.lease_owner = None
                row.lease_expires_at = None
                row.completed_at = None
                row.updated_at = now
                session.commit()
            except SQLAlchemyError:
                session.rollback()
                raise

    @staticmethod
    def _locked_owned_row(
        session: Session,
        action_key: str,
        owner: str,
    ) -> ExternalActionRecord | None:
        row = session.scalar(
            select(ExternalActionRecord)
            .where(
                ExternalActionRecord.action_key == action_key,
                ExternalActionRecord.lease_owner == owner,
            )
            .with_for_update()
        )
        return row

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _copy_audit(row: ExternalActionRecord, audit: GitHubCallAudit) -> None:
    row.request_method = audit.request_method
    row.request_path = audit.request_path
    row.response_status = audit.response_status
    row.github_request_id = audit.github_request_id
    row.duration_ms = audit.duration_ms
    row.rate_limit_remaining = audit.rate_limit_remaining
