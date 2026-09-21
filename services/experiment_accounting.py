"""独立受控实验的费用上限和调用证据，不写入日常任务账本。"""

import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from domain.evaluation_outputs import (
    MAX_OUTPUT_BYTES,
    MAX_RUN_OUTPUT_BYTES,
    CapturedModelOutput,
)
from domain.security import ErrorCode, SafeApplicationError, SafeError
from services.model_budget import ModelBudgetRequest, ModelBudgetReservation


class ExperimentBudget:
    def __init__(self, directory: Path, limit_microusd: int, max_requests: int):
        if limit_microusd <= 0 or not 1 <= max_requests <= 200:
            raise ValueError("实验必须指定正数费用上限和 1 至 200 次请求上限")
        self.directory, self.limit, self.max_requests = (
            directory,
            limit_microusd,
            max_requests,
        )
        self.lock = Lock()
        self.reserved = 0
        self.requests: dict[str, dict[str, Any]] = {}
        self.output_bytes: dict[str, int] = {}
        self.variant_counts: dict[str, int] = {}
        self._journal(
            {
                "event": "experiment_budget",
                "limit_microusd": limit_microusd,
                "max_requests": max_requests,
            }
        )

    def _journal(self, entry):
        with (self.directory / "requests.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"at": datetime.now(UTC).isoformat(), **entry}, ensure_ascii=False
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

    def accountant(self, run_id: str, agent: str, run_limit: int | None = None):
        return ExperimentAccountant(self, run_id, agent, run_limit)


class ExperimentAccountant:
    def __init__(
        self, budget: ExperimentBudget, run_id: str, agent: str, run_limit: int | None
    ):
        self.budget, self.run_id, self.agent, self.run_limit = (
            budget,
            run_id,
            agent,
            run_limit,
        )

    def reserve(self, request: ModelBudgetRequest) -> ModelBudgetReservation:
        budget = self.budget
        with budget.lock:
            cost = request.cost_upper_bound_microusd
            count = budget.variant_counts.get(self.run_id, 0)
            if (
                cost is None
                or budget.reserved + cost > budget.limit
                or len(budget.requests) >= budget.max_requests
                or (self.run_limit is not None and count >= self.run_limit)
            ):
                raise SafeApplicationError(
                    SafeError(
                        ErrorCode.MODEL_BUDGET_EXCEEDED,
                        "实验价格缺失或已达到费用/请求上限，未发送本次请求",
                        False,
                    )
                )
            identifier = str(uuid4())
            row = {
                "id": identifier,
                "run_id": self.run_id,
                "agent": self.agent,
                "status": "reserved",
                "provider": request.provider,
                "model": request.model,
                "api_protocol": request.api_protocol,
                "attempt_kind": request.attempt_kind,
                "request_sha256": request.request_sha256,
                "source": asdict(request.output_source)
                if request.output_source
                else None,
                "reserved_cost_microusd": cost,
                "estimated_cost_microusd": None,
                "duration_ms": None,
            }
            budget._journal({"event": "reserved", **row})
            budget.requests[identifier] = row
            budget.reserved += cost
            budget.variant_counts[self.run_id] = count + 1
            return ModelBudgetReservation(
                identifier,
                request.output_source.review_plan_id if request.output_source else "",
                len(budget.requests),
                request.input_token_upper_bound,
                request.output_token_upper_bound,
                cost,
                0,
                True,
            )

    def record_output(
        self, reservation: ModelBudgetReservation, output: CapturedModelOutput
    ) -> None:
        budget = self.budget
        with budget.lock:
            used = budget.output_bytes.get(self.run_id, 0)
            row = budget.requests[reservation.id]
            previous = row.get("output_bytes", 0)
            if (
                output.byte_size > MAX_OUTPUT_BYTES
                or used - previous + output.byte_size > MAX_RUN_OUTPUT_BYTES
            ):
                payload = {
                    "status": "run_limit"
                    if output.byte_size <= MAX_OUTPUT_BYTES
                    else "oversized"
                }
            else:
                payload = asdict(output)
            path = budget.directory / (reservation.id + ".output.json")
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            row["output_bytes"] = output.byte_size if "text" in payload else 0
            row["output_status"] = payload["status"]
            budget.output_bytes[self.run_id] = used - previous + row["output_bytes"]
            budget._journal(
                {"event": "output", "id": reservation.id, "status": payload["status"]}
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
        budget = self.budget
        with budget.lock:
            row = budget.requests[reservation.id]
            if row["status"] != "reserved":
                return
            row.update(
                status="uncertain" if uncertain else "settled",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_microusd=estimated_cost_microusd,
                response_status=response_status,
                duration_ms=duration_ms,
            )
            budget._journal({"event": "settled", **row})
            # 费用预占不释放，按每次最坏估算累计；未知、失败和重试仍占实验额度。
