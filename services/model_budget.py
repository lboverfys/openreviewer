"""模型 HTTP 调用预算的进程内作用域与持久化接口。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ModelBudgetRequest:
    provider: str
    api_protocol: str
    model: str
    request_bytes: int
    input_token_upper_bound: int
    output_token_upper_bound: int
    # 没有完整价格表时无法给出可信的最坏费用；持久层会在启用费用上限时
    # 拒绝这种请求，避免把“未知”误当成 0 美元。
    cost_upper_bound_microusd: int | None


@dataclass(frozen=True, slots=True)
class ModelBudgetReservation:
    id: str
    review_plan_id: str
    sequence: int
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_cost_microusd: int
    remaining_duration_ms: int


class ModelBudgetAccountant(Protocol):
    def reserve(self, request: ModelBudgetRequest) -> ModelBudgetReservation: ...

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
    ) -> None: ...


_CURRENT_ACCOUNTANT: ContextVar[ModelBudgetAccountant | None] = ContextVar(
    "openreviewer_model_budget_accountant",
    default=None,
)


def current_model_budget_accountant() -> ModelBudgetAccountant | None:
    return _CURRENT_ACCOUNTANT.get()


@contextmanager
def model_budget_scope(accountant: ModelBudgetAccountant) -> Iterator[None]:
    token: Token[ModelBudgetAccountant | None] = _CURRENT_ACCOUNTANT.set(accountant)
    try:
        yield
    finally:
        _CURRENT_ACCOUNTANT.reset(token)
