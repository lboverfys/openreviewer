"""每次 HTTP 请求固定查询数量的费用账本；与历史任务保留期分离。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ExecutionStatus
from domain.evaluation_outputs import MAX_RUN_OUTPUT_BYTES, CapturedModelOutput
from domain.logging import log_event
from domain.model_review import ModelTokenUsage
from domain.platform import MonthlyBudgetExceededError, UsageCostReason, month_start
from domain.repository_policy import RepositoryPolicy
from persistence.models import (
    EvaluationModelOutputRecord,
    RepositoryPolicyRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.models import ModelUsageRequestRecord as Request
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
        capture_requested = False
        with self.sessions() as session, session.begin():
            target = session.execute(
                select(
                    ReviewRunRecord.installation_id,
                    ReviewRunRecord.repository,
                    ReviewRunRecord.repository_key,
                    ReviewRunRecord.capture_model_outputs,
                    ReviewRunRecord.head_sha,
                    ReviewRunRecord.repository_policy,
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
                    cost_reason="pending",
                    pricing_snapshot=request.pricing_snapshot,
                    reserved_cost_microusd=cost,
                    created_at=now,
                    connection_key=request.connection_key,
                    permit_expires_at=now + timedelta(seconds=request.timeout_seconds),
                )
            )
            source = request.output_source
            capture_requested = target.capture_model_outputs and request.purpose == "review" and source is not None
            if capture_requested and source is not None:
                if source.review_run_id != lease.review_run_id or source.review_plan_id != lease.review_plan_id or source.head_sha != target.head_sha:
                    raise ValueError("评测输出来源与当前任务不一致")
                if not request.request_sha256:
                    raise ValueError("评测输出缺少请求指纹")
                session.add(EvaluationModelOutputRecord(
                    id=identifier, review_run_id=source.review_run_id, review_plan_id=source.review_plan_id,
                    installation_id=target.installation_id, repository=target.repository, repository_key=target.repository_key,
                    head_sha=source.head_sha, profile_id=(target.repository_policy or {}).get("review_profile_id"),
                    agent=agent, batch_number=source.batch_number, split_depth=source.split_depth,
                    request_sequence=reserved.request_count, attempt_kind=request.attempt_kind,
                    provider=request.provider, model=request.model, api_protocol=request.api_protocol,
                    prompt_content_sha256=source.prompt_content_sha256, request_sha256=request.request_sha256,
                    application_revision=source.application_revision, status="pending",
                    created_at=now, expires_at=now + timedelta(days=30),
                ))
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
                        event_key=f"platform:budget:{month_id}:{budget}:{policy.budget_warning_percent}",
                        details={
                            "installation_id": target.installation_id,
                            "budget_microusd": budget,
                        },
                    )
        log_event("model_request_reserved", review_run_id=lease.review_run_id,
            review_task_id=lease.task_id, agent=agent, usage_request_id=identifier,
            attempt_count=lease.attempt_count, model_attempt_count=lease.model_attempt_count,
            attempt_kind=request.attempt_kind, purpose=request.purpose,
            batch_number=request.output_source.batch_number if request.output_source else None,
            split_depth=request.output_source.split_depth if request.output_source else None,
            status="reserved")
        return ModelBudgetReservation(
            id=identifier,
            review_plan_id=lease.review_plan_id or "",
            sequence=reserved.request_count,
            reserved_input_tokens=request.input_token_upper_bound,
            reserved_output_tokens=request.output_token_upper_bound,
            reserved_cost_microusd=cost,
            remaining_duration_ms=0,
            capture_output=capture_requested,
        )

    def record_output(self, reservation: ModelBudgetReservation, output: CapturedModelOutput) -> None:
        record = EvaluationModelOutputRecord
        with self.sessions() as session, session.begin():
            row = session.execute(select(record.id, record.review_run_id, record.status, record.expires_at)
                .where(record.id == reservation.id).with_for_update()).one_or_none()
            if row is None:
                raise ValueError("评测请求的预占证据记录不存在")
            now = self.clock()
            expires_at = row.expires_at.replace(tzinfo=UTC) if row.expires_at.tzinfo is None else row.expires_at
            if expires_at <= now or row.status == "expired":
                return
            if output.status == "parse_failed":
                session.execute(update(record).where(record.id == row.id, record.status == "captured")
                    .values(status="parse_failed", error_code=output.error_code))
                return
            if row.status != "pending":
                return
            values: dict[str, object] = {"status": output.status, "output_format": output.format,
                "output_text": output.text, "output_sha256": output.sha256, "byte_size": output.byte_size,
                "error_code": output.error_code, "captured_at": now}
            if output.text is not None:
                accepted = session.scalar(update(ReviewRunRecord).where(
                    ReviewRunRecord.id == row.review_run_id,
                    ReviewRunRecord.evaluation_output_bytes + output.byte_size <= MAX_RUN_OUTPUT_BYTES,
                ).values(evaluation_output_bytes=ReviewRunRecord.evaluation_output_bytes + output.byte_size)
                    .returning(ReviewRunRecord.id))
                if accepted is None:
                    source_exists = session.scalar(select(ReviewRunRecord.id).where(ReviewRunRecord.id == row.review_run_id)) is not None
                    values.update(status="run_limit" if source_exists else "missing", output_text=None, output_sha256=None, byte_size=0,
                                  error_code=None if source_exists else "source_run_removed")
            session.execute(update(record).where(record.id == row.id).values(**values))

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
        cost_reason: UsageCostReason | None = None,
        usage_details: ModelTokenUsage | None = None,
    ) -> None:
        values = (input_tokens, output_tokens, estimated_cost_microusd, duration_ms)
        if any(value is not None and value < 0 for value in values):
            raise ValueError("模型用量不能为负数")
        unknown = estimated_cost_microusd is None
        if cost_reason is None and unknown:
            cost_reason = "usage_missing" if input_tokens is None or output_tokens is None else "legacy_unknown"
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
                    cost_reason=cost_reason,
                    usage_details=usage_details.model_dump(mode="json") if usage_details is not None else None,
                    response_status=response_status,
                    duration_ms=duration_ms,
                    completed_at=now,
                )
                .returning(Request.month_id, Request.reserved_cost_microusd, Request.review_run_id, Request.agent)
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
        log_event("model_request_settled", review_run_id=row.review_run_id,
            agent=row.agent, usage_request_id=reservation.id,
            status="uncertain" if uncertain else "settled",
            response_status=response_status, duration_ms=duration_ms)
