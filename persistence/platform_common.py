"""平台管理复用的事务内审计与 PostgreSQL/隔离测试写入适配。"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from persistence.models import OutboxEventRecord


def dialect_insert(session: Session, model):
    factory = (
        pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    )
    return factory(model)


def platform_audit(
    session: Session,
    event_type: str,
    object_id: str,
    repository: str,
    actor: str,
    now: datetime,
    *,
    revision: int | None = None,
    details: dict[str, object] | None = None,
) -> None:
    identifier = str(uuid4())
    session.add(
        OutboxEventRecord(
            id=identifier,
            event_key=f"platform:{identifier}",
            aggregate_type="platform",
            aggregate_id=object_id,
            event_type=event_type,
            occurred_at=now,
            publish_attempts=0,
            payload={
                "actor": actor,
                "repository": repository,
                "repository_key": repository.casefold(),
                "revision": revision,
                **(details or {}),
            },
        )
    )
