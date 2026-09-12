"""模型 HTTP 调用资源统计的进程内作用域与持久化接口。"""

from collections.abc import Callable, Iterator
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
    # 没有完整价格表时无法给出可信的最坏费用；硬限制模式会拒绝这种请求，
    # 观测模式仍会记录请求并把费用标记为未知。
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

_REQUEST_GUARD: ContextVar[Callable[[], None] | None] = ContextVar(
    "openreviewer_repository_request_guard", default=None,
)


def check_model_request_limit() -> None:
    guard = _REQUEST_GUARD.get()
    if guard is not None:
        guard()


@contextmanager
def model_request_scope(guard: Callable[[], None] | None) -> Iterator[None]:
    token = _REQUEST_GUARD.set(guard)
    try:
        yield
    finally:
        _REQUEST_GUARD.reset(token)


def current_model_budget_accountant() -> ModelBudgetAccountant | None:
    return _CURRENT_ACCOUNTANT.get()


@contextmanager
def model_budget_scope(accountant: ModelBudgetAccountant) -> Iterator[None]:
    token: Token[ModelBudgetAccountant | None] = _CURRENT_ACCOUNTANT.set(accountant)
    try:
        yield
    finally:
        _CURRENT_ACCOUNTANT.reset(token)
