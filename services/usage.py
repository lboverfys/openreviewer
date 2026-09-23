"""把当前任务身份传递给请求账本；供应商不依赖 SQLAlchemy。"""

from collections.abc import Callable
from typing import Protocol

from domain.evaluation_outputs import CapturedModelOutput
from domain.model_review import ModelTokenUsage
from domain.platform import UsageCostReason
from services.model_budget import ModelBudgetRequest, ModelBudgetReservation
from services.task_queue import ReviewTaskLease


class UsageLedger(Protocol):
    def record_output(self, reservation: ModelBudgetReservation, output: CapturedModelOutput) -> None: ...

    def reserve(
        self, lease: ReviewTaskLease, agent: str, request: ModelBudgetRequest
    ) -> ModelBudgetReservation: ...

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
    ) -> None: ...


class MonthlyModelAccountant:
    def __init__(
        self, ledger: UsageLedger, lease: Callable[[], ReviewTaskLease], agent: str
    ) -> None:
        self.ledger, self.lease, self.agent = ledger, lease, agent

    def reserve(self, request: ModelBudgetRequest) -> ModelBudgetReservation:
        return self.ledger.reserve(self.lease(), self.agent, request)

    def record_output(self, reservation: ModelBudgetReservation, output: CapturedModelOutput) -> None:
        self.ledger.record_output(reservation, output)

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
        self.ledger.settle(
            reservation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_microusd=estimated_cost_microusd,
            response_status=response_status,
            duration_ms=duration_ms,
            uncertain=uncertain,
            cost_reason=cost_reason,
            usage_details=usage_details,
        )
