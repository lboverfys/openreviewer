"""阿里云向量与精排接口；批量输入、固定公网连接和有界响应。"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit

import httpx

from domain.model_review import ModelTokenUsage
from domain.platform import UsageCostReason
from domain.retrieval import VECTOR_DIMENSIONS, RetrievalSettings
from domain.security import ErrorCode, SafeApplicationError, SafeError
from services.model_budget import ModelBudgetRequest, current_model_budget_accountant
from services.model_review import normalize_api_base_url
from services.pinned_http import PublicDnsPinnedHTTPTransport
from services.telemetry import GLOBAL_TELEMETRY


def external_retrieval_paused(settings: RetrievalSettings | None = None) -> bool:
    if settings is not None and settings.external_calls_enabled is not None:
        return not settings.external_calls_enabled
    return os.environ.get("OPENREVIEWER_RETRIEVAL_API_DISABLED", "").lower() in {
        "1",
        "true",
        "yes",
    }


class RetrievalError(SafeApplicationError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float = 0,
        response_status: int | None = None,
    ) -> None:
        super().__init__(
            SafeError(
                code=ErrorCode.RETRIEVAL_UNAVAILABLE,
                safe_message=message,
                retryable=retryable,
            )
        )
        self.retryable = retryable
        self.retry_after = min(30.0, max(0.0, retry_after))
        self.response_status = response_status


def normalize_aliyun_host(value: str) -> str:
    normalized = normalize_api_base_url(value)
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    host = parsed.hostname or ""
    allowed = host.endswith(".maas.aliyuncs.com") or host in {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
    }
    if not allowed or parsed.port not in {None, 443}:
        raise ValueError("检索模型地址必须是阿里云百炼 HTTPS 接入域名")
    if parsed.path.rstrip("/") not in {
        "",
        "/api/v1",
        "/compatible-mode/v1",
        "/compatible-api/v1",
    }:
        raise ValueError("请填写百炼 API Host 或其标准接入地址")
    return f"https://{host}"


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    duration_ms: int
    input_tokens: int | None


@dataclass(frozen=True, slots=True)
class RerankResult:
    ranking: tuple[tuple[int, float], ...]
    duration_ms: int
    input_tokens: int | None
    cache_hit: bool = False


@dataclass(slots=True)
class RequestBudget:
    limit: int
    used: int = 0
    charge: Callable[[], bool] | None = None
    exhausted: bool = field(default=False, init=False)

    def consume(self) -> None:
        if self.used >= self.limit:
            self.exhausted = True
            raise RetrievalError("本次操作已达到模型请求上限，已停止额外调用")
        if self.charge is not None and not self.charge():
            self.exhausted = True
            raise RetrievalError("本次任务累计已达到模型请求上限，重试不会重置额度")
        self.used += 1


@dataclass(slots=True)
class AliyunRetrievalClient:
    settings: RetrievalSettings
    api_key: str = field(repr=False)
    client: httpx.Client | None = field(default=None, repr=False)
    _owns_client: bool = field(default=False, init=False)
    budget: RequestBudget | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.api_key or any(char.isspace() for char in self.api_key):
            raise RetrievalError("检索模型密钥尚未配置或格式无效")
        origin = normalize_aliyun_host(self.settings.api_host)
        if not origin:
            raise RetrievalError("检索模型地址尚未配置")
        if self.client is None:
            self.client = httpx.Client(
                transport=PublicDnsPinnedHTTPTransport(),
                timeout=httpx.Timeout(self.settings.timeout_seconds, connect=10),
                follow_redirects=False,
                trust_env=False,
            )
            self._owns_client = True

    def close(self) -> None:
        if self._owns_client and self.client is not None:
            self.client.close()

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, Any]:
        from services.egress import check_payload

        check_payload(self.settings.api_host, payload)
        if self._owns_client and external_retrieval_paused(self.settings):
            raise RetrievalError("向量与精排调用已关闭，请在模型配置中开启")
        started = time.monotonic()
        accountant = current_model_budget_accountant()
        request_bytes = len(json.dumps(payload, ensure_ascii=False).encode())
        purpose = "embedding" if "embeddings" in path else "rerank"
        price = (
            self.settings.embedding_usd_per_million
            if purpose == "embedding"
            else self.settings.rerank_usd_per_million
        )
        for attempt in range(3):
            if self.budget is not None:
                self.budget.consume()
            request_started = time.monotonic()
            outcome = "success"
            reservation = (
                accountant.reserve(
                    ModelBudgetRequest(
                        provider="aliyun",
                        api_protocol=purpose,
                        model=str(payload.get("model", "unknown")),
                        request_bytes=request_bytes,
                        input_token_upper_bound=request_bytes,
                        output_token_upper_bound=0,
                        purpose=purpose,
                        connection_key=sha256(
                            (self.settings.api_host + "\0" + self.api_key).encode()
                        ).hexdigest(),
                        timeout_seconds=self.settings.timeout_seconds + 60,
                        cost_upper_bound_microusd=int(
                            (price * request_bytes).to_integral_value(
                                rounding=ROUND_CEILING
                            )
                        )
                        if price is not None
                        else None,
                        pricing_snapshot={"input_usd_per_million": str(price), "output_usd_per_million": "0"} if price is not None else None,
                    )
                )
                if accountant is not None
                else None
            )
            response_body = None
            response_status: int | None = None
            retry_delay = 0.0
            try:
                response_body = self._post_once(path, payload)
                response_status = 200
                return response_body
            except RetrievalError as exc:
                outcome = "client_error"
                response_status = exc.response_status
                if (
                    not exc.retryable
                    or attempt == 2
                    or time.monotonic() - started > 200 - self.settings.timeout_seconds
                ):
                    raise
                retry_delay = max(exc.retry_after, 2**attempt)
            finally:
                if accountant is not None and reservation is not None:
                    tokens = (
                        self._tokens(response_body)
                        if response_body is not None
                        else None
                    )
                    cost_reason: UsageCostReason | None = (
                        "usage_missing" if tokens is None else "pricing_missing" if price is None else None
                    )
                    accountant.settle(
                        reservation,
                        input_tokens=tokens,
                        output_tokens=0 if tokens is not None else None,
                        estimated_cost_microusd=int(
                            (price * Decimal(tokens)).to_integral_value(
                                rounding=ROUND_CEILING
                            )
                        )
                        if price is not None and tokens is not None
                        else None,
                        response_status=response_status,
                        duration_ms=round((time.monotonic() - request_started) * 1000),
                        uncertain=tokens is None,
                        cost_reason=cost_reason,
                        usage_details=ModelTokenUsage(input_tokens=tokens, output_tokens=0) if tokens is not None else None,
                    )
                GLOBAL_TELEMETRY.observe_external(
                    "retrieval_embedding"
                    if "embeddings" in path
                    else "retrieval_rerank",
                    time.monotonic() - request_started,
                    outcome=outcome,
                )
            # 当前请求先结算并释放通道，再等待下一次尝试；退避不占用并发名额。
            if retry_delay:
                time.sleep(retry_delay)
        raise AssertionError("unreachable retry state")

    def _post_once(self, path: str, payload: dict[str, object]) -> dict[str, Any]:
        assert self.client is not None
        try:
            with self.client.stream(
                "POST",
                normalize_aliyun_host(self.settings.api_host) + path,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            ) as response:
                if response.status_code != 200:
                    retry_header = response.headers.get("retry-after", "")
                    retry_after = (
                        float(retry_header)
                        if retry_header.isascii() and retry_header.isdigit()
                        else 0
                    )
                    error_bytes = bytearray()
                    for part in response.iter_bytes():
                        error_bytes.extend(part)
                        if len(error_bytes) > 16_384:
                            break
                    try:
                        error_body = json.loads(error_bytes)
                    except ValueError:
                        error_body = {}
                    error = (
                        error_body.get("error", error_body)
                        if isinstance(error_body, dict)
                        else {}
                    )
                    code = str(error.get("code", "")) if isinstance(error, dict) else ""
                    code = (
                        code
                        if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code)
                        and not code.startswith("sk-")
                        else ""
                    )
                    description = f"百炼检索接口返回 HTTP {response.status_code}" + (
                        f"（{code}）" if code else ""
                    )
                    raise RetrievalError(
                        description,
                        retryable=response.status_code == 429
                        or response.status_code >= 500
                        or code.startswith("Throttling"),
                        retry_after=retry_after,
                        response_status=response.status_code,
                    )
                data = bytearray()
                for part in response.iter_bytes():
                    data.extend(part)
                    if len(data) > 4 * 1024 * 1024:
                        raise RetrievalError("百炼检索响应超过允许大小")
                body = json.loads(data)
        except httpx.HTTPError as exc:
            raise RetrievalError("百炼检索连接失败或超时", retryable=True) from exc
        except ValueError as exc:
            raise RetrievalError("百炼检索返回了无效 JSON") from exc
        if not isinstance(body, dict):
            raise RetrievalError("百炼检索返回结构无效")
        return body

    @staticmethod
    def _tokens(body: dict[str, Any]) -> int | None:
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return None
        value = usage.get(
            "input_tokens", usage.get("prompt_tokens", usage.get("total_tokens"))
        )
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else None
        )

    def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
        # The indexer packs bounded batches before calling this method.
        if not 1 <= len(texts) <= 20 or any(not value.strip() for value in texts):
            raise ValueError("向量请求需要 1 到 20 段非空文本")
        if sum(len(value.encode()) for value in texts) > self.settings.embedding_batch_max_bytes:
            raise ValueError(f"单批向量输入超过配置上限 {self.settings.embedding_batch_max_bytes / 1000:g} KB")
        started = time.monotonic()
        body = self._post(
            "/compatible-mode/v1/embeddings",
            {
                "model": self.settings.embedding_model,
                "input": list(texts),
                "dimensions": VECTOR_DIMENSIONS,
            },
        )
        rows = body.get("data")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise RetrievalError("向量响应条数与请求不一致")
        vectors: dict[int, tuple[float, ...]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise RetrievalError("向量响应条目无效")
            index, vector = row.get("index"), row.get("embedding")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < len(texts)
                or index in vectors
            ):
                raise RetrievalError("向量响应索引无效或重复")
            if not isinstance(vector, list) or len(vector) != VECTOR_DIMENSIONS:
                raise RetrievalError("向量维度与索引配置不一致")
            if not all(
                isinstance(v, (int, float))
                and not isinstance(v, bool)
                and math.isfinite(v)
                for v in vector
            ):
                raise RetrievalError("向量响应包含无效数值")
            if not any(vector):
                raise RetrievalError("向量响应为零向量")
            vectors[index] = tuple(float(v) for v in vector)
        return EmbeddingResult(
            tuple(vectors[i] for i in range(len(texts))),
            round((time.monotonic() - started) * 1000),
            self._tokens(body),
        )

    def rerank(self, query: str, documents: tuple[str, ...]) -> RerankResult:
        if not query.strip() or not 1 <= len(documents) <= 30:
            raise ValueError("精排请求需要查询及 1 到 30 个候选")
        if sum(len(value.encode()) for value in (query, *documents)) > 180_000:
            raise ValueError("精排请求超过允许大小")
        started = time.monotonic()
        if self.settings.rerank_model == "qwen3-rerank":
            path = "/compatible-api/v1/reranks"
            payload: dict[str, object] = {
                "model": self.settings.rerank_model,
                "query": query,
                "documents": list(documents),
                "top_n": len(documents),
            }
        else:
            path = "/api/v1/services/rerank/text-rerank/text-rerank"
            payload = {
                "model": self.settings.rerank_model,
                "input": {"query": query, "documents": list(documents)},
                "parameters": {
                    "top_n": len(documents),
                    "instruct": "Retrieve code and rules that are relevant evidence for reviewing the described code change.",
                },
            }
        body = self._post(path, payload)
        output = body.get("output", body)
        rows = output.get("results") if isinstance(output, dict) else None
        if not isinstance(rows, list) or len(rows) != len(documents):
            raise RetrievalError("精排响应条数与候选不一致")
        ranking: list[tuple[int, float]] = []
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise RetrievalError("精排响应条目无效")
            index, score = row.get("index"), row.get("relevance_score")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < len(documents)
                or index in seen
            ):
                raise RetrievalError("精排响应索引无效或重复")
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not math.isfinite(score)
            ):
                raise RetrievalError("精排响应分数无效")
            seen.add(index)
            ranking.append((index, float(score)))
        ranking.sort(key=lambda item: (-item[1], item[0]))
        return RerankResult(
            tuple(ranking),
            round((time.monotonic() - started) * 1000),
            self._tokens(body),
        )
