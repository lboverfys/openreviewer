"""每次 HTTP 请求固定查询数量的费用账本；与历史任务保留期分离。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ExecutionStatus
from domain.platform import MonthlyBudgetExceededError, month_start
from domain.repository_policy import RepositoryPolicy
from persistence.models import ModelUsageRequestRecord as Request
from persistence.models import RepositoryPolicyRecord, ReviewRunRecord, ReviewTaskRecord
from persistence.models import RepositoryUsageMonthRecord as Month
from persistence.platform_common import dialect_insert, platform_audit
from persistence.provider_channels import acquire_channel, lock_channel, settle_channel
from services.model_budget import ModelBudgetRequest, ModelBudgetReservation
from services.task_queue import ReviewTaskLease, TaskLeaseLostError


class SqlAlchemyUsageLedger:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.sessions = sessions
        self.clock = clock or (lambda: datetime.now(UTC))

    def reserve(
        self, lease: ReviewTaskLease, agent: str, request: ModelBudgetRequest
    ) -> ModelBudgetReservation:
        if (
            request.purpose not in {"review", "embedding", "rerank"}
            or not 1 <= len(agent) <= 32
        ):
            raise ValueError("模型请求类型或角色无效")
        if any(
            value < 0
            for value in (
                request.request_bytes,
                request.input_token_upper_bound,
                request.output_token_upper_bound,
            )
        ):
            raise ValueError("请求预占量不能为负数")
        if (
            request.cost_upper_bound_microusd is not None
            and request.cost_upper_bound_microusd < 0
        ):
            raise ValueError("请求预占费用不能为负数")
        now = self.clock()
        month = month_start(now)
        identifier = str(uuid4())
        with self.sessions() as session, session.begin():
            target = session.execute(
                select(
                    ReviewRunRecord.installation_id,
                    ReviewRunRecord.repository,
                    ReviewRunRecord.repository_key,
                )
                .join(
                    ReviewTaskRecord,
                    ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
                )
                .where(
                    ReviewRunRecord.id == lease.review_run_id,
                    ReviewTaskRecord.id == lease.task_id,
                    ReviewTaskRecord.execution_status == ExecutionStatus.RUNNING.value,
                    ReviewTaskRecord.lease_owner == lease.worker_id,
                    ReviewTaskRecord.attempt_count == lease.attempt_count,
                    ReviewTaskRecord.model_attempt_count == lease.model_attempt_count,
                    ReviewTaskRecord.ci_poll_count == lease.ci_poll_count,
                    ReviewTaskRecord.lease_expires_at > now,
                    or_(
                        ReviewTaskRecord.workflow_status.is_(None),
                        ReviewTaskRecord.workflow_status
                        != ExecutionStatus.PAUSED.value,
                    ),
                )
            ).one_or_none()
            if target is None:
                raise TaskLeaseLostError()
            # 当前预算属于运营控制；不从旧任务的策略快照读取。
            raw_policy = session.scalar(
                select(RepositoryPolicyRecord.policy)
                .where(
                    RepositoryPolicyRecord.repository_key == target.repository_key,
                )
                .with_for_update(read=True)
            )
            policy = RepositoryPolicy.model_validate(raw_policy or {})
            budget = policy.monthly_budget_microusd
            if budget is not None and request.cost_upper_bound_microusd is None:
                raise MonthlyBudgetExceededError(unknown_price=True)
            acquire_channel(session, request, now)
            cost = request.cost_upper_bound_microusd or 0
            month_id = sha256(
                f"{target.installation_id}:{target.repository_key}:{month.isoformat()}".encode()
            ).hexdigest()
            session.execute(
                dialect_insert(session, Month)
                .values(
                    id=month_id,
                    installation_id=target.installation_id,
                    repository=target.repository,
                    repository_key=target.repository_key,
                    month=month,
                    request_count=0,
                    input_tokens=0,
                    output_tokens=0,
                    estimated_cost_microusd=0,
                    reserved_cost_microusd=0,
                    unknown_count=0,
                    uncertain_count=0,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            statement = update(Month).where(Month.id == month_id)
            if budget is not None:
                statement = statement.where(
                    Month.estimated_cost_microusd + Month.reserved_cost_microusd + cost
                    <= budget
                )
            reserved = session.execute(
                statement.values(
                    request_count=Month.request_count + 1,
                    reserved_cost_microusd=Month.reserved_cost_microusd + cost,
                ).returning(
                    Month.request_count,
                    Month.estimated_cost_microusd,
                    Month.reserved_cost_microusd,
                )
            ).one_or_none()
            if reserved is None:
                raise MonthlyBudgetExceededError()
            session.add(
                Request(
                    id=identifier,
                    month_id=month_id,
                    review_run_id=lease.review_run_id,
                    installation_id=target.installation_id,
                    repository=target.repository,
                    repository_key=target.repository_key,
                    agent=agent,
                    purpose=request.purpose,
                    provider=request.provider,
                    model=request.model,
                    status="reserved",
                    reserved_cost_microusd=cost,
                    created_at=now,
                    connection_key=request.connection_key,
                    permit_expires_at=now + timedelta(seconds=request.timeout_seconds),
                )
            )
            if budget is not None:
                threshold = budget * policy.budget_warning_percent
                total = (
                    reserved.estimated_cost_microusd + reserved.reserved_cost_microusd
                )
                if (total - cost) * 100 < threshold <= total * 100:
                    platform_audit(
                        session,
                        "platform.budget.warning",
                        month_id,
                        target.repository,
                        "worker",
                        now,
                        details={
                            "installation_id": target.installation_id,
                            "budget_microusd": budget,
                        },
                    )
        return ModelBudgetReservation(
            id=identifier,
            review_plan_id=lease.review_plan_id or "",
            sequence=reserved.request_count,
            reserved_input_tokens=request.input_token_upper_bound,
            reserved_output_tokens=request.output_token_upper_bound,
            reserved_cost_microusd=cost,
            remaining_duration_ms=0,
        )

    def settle(
        self,
        reservation: ModelBudgetReservation,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        estimated_cost_microusd: int | None,
        response_status: int | None,
        duration_ms: int,
        uncertain: bool = False,
    ) -> None:
        values = (input_tokens, output_tokens, estimated_cost_microusd, duration_ms)
        if any(value is not None and value < 0 for value in values):
            raise ValueError("模型用量不能为负数")
        unknown = estimated_cost_microusd is None
        now = self.clock()
        with self.sessions() as session, session.begin():
            # 状态比较更新保证重复结算只影响一条记录；迟到结算仍计入原请求月份。
            connection_key = session.scalar(
                select(Request.connection_key).where(Request.id == reservation.id)
            )
            circuit = lock_channel(session, connection_key)
            row = session.execute(
                update(Request)
                .where(
                    Request.id == reservation.id,
                    Request.status == "reserved",
                )
                .values(
                    status="uncertain" if uncertain else "settled",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost_microusd=estimated_cost_microusd,
                    response_status=response_status,
                    duration_ms=duration_ms,
                    completed_at=now,
                )
                .returning(Request.month_id, Request.reserved_cost_microusd)
            ).one_or_none()
            if row is None:
                return
            # 用量不确定时保留预占；不能把超时或缺少 usage 的请求当作免费。
            released = (
                0
                if unknown
                else min(row.reserved_cost_microusd, estimated_cost_microusd or 0)
                if uncertain
                else row.reserved_cost_microusd
            )
            session.execute(
                update(Month)
                .where(Month.id == row.month_id)
                .values(
                    input_tokens=Month.input_tokens + (input_tokens or 0),
                    output_tokens=Month.output_tokens + (output_tokens or 0),
                    estimated_cost_microusd=Month.estimated_cost_microusd
                    + (estimated_cost_microusd or 0),
                    reserved_cost_microusd=Month.reserved_cost_microusd - released,
                    unknown_count=Month.unknown_count + int(unknown),
                    uncertain_count=Month.uncertain_count + int(uncertain),
                )
            )
            settle_channel(session, connection_key, circuit, response_status, now)
