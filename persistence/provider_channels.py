"""按连接身份共享的并发准入和熔断；事务不覆盖模型 HTTP。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from domain.platform import ModelChannelUnavailableError
from persistence.models import ModelUsageRequestRecord, ProviderCircuitRecord
from persistence.platform_common import dialect_insert
from services.model_budget import ModelBudgetRequest

PROVIDER_CONCURRENCY = 3
FAILURE_THRESHOLD = 5
OPEN_SECONDS = 60


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def lock_channel(session: Session, key: str | None):
    if key is None:
        return None
    return session.execute(
        select(ProviderCircuitRecord.failure_count, ProviderCircuitRecord.open_until)
        .where(
            ProviderCircuitRecord.connection_key == key,
        )
        .with_for_update()
    ).one_or_none()


def acquire_channel(
    session: Session, request: ModelBudgetRequest, now: datetime
) -> None:
    key = request.connection_key
    if key is None:
        return
    if len(key) != 64 or not 1 <= request.timeout_seconds <= 86_400:
        raise ValueError("模型通道身份或请求租约无效")
    session.execute(
        dialect_insert(session, ProviderCircuitRecord)
        .values(
            connection_key=key,
            provider=request.provider,
            failure_count=0,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["connection_key"], set_={"updated_at": now}
        )
    )
    circuit = lock_channel(session, key)
    if circuit is None:
        raise ModelChannelUnavailableError(now)
    if circuit.open_until is not None and _utc(circuit.open_until) > now:
        raise ModelChannelUnavailableError(now, retry_at=_utc(circuit.open_until))
    active = (
        session.scalar(
            select(func.count())
            .select_from(ModelUsageRequestRecord)
            .where(
                ModelUsageRequestRecord.connection_key == key,
                ModelUsageRequestRecord.status == "reserved",
                ModelUsageRequestRecord.permit_expires_at > now,
            )
        )
        or 0
    )
    # 半开阶段只允许一个请求探测恢复；其它任务继续退避。
    limit = 1 if circuit.failure_count >= FAILURE_THRESHOLD else PROVIDER_CONCURRENCY
    if active >= limit:
        raise ModelChannelUnavailableError(now)


def settle_channel(
    session: Session,
    key: str | None,
    circuit,
    response_status: int | None,
    now: datetime,
) -> None:
    if key is None or circuit is None:
        return
    failure = (
        response_status is None or response_status == 429 or response_status >= 500
    )
    if failure:
        count = circuit.failure_count + 1
        values = {
            "failure_count": count,
            "open_until": now + timedelta(seconds=OPEN_SECONDS)
            if count >= FAILURE_THRESHOLD
            else None,
        }
    elif (
        response_status is not None
        and 200 <= response_status < 300
        and (circuit.open_until is None or _utc(circuit.open_until) <= now)
    ):
        values = {"failure_count": 0, "open_until": None}
    else:
        return
    session.execute(
        update(ProviderCircuitRecord)
        .where(ProviderCircuitRecord.connection_key == key)
        .values(**values, updated_at=now)
    )
