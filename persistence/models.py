"""SQLAlchemy records for durable review task submission."""

from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from domain.enums import (
    CoverageStatus,
    ExecutionStatus,
    ReviewConclusion,
    WorkerStatus,
)


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    return datetime.now(UTC)


def enum_values(enum_type: type[Enum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


class ReviewRunRecord(Base):
    __tablename__ = "review_runs"
    __table_args__ = (
        CheckConstraint(
            f"execution_status IN ({enum_values(ExecutionStatus)})",
            name="execution_status_value",
        ),
        CheckConstraint(
            "review_conclusion IS NULL OR "
            f"review_conclusion IN ({enum_values(ReviewConclusion)})",
            name="review_conclusion_value",
        ),
        CheckConstraint(
            f"coverage_status IN ({enum_values(CoverageStatus)})",
            name="coverage_status_value",
        ),
        CheckConstraint("repository_id > 0", name="repository_id_positive"),
        CheckConstraint(
            "pull_request_number > 0",
            name="pull_request_number_positive",
        ),
        CheckConstraint("installation_id > 0", name="installation_id_positive"),
        UniqueConstraint("idempotency_key"),
        Index(
            "ix_review_runs_version_created",
            "review_version_key",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_version_key: Mapped[str] = mapped_column(String(360), nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_status: Mapped[str] = mapped_column(String(32), nullable=False)
    review_conclusion: Mapped[str | None] = mapped_column(String(32))
    coverage_status: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class ReviewTaskRecord(Base):
    __tablename__ = "review_tasks"
    __table_args__ = (
        CheckConstraint(
            f"execution_status IN ({enum_values(ExecutionStatus)})",
            name="execution_status_value",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        UniqueConstraint("review_run_id"),
        Index(
            "ix_review_tasks_claimable",
            "execution_status",
            "available_at",
            "priority",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("review_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_status: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=100)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class WorkerHeartbeatRecord(Base):
    """Last known state for one worker process.

    Heartbeats are deliberately durable so the dashboard can distinguish an
    idle worker from a crashed or disconnected worker without introducing
    Redis merely for presence tracking.
    """

    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({enum_values(WorkerStatus)})",
            name="status_value",
        ),
        Index("ix_worker_heartbeats_last_seen", "last_seen_at"),
    )

    worker_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_task_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("review_tasks.id", ondelete="SET NULL"),
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class OutboxEventRecord(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "publish_attempts >= 0",
            name="publish_attempts_nonnegative",
        ),
        UniqueConstraint("event_key"),
        Index("ix_outbox_events_pending", "published_at", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(200), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
