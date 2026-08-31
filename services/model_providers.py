"""OpenAI 与 Anthropic 官方 API 的严格结构化输出适配器。"""

import json
import re
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from domain.enums import (
    ModelApiProtocol,
    ModelCallStatus,
    ModelProvider,
    ReviewAgent,
)
from domain.model_review import (
    ModelReviewInput,
    ModelReviewOutput,
    ModelReviewResult,
    ModelTokenUsage,
    model_review_output_schema,
)
from domain.security import ErrorCode, SafeApplicationError, SafeError
from services.model_budget import (
    ModelBudgetRequest,
    ModelBudgetReservation,
    current_model_budget_accountant,
)
from services.model_review import (
    ModelReviewer,
    ModelServiceSettings,
    ReviewPrompt,
    StructuredReviewPromptBuilder,
    validate_model_api_endpoint,
)
from services.pinned_http import PublicDnsPinnedHTTPTransport
from services.telemetry import GLOBAL_TELEMETRY, TelemetryRegistry
from services.token_estimation import estimate_model_request_tokens

_COMPATIBILITY_ERROR_BODY_LIMIT = 16 * 1024
_UNSUPPORTED_MARKERS = (
    "unsupported",
    "not supported",
    "does not support",
    "unknown parameter",
    "unknown field",
    "unrecognized parameter",
    "unrecognized field",
    "unexpected field",
    "extra inputs are not permitted",
    "invalid request argument",
    # Several relays use this wording for a field they do not implement.  A
    # known optional parameter must still be present before we downgrade.
    "invalid parameter",
    "invalid field",
    "invalid argument",
)
# Some OpenAI-compatible relays describe an unsupported option as an invalid
# value and list the values they do support, without using the word
# ``unsupported``.  These markers are intentionally kept separate from the
# broad HTTP error classifier so ordinary validation failures are not retried.
_INVALID_SUPPORTED_VALUE_MARKERS = (
    "invalid value",
    "invalid parameter",
    "invalid field",
    "invalid argument",
    "invalid option",
    "value is not valid",
    "无效值",
    "参数值无效",
    "字段值无效",
    "值无效",
)
_SUPPORTED_VALUE_MARKERS = (
    "supported value",
    "supported values",
    "supported option",
    "supported options",
    "supported types",
    "allowed value",
    "allowed values",
    "expected one of",
    "one of the following",
    "must be one of",
    "支持的值",
    "可用值",
    "允许的值",
    "有效值",
)
_COMPATIBILITY_PARAMETER_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("max_completion_tokens", ("max_completion_tokens",)),
    ("reasoning_effort", ("reasoning_effort",)),
    ("response_format", ("response_format",)),
    ("json_schema", ("json_schema",)),
    ("output_config", ("output_config",)),
    ("reasoning", ("reasoning",)),
    ("effort", ("effort",)),
    ("store", ("store",)),
    ("stream", ("stream",)),
)
_REQUIRED_OUTPUT_FIELDS = ("verdict", "summary", "checked_areas", "findings")
_MAX_VALIDATION_ISSUES = 8
_TRUNCATION_RETRY_MAX_OUTPUT_TOKENS = 8_192
_FORMAT_REPAIR_INSTRUCTION = """上一条回答没有通过结构化审查契约校验。请重新完成同一审查，只返回一个 JSON 对象，不要使用 Markdown 代码块或附加说明。
顶层必须且只能包含 verdict、summary、checked_areas、findings。verdict 只能是 issues_found、no_actionable_issue、insufficient_context；summary 必须是简体中文非空字符串；checked_areas 必须是去重后的字符串数组。发现问题时 verdict=issues_found 且 findings 非空；没有可靠问题时 verdict=no_actionable_issue 且 findings=[]；上下文不足时使用 insufficient_context。每个 finding 必须严格符合原请求 output_contract。"""
_TRUNCATION_RETRY_INSTRUCTION = """上一条回答没有完整结束，可能触及了输出上限。请在同一审查范围内重新回答，并严格控制输出长度：只保留最多 8 条最高价值、由补丁直接支持的问题；summary 不超过 300 字；checked_areas 不超过 8 项；每个 finding 的 title 不超过 120 字，evidence、impact、suggestion 和 required_test 各不超过 500 字。不要输出思维链、Markdown 或任何额外字段；即使没有可靠问题也必须返回完整且合法的 JSON 对象。"""


@dataclass(frozen=True, slots=True)
class ModelHttpAudit:
    response_status: int | None
    provider_request_id: str | None
    duration_ms: int
    budget_reservation: ModelBudgetReservation | None = None


class _StructuredModelReviewer(ModelReviewer):
    """三个官方协议共享的有界 HTTP、错误分类和结果组装逻辑。"""

    provider: ModelProvider
    api_protocol: ModelApiProtocol
    request_path: str

    def __init__(
        self,
        settings: ModelServiceSettings,
        *,
        client: httpx.Client | None = None,
        monotonic: Callable[[], float] | None = None,
        prompt_builder: StructuredReviewPromptBuilder | None = None,
        telemetry: TelemetryRegistry | None = None,
    ) -> None:
        if settings.provider is not self.provider:
            raise ValueError("model settings provider does not match the adapter")
        if settings.resolved_api_protocol is not self.api_protocol:
            raise ValueError("model settings API protocol does not match the adapter")
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=settings.resolved_api_base_url,
            timeout=settings.timeout,
            follow_redirects=False,
            trust_env=False,
            transport=PublicDnsPinnedHTTPTransport(),
        )
        self._monotonic = monotonic or time.monotonic
        self._prompt_builder = prompt_builder or StructuredReviewPromptBuilder()
        self._telemetry = telemetry or GLOBAL_TELEMETRY

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def review(self, review_input: ModelReviewInput) -> ModelReviewResult:
        # 汇总节点只需要前三路的结构化候选；即使调用方忘记在编排层
        # 清空计划，也不能把完整 rules/patch 再发送给中转站。
        prompt_input = _summary_prompt_input(review_input)
        prompt = self._prompt_builder.build(
            prompt_input,
            self.provider,
            self._settings.model,
            self.api_protocol,
        )
        # 普通空审查没有任何可供模型检查的内容，应保持 SKIPPED；汇总
        # Agent 则通过有界 prior_agent_results 工作，即使 units 已被编排器
        # 清空，也必须真正调用模型生成全局结论。
        summary_context = (
            review_input.review_agent is ReviewAgent.SUMMARY
            and bool(review_input.prior_agent_results)
        )
        if not review_input.units and not summary_context:
            return ModelReviewResult(
                provider=self.provider,
                api_protocol=self.api_protocol,
                model=self._settings.model,
                status=ModelCallStatus.SKIPPED,
                prompt_version=prompt.version,
                request_fingerprint=prompt.request_fingerprint,
                duration_ms=0,
                usage=ModelTokenUsage(input_tokens=0, output_tokens=0),
                estimated_cost_microusd=0,
                output=ModelReviewOutput(findings=()),
            )

        # 使用完整 URL，避免 httpx 对 ``https://relay/v1`` 这类无末尾斜杠
        # Base URL 做相对路径合并时丢掉中转站路径前缀。
        request_path = self._settings.api_request_url(self.request_path)
        request_body = self._request_body(prompt)
        settled_reservations: set[str] = set()

        def settle_budget_once(
            audit: ModelHttpAudit | None,
            usage: ModelTokenUsage | None,
            *,
            uncertain: bool = False,
        ) -> None:
            """在一次 review 调用内至多结算每个 reservation 一次。"""

            if audit is None or audit.budget_reservation is None:
                return
            reservation_id = audit.budget_reservation.id
            if reservation_id in settled_reservations:
                return
            self._settle_budget_audit(audit, usage, uncertain=uncertain)
            settled_reservations.add(reservation_id)

        attempted_bodies: set[str] = set()
        while True:
            # 以稳定 JSON 签名限制兼容重试次数；即使中转站反复返回同一
            # ``unsupported parameter`` 错误，也不会形成无限请求循环。
            body_signature = json.dumps(
                request_body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if body_signature in attempted_bodies:
                raise self._error(
                    ErrorCode.MODEL_REQUEST_REJECTED,
                    "模型中转站不支持当前请求参数",
                    retryable=False,
                )
            attempted_bodies.add(body_signature)
            try:
                payload, audit = self._post_json(
                    request_path,
                    headers=self._request_headers(),
                    body=request_body,
                )
                break
            except SafeApplicationError as exc:
                fallback = self._compatibility_fallback_body(
                    request_body,
                    exc.error,
                )
                if fallback is None:
                    raise
                request_body = fallback
        try:
            try:
                output, usage, response_id = self._parse_success(payload)
            except SafeApplicationError as exc:
                # 解析失败发生在 HTTP 返回之后；把响应审计信息合并到安全错误，
                # 这样批次事件能区分“模型截断”与“代理超时”，而不会保存响应正文。
                parse_error = self._with_audit_details(exc, audit)
                first_usage = _known_failed_usage(parse_error.error.details)
                settle_budget_once(
                    audit,
                    first_usage,
                    uncertain=first_usage is None,
                )
                if (
                    parse_error.error.code is ErrorCode.MODEL_OUTPUT_TRUNCATED
                    and self._should_retry_truncated_output(parse_error.error)
                ):
                    compact_body = self._truncation_retry_body(request_body)
                    if compact_body is not None:
                        (
                            output,
                            usage,
                            response_id,
                            audit,
                        ) = self._retry_truncated_output(
                            request_path,
                            compact_body,
                            parse_error.error,
                            audit,
                            settle_budget_once=settle_budget_once,
                        )
                    else:
                        raise parse_error from exc
                elif parse_error.error.details.get("contract_validation") is not True:
                    raise parse_error from exc
                else:
                    first_error = parse_error.error
                    first_audit = audit
                    repair_body = self._contract_repair_body(request_body, first_error)
                    repair_audit: ModelHttpAudit | None = None
                    try:
                        repair_payload, repair_audit = self._post_json(
                            request_path,
                            headers=self._request_headers(),
                            body=repair_body,
                        )
                        try:
                            output, repair_usage, response_id = self._parse_success(
                                repair_payload
                            )
                        except SafeApplicationError as repair_parse_exc:
                            repair_parse_error = self._with_audit_details(
                                repair_parse_exc,
                                repair_audit,
                            )
                            repair_failed_usage = _known_failed_usage(
                                repair_parse_error.error.details
                            )
                            settle_budget_once(
                                repair_audit,
                                repair_failed_usage,
                                uncertain=repair_failed_usage is None,
                            )
                            raise repair_parse_error from repair_parse_exc
                        except Exception as repair_parse_exc:
                            # 用量、响应身份或兼容适配器字段损坏时，解析器
                            # 可能抛出原生异常；必须结算这一次重试的预留，
                            # 不能让外层只结算首次请求而遗留 reserved 记录。
                            settle_budget_once(
                                repair_audit,
                                None,
                                uncertain=True,
                            )
                            raise self._error(
                                ErrorCode.MODEL_INVALID_RESPONSE,
                                "模型 API 用量或响应身份字段无效",
                                retryable=False,
                                details=self._audit_details(repair_audit),
                            ) from repair_parse_exc
                        settle_budget_once(repair_audit, repair_usage)
                    except SafeApplicationError as repair_exc:
                        raise self._format_repair_failure(
                            first_error,
                            first_audit,
                            repair_exc.error,
                            repair_audit,
                        ) from repair_exc
                    usage = _combine_usage(
                        _failed_usage(first_error.details),
                        repair_usage,
                    )
                    if repair_audit is None:
                        raise self._error(
                            ErrorCode.MODEL_INVALID_RESPONSE,
                            "模型格式纠正缺少 HTTP 审计信息",
                            retryable=False,
                        ) from None
                    audit = ModelHttpAudit(
                        response_status=repair_audit.response_status,
                        provider_request_id=_join_request_ids(
                            first_audit.provider_request_id,
                            repair_audit.provider_request_id,
                        ),
                        duration_ms=first_audit.duration_ms + repair_audit.duration_ms,
                    )
            else:
                settle_budget_once(audit, usage)
        except SafeApplicationError:
            raise
        except Exception as exc:
            settle_budget_once(audit, None, uncertain=True)
            raise self._error(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "模型 API 用量或响应身份字段无效",
                retryable=False,
                details=self._audit_details(audit),
            ) from exc
        estimated_cost = (
            self._settings.pricing.estimate_microusd(usage)
            if self._settings.pricing is not None
            else None
        )
        return ModelReviewResult(
            provider=self.provider,
            api_protocol=self.api_protocol,
            model=self._settings.model,
            status=ModelCallStatus.SUCCEEDED,
            prompt_version=prompt.version,
            request_fingerprint=prompt.request_fingerprint,
            provider_response_id=response_id,
            provider_request_id=audit.provider_request_id,
            response_status=audit.response_status,
            duration_ms=audit.duration_ms,
            usage=usage,
            estimated_cost_microusd=estimated_cost,
            output=output,
        )

    def _contract_repair_body(
        self,
        body: dict[str, object],
        error: SafeError,
    ) -> dict[str, object]:
        """基于原始有界请求追加一次格式纠正指令。"""

        candidate = deepcopy(body)
        issue_paths = _validation_issue_paths(error.details)
        instruction = _FORMAT_REPAIR_INSTRUCTION
        if issue_paths:
            instruction += f"\n本次未通过的字段：{', '.join(issue_paths)}。"
        if self.api_protocol is ModelApiProtocol.RESPONSES:
            messages = candidate.get("input")
            if isinstance(messages, list):
                messages.append(
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": instruction}],
                    }
                )
                return candidate
        else:
            messages = candidate.get("messages")
            if isinstance(messages, list):
                messages.append({"role": "user", "content": instruction})
                return candidate
        raise self._error(
            ErrorCode.MODEL_REVIEW_INPUT_INVALID,
            "模型格式纠正请求无法构造",
            retryable=False,
        )

    def _with_audit_details(
        self,
        error: SafeApplicationError,
        audit: ModelHttpAudit,
    ) -> SafeApplicationError:
        """给解析错误补上 HTTP 审计字段，但不保存供应商响应正文。"""

        return SafeApplicationError(
            SafeError(
                code=error.error.code,
                safe_message=error.error.safe_message,
                retryable=error.error.retryable,
                details={
                    **dict(error.error.details),
                    **self._audit_details(audit),
                },
            )
        )

    def _truncation_retry_body(
        self,
        body: dict[str, object],
    ) -> dict[str, object] | None:
        """为截断响应追加一次有界的“精简输出”指令。"""

        candidate = deepcopy(body)
        # 截断通常意味着模型把大量 Token 花在冗长说明或推理上。精简重试
        # 使用更小的显式上限，并关闭可选推理字段，让有限额度留给完整 JSON。
        for name in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
            value = candidate.get(name)
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value > _TRUNCATION_RETRY_MAX_OUTPUT_TOKENS
            ):
                candidate[name] = _TRUNCATION_RETRY_MAX_OUTPUT_TOKENS
        if self.api_protocol is ModelApiProtocol.RESPONSES:
            candidate.pop("reasoning", None)
        elif self.api_protocol is ModelApiProtocol.CHAT_COMPLETIONS:
            candidate.pop("reasoning_effort", None)
        else:
            output_config = candidate.get("output_config")
            if isinstance(output_config, dict):
                output_config.pop("effort", None)
        if self.api_protocol is ModelApiProtocol.RESPONSES:
            messages = candidate.get("input")
            if isinstance(messages, list):
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": _TRUNCATION_RETRY_INSTRUCTION}
                        ],
                    }
                )
                return candidate
        else:
            messages = candidate.get("messages")
            if isinstance(messages, list):
                messages.append(
                    {"role": "user", "content": _TRUNCATION_RETRY_INSTRUCTION}
                )
                return candidate
        return None

    def _should_retry_truncated_output(self, error: SafeError) -> bool:
        """只对能通过精简输出改善的明确截断原因重试一次。"""

        details = error.details
        if self.api_protocol is ModelApiProtocol.RESPONSES:
            # Responses 的 incomplete 还可能表示内容过滤或未来新增状态。
            # 未知原因不能假定为长度不足，否则会无谓消耗第二次预算。
            reason = details.get("incomplete_reason")
            return isinstance(reason, str) and reason in {
                "max_output_tokens",
                "length",
            }
        if self.api_protocol is ModelApiProtocol.CHAT_COMPLETIONS:
            return details.get("finish_reason") == "length"
        return details.get("stop_reason") == "max_tokens"

    def _retry_truncated_output(
        self,
        path: str,
        body: dict[str, object],
        first_error: SafeError,
        first_audit: ModelHttpAudit,
        *,
        settle_budget_once: Callable[..., None],
    ) -> tuple[ModelReviewOutput, ModelTokenUsage, str | None, ModelHttpAudit]:
        """截断后只再请求一次精简结果，成功才恢复为正常审查结果。"""

        retry_audit: ModelHttpAudit | None = None
        try:
            retry_payload, retry_audit = self._post_json(
                path,
                headers=self._request_headers(),
                body=body,
            )
            try:
                output, retry_usage, response_id = self._parse_success(retry_payload)
            except SafeApplicationError as parse_exc:
                parse_error = self._with_audit_details(parse_exc, retry_audit)
                failed_usage = _known_failed_usage(parse_error.error.details)
                settle_budget_once(
                    retry_audit,
                    failed_usage,
                    uncertain=failed_usage is None,
                )
                raise parse_error from parse_exc
            except Exception as parse_exc:
                # 与格式纠正路径相同，任意原生解析异常也必须结算当前
                # 重试的 reservation；否则数据库会留下永远 reserved 的调用。
                settle_budget_once(
                    retry_audit,
                    None,
                    uncertain=True,
                )
                raise self._error(
                    ErrorCode.MODEL_INVALID_RESPONSE,
                    "模型 API 用量或响应身份字段无效",
                    retryable=False,
                    details=self._audit_details(retry_audit),
                ) from parse_exc
            settle_budget_once(retry_audit, retry_usage)
        except SafeApplicationError as retry_exc:
            raise self._format_truncation_failure(
                first_error,
                first_audit,
                retry_exc.error,
                retry_audit,
            ) from retry_exc

        if retry_audit is None:
            # _post_json either returns an audit or raises; this guard keeps the
            # invariant explicit for type checkers and future adapters.
            raise self._error(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "模型精简重试缺少 HTTP 审计信息",
                retryable=False,
            )
        first_usage = _known_failed_usage(first_error.details)
        combined_usage = _combine_usage(
            first_usage or ModelTokenUsage(input_tokens=0, output_tokens=0),
            retry_usage,
        )
        return (
            output,
            combined_usage,
            response_id,
            ModelHttpAudit(
                response_status=retry_audit.response_status,
                provider_request_id=_join_request_ids(
                    first_audit.provider_request_id,
                    retry_audit.provider_request_id,
                ),
                duration_ms=first_audit.duration_ms + retry_audit.duration_ms,
            ),
        )

    def _format_truncation_failure(
        self,
        first_error: SafeError,
        first_audit: ModelHttpAudit,
        retry_error: SafeError,
        retry_audit: ModelHttpAudit | None,
    ) -> SafeApplicationError:
        """合并首次截断与精简重试失败的安全诊断。"""

        retry_request_id = (
            retry_audit.provider_request_id
            if retry_audit is not None
            else retry_error.details.get("provider_request_id")
        )
        retry_duration = (
            retry_audit.duration_ms
            if retry_audit is not None
            else retry_error.details.get("duration_ms")
        )
        first_usage = _known_failed_usage(first_error.details)
        retry_usage = _known_failed_usage(retry_error.details)
        combined_usage = _combine_usage(
            first_usage or ModelTokenUsage(input_tokens=0, output_tokens=0),
            retry_usage or ModelTokenUsage(input_tokens=0, output_tokens=0),
        )
        details = {
            **dict(retry_error.details),
            **_failed_usage_details(combined_usage),
            "compact_retry_attempted": True,
            "initial_error_code": first_error.code.value,
            "initial_provider_request_id": first_audit.provider_request_id,
            "initial_status_code": first_audit.response_status,
            "initial_duration_ms": first_audit.duration_ms,
            **{
                f"initial_{key}": first_error.details[key]
                for key in (
                    "incomplete_reason",
                    "finish_reason",
                    "stop_reason",
                )
                if key in first_error.details
            },
            "provider_request_id": _join_request_ids(
                first_audit.provider_request_id,
                retry_request_id if isinstance(retry_request_id, str) else None,
            ),
            "status_code": (
                retry_audit.response_status
                if retry_audit is not None
                else retry_error.details.get("status_code")
            ),
            "duration_ms": first_audit.duration_ms
            + (
                retry_duration
                if isinstance(retry_duration, int)
                and not isinstance(retry_duration, bool)
                and retry_duration >= 0
                else 0
            ),
        }
        message = retry_error.safe_message
        if retry_error.code is ErrorCode.MODEL_OUTPUT_TRUNCATED:
            message = f"{message}（精简输出重试后仍未完整结束）"
        else:
            message = f"{message}（截断响应后的精简输出重试失败）"
        return SafeApplicationError(
            SafeError(
                code=retry_error.code,
                safe_message=message,
                retryable=retry_error.retryable,
                details=details,
            )
        )

    def _format_repair_failure(
        self,
        first_error: SafeError,
        first_audit: ModelHttpAudit,
        repair_error: SafeError,
        repair_audit: ModelHttpAudit | None,
    ) -> SafeApplicationError:
        """合并两次请求的安全诊断，不保存模型原文。"""

        combined_usage = _combine_usage(
            _failed_usage(first_error.details),
            _failed_usage(repair_error.details),
        )
        repair_request_id = (
            repair_audit.provider_request_id
            if repair_audit is not None
            else repair_error.details.get("provider_request_id")
        )
        request_id = _join_request_ids(
            first_audit.provider_request_id,
            repair_request_id if isinstance(repair_request_id, str) else None,
        )
        repair_duration = (
            repair_audit.duration_ms
            if repair_audit is not None
            else repair_error.details.get("duration_ms")
        )
        details = {
            **dict(repair_error.details),
            **_failed_usage_details(combined_usage),
            "format_repair_attempted": True,
            "initial_validation_issues": first_error.details.get(
                "validation_issues",
                [],
            ),
            "initial_provider_request_id": first_audit.provider_request_id,
            "provider_request_id": request_id,
            "status_code": (
                repair_audit.response_status
                if repair_audit is not None
                else repair_error.details.get("status_code")
            ),
            "duration_ms": first_audit.duration_ms
            + (
                repair_duration
                if isinstance(repair_duration, int)
                and not isinstance(repair_duration, bool)
                and repair_duration >= 0
                else 0
            ),
        }
        message = repair_error.safe_message
        if repair_error.details.get("contract_validation") is True:
            message = f"{message}（自动纠正一次后仍不合格）"
        return SafeApplicationError(
            SafeError(
                code=repair_error.code,
                safe_message=message,
                retryable=repair_error.retryable,
                details=details,
            )
        )

    def _compatibility_fallback_body(
        self,
        body: dict[str, object],
        error: SafeError,
    ) -> dict[str, object] | None:
        """仅针对明确的可选参数不兼容错误生成下一版请求体。

        这里不根据任意 4xx 自动重试。``_classify_response`` 只会在响应状态为
        400/422 且错误提示同时包含“不支持”和已知可选字段时写入
        ``unsupported_parameters``；因此普通业务校验失败、鉴权失败和限流都
        不会触发降级。
        """

        raw = error.details.get("unsupported_parameters")
        if not isinstance(raw, (list, tuple, set, frozenset)):
            return None
        parameters = {
            item for item in raw if isinstance(item, str) and item
        }
        if not parameters:
            return None

        candidate = deepcopy(body)
        changed = False

        if "reasoning_effort" in parameters:
            if "reasoning_effort" in candidate:
                candidate.pop("reasoning_effort", None)
                changed = True
            if self.api_protocol is ModelApiProtocol.RESPONSES:
                reasoning = candidate.get("reasoning")
                if isinstance(reasoning, dict) and "effort" in reasoning:
                    candidate.pop("reasoning", None)
                    changed = True
            if self.api_protocol is ModelApiProtocol.MESSAGES:
                output_config = candidate.get("output_config")
                if isinstance(output_config, dict) and "effort" in output_config:
                    output_config.pop("effort", None)
                    changed = True

        if "reasoning" in parameters and "reasoning" in candidate:
            candidate.pop("reasoning", None)
            changed = True

        if "effort" in parameters:
            output_config = candidate.get("output_config")
            if isinstance(output_config, dict) and "effort" in output_config:
                output_config.pop("effort", None)
                changed = True

        if "max_completion_tokens" in parameters and "max_completion_tokens" in candidate:
            # 旧版 Chat Completions 中转站通常仍接受 max_tokens；只在服务端
            # 明确指出新字段不支持时转换，避免改变正常请求语义。
            value = candidate.pop("max_completion_tokens")
            if "max_tokens" not in candidate:
                candidate["max_tokens"] = value
            changed = True

        if "response_format" in parameters or "json_schema" in parameters:
            if self.api_protocol is ModelApiProtocol.CHAT_COMPLETIONS:
                response_format = candidate.get("response_format")
                if isinstance(response_format, dict):
                    candidate["response_format"] = {"type": "json_object"}
                    changed = response_format != candidate["response_format"]
            elif self.api_protocol is ModelApiProtocol.RESPONSES:
                text_config = candidate.get("text")
                if isinstance(text_config, dict):
                    candidate["text"] = {
                        "format": {"type": "json_object"}
                    }
                    changed = text_config != candidate["text"]
            elif self.api_protocol is ModelApiProtocol.MESSAGES:
                # Anthropic 旧兼容端点没有统一的 JSON Object 格式字段；移除
                # 可选结构化配置后仍由本地 Pydantic 严格校验模型文本。
                if "output_config" in candidate:
                    candidate.pop("output_config", None)
                    changed = True

        if "output_config" in parameters and "output_config" in candidate:
            candidate.pop("output_config", None)
            changed = True

        if "store" in parameters and "store" in candidate:
            candidate.pop("store", None)
            changed = True

        if "stream" in parameters and candidate.get("stream") is True:
            # 少数兼容中转站只实现同步 JSON。仅在服务端明确指出 stream
            # 不支持时回退，正常情况下始终保留 SSE，避免再次触发网关等待超时。
            candidate.pop("stream", None)
            changed = True

        return candidate if changed else None

    def _request_headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _request_body(self, prompt: ReviewPrompt) -> dict[str, object]:
        raise NotImplementedError

    def _parse_success(
        self,
        payload: dict[str, object],
    ) -> tuple[ModelReviewOutput, ModelTokenUsage, str | None]:
        raise NotImplementedError

    def _post_json(
        self,
        path: str,
        *,
        headers: dict[str, str],
        body: dict[str, object],
    ) -> tuple[dict[str, object], ModelHttpAudit]:
        try:
            request_content = json.dumps(
                body,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise self._error(
                ErrorCode.MODEL_REVIEW_INPUT_INVALID,
                "模型请求无法序列化",
                retryable=False,
            ) from exc
        if len(request_content) > self._settings.max_request_bytes:
            raise self._error(
                ErrorCode.MODEL_REVIEW_INPUT_INVALID,
                "模型请求超过允许大小",
                retryable=False,
                details={"request_bytes": len(request_content)},
            )

        if self._owns_client:
            try:
                validate_model_api_endpoint(self._settings.resolved_api_base_url)
            except ValueError as exc:
                raise self._error(
                    ErrorCode.MODEL_REQUEST_REJECTED,
                    "模型 API 地址不安全或无法解析",
                    retryable=False,
                ) from exc

        reservation = self._reserve_budget(request_content, body)
        started = self._monotonic()
        audit: ModelHttpAudit | None = None
        budget_settled = False
        request_headers = dict(headers)
        if body.get("stream") is True:
            request_headers.setdefault("Accept", "text/event-stream")
        try:
            with self._client.stream(
                "POST",
                path,
                headers=request_headers,
                content=request_content,
                timeout=self._budget_timeout(reservation),
            ) as response:
                audit = self._audit(response, started, reservation)
                if not 200 <= response.status_code < 300:
                    self._observe_external(
                        audit.duration_ms / 1000,
                        status_code=response.status_code,
                    )
                    uncertain = (
                        response.status_code in {408, 409, 429}
                        or response.status_code >= 500
                    )
                    settlement_usage = (
                        None
                        if uncertain
                        else ModelTokenUsage(input_tokens=0, output_tokens=0)
                    )
                    try:
                        self._settle_budget_audit(
                            audit,
                            settlement_usage,
                            uncertain=uncertain,
                        )
                    except Exception as settle_error:
                        # ``settle_model_budget`` 可能在提交后抛出预算超限错误；
                        # 这种错误代表 reservation 已经终态化，不能再尝试一次。
                        # 其他队列/数据库错误的提交结果可能未知：补偿一次带
                        # ``uncertain`` 的幂等结算，成功后仍应返回真正的 HTTP
                        # 错误，而不是把临时队列异常误报成模型失败。
                        if (
                            isinstance(settle_error, SafeApplicationError)
                            and settle_error.error.code
                            is ErrorCode.MODEL_BUDGET_EXCEEDED
                        ):
                            budget_settled = True
                            raise
                        try:
                            self._settle_budget_audit(
                                audit,
                                None,
                                uncertain=True,
                            )
                        except Exception as compensation_error:
                            # 两次结算都失败时禁止外层异常分支再发起第三次
                            # 调用；队列层应通过其自身恢复/幂等机制处理该
                            # reservation，当前请求则保留补偿错误作为根因。
                            budget_settled = True
                            raise compensation_error from settle_error
                        budget_settled = True
                    else:
                        budget_settled = True
                    raise SafeApplicationError(
                        self._classify_response(response, audit)
                    )
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > self._settings.max_response_bytes:
                        self._observe_external(
                            audit.duration_ms / 1000,
                            outcome="invalid_response",
                        )
                        raise self._error(
                            ErrorCode.MODEL_RESPONSE_TOO_LARGE,
                            "模型 API 响应超过允许大小",
                            retryable=False,
                            details=self._audit_details(audit),
                        )
                    content.extend(chunk)
        except SafeApplicationError:
            if audit is not None and not budget_settled:
                self._settle_budget_audit(audit, None, uncertain=True)
            raise
        except httpx.TimeoutException as exc:
            audit = ModelHttpAudit(
                # A response may have already yielded headers before the
                # body stream times out.  Preserve that audit context instead
                # of turning a useful 200/request-id into an anonymous error.
                response_status=(audit.response_status if audit is not None else None),
                provider_request_id=(
                    audit.provider_request_id if audit is not None else None
                ),
                duration_ms=max(0, int((self._monotonic() - started) * 1000)),
                budget_reservation=reservation,
            )
            if not budget_settled:
                self._settle_budget_audit(audit, None, uncertain=True)
            self._observe_external(
                audit.duration_ms / 1000,
                outcome="timeout",
            )
            raise self._error(
                ErrorCode.MODEL_TIMEOUT,
                "模型 API 请求超时",
                retryable=True,
                details={"path": path, **self._audit_details(audit)},
            ) from exc
        except httpx.RequestError as exc:
            audit = ModelHttpAudit(
                # Request errors can also be raised while consuming a
                # response body; keep any audit data collected so far.
                response_status=(audit.response_status if audit is not None else None),
                provider_request_id=(
                    audit.provider_request_id if audit is not None else None
                ),
                duration_ms=max(0, int((self._monotonic() - started) * 1000)),
                budget_reservation=reservation,
            )
            if not budget_settled:
                self._settle_budget_audit(audit, None, uncertain=True)
            self._observe_external(
                audit.duration_ms / 1000,
                outcome="network_error",
            )
            raise self._error(
                ErrorCode.MODEL_SERVER_ERROR,
                "模型 API 暂时无法访问",
                retryable=True,
                details={
                    "path": path,
                    "exception_type": type(exc).__name__,
                    **self._audit_details(audit),
                },
            ) from exc
        except Exception as exc:
            # 第三方 Transport/响应迭代器不一定继承 httpx.RequestError。
            # reservation 已在进入 HTTP 前创建；任何未知异常都必须先以
            # uncertain 结算，避免数据库中的 reserved 记录永久占用预算。
            audit = ModelHttpAudit(
                response_status=(audit.response_status if audit is not None else None),
                provider_request_id=(
                    audit.provider_request_id if audit is not None else None
                ),
                duration_ms=max(0, int((self._monotonic() - started) * 1000)),
                budget_reservation=reservation,
            )
            if not budget_settled:
                self._settle_budget_audit(audit, None, uncertain=True)
            self._observe_external(
                audit.duration_ms / 1000,
                outcome="network_error",
            )
            raise self._error(
                ErrorCode.MODEL_SERVER_ERROR,
                "模型 API 请求发生未预期错误",
                retryable=True,
                details={
                    "path": path,
                    "exception_type": type(exc).__name__,
                    **self._audit_details(audit),
                },
            ) from exc
        decoded: object
        try:
            content_type = response.headers.get("content-type", "")
            if self.api_protocol is ModelApiProtocol.RESPONSES and (
                "text/event-stream" in content_type.casefold()
                or _looks_like_sse(content)
            ):
                decoded = _parse_responses_sse(bytes(content))
            else:
                decoded = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._settle_budget_audit(audit, None, uncertain=True)
            self._observe_external(
                audit.duration_ms / 1000,
                outcome="invalid_response",
            )
            raise self._error(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "模型 API 返回了无法解析的 JSON",
                retryable=False,
                details=self._audit_details(audit),
            ) from exc
        if not isinstance(decoded, dict):
            self._settle_budget_audit(audit, None, uncertain=True)
            self._observe_external(
                audit.duration_ms / 1000,
                outcome="invalid_response",
            )
            raise self._error(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "模型 API 响应不是 JSON 对象",
                retryable=False,
                details=self._audit_details(audit),
            )
        self._observe_external(
            audit.duration_ms / 1000,
            status_code=audit.response_status,
        )
        return decoded, audit

    def _observe_external(
        self,
        duration_seconds: float,
        *,
        status_code: int | None = None,
        outcome: str | None = None,
    ) -> None:
        """指标采集只做旁路记录，不能改变模型调用或预算结算结果。"""

        try:
            self._telemetry.observe_external(
                self._telemetry_service,
                duration_seconds,
                status_code=status_code,
                outcome=outcome,
            )
        except Exception:
            # 指标后端异常时继续主流程；否则成功响应会在返回 audit 前中断，
            # 使调用方无法结算已经创建的预算预留。
            return

    @property
    def _telemetry_service(self) -> str:
        if self.provider is ModelProvider.OPENAI:
            return "model_openai"
        return "model_anthropic"

    def _audit(
        self,
        response: httpx.Response,
        started: float,
        reservation: ModelBudgetReservation | None = None,
    ) -> ModelHttpAudit:
        request_id = (
            response.headers.get("x-request-id")
            or response.headers.get("request-id")
        )
        if request_id is not None and (not request_id or len(request_id) > 200):
            request_id = None
        return ModelHttpAudit(
            response_status=response.status_code,
            provider_request_id=request_id,
            duration_ms=max(0, int((self._monotonic() - started) * 1000)),
            budget_reservation=reservation,
        )

    def _reserve_budget(
        self,
        request_content: bytes,
        body: dict[str, object],
    ) -> ModelBudgetReservation | None:
        accountant = current_model_budget_accountant()
        if accountant is None:
            return None
        output_limit = next(
            (
                value
                for name in ("max_output_tokens", "max_completion_tokens", "max_tokens")
                if isinstance((value := body.get(name)), int)
                and not isinstance(value, bool)
                and value >= 0
            ),
            self._settings.max_output_tokens,
        )
        input_estimate = estimate_model_request_tokens(
            body,
            request_content,
            provider=self.provider,
            protocol=self.api_protocol,
            model=self._settings.model,
        )
        # 规划阶段按同一估算口径切分批次。正常请求使用估算值加有限
        # 余量，避免把接近 UTF-8 字节数的结构上界重复计入四路 Agent
        # 的总预算；无法识别的请求仍由 ``reservation_tokens`` 退回
        # 完整序列化字节数，保持 fail-closed。
        input_limit = input_estimate.reservation_tokens
        cost_limit = (
            self._settings.pricing.upper_bound_microusd(input_limit, output_limit)
            if self._settings.pricing is not None
            else None
        )
        return accountant.reserve(
            ModelBudgetRequest(
                provider=self.provider.value,
                api_protocol=self.api_protocol.value,
                model=self._settings.model,
                request_bytes=len(request_content),
                input_token_upper_bound=input_limit,
                output_token_upper_bound=output_limit,
                cost_upper_bound_microusd=cost_limit,
            )
        )

    def _budget_timeout(
        self,
        reservation: ModelBudgetReservation | None,
    ) -> httpx.Timeout:
        if reservation is None:
            return self._settings.timeout
        remaining = max(0.001, reservation.remaining_duration_ms / 1000)
        return httpx.Timeout(
            connect=min(self._settings.connect_timeout_seconds, remaining),
            read=min(self._settings.read_timeout_seconds, remaining),
            write=min(self._settings.write_timeout_seconds, remaining),
            pool=min(self._settings.pool_timeout_seconds, remaining),
        )

    def _settle_budget_audit(
        self,
        audit: ModelHttpAudit | None,
        usage: ModelTokenUsage | None,
        *,
        uncertain: bool = False,
    ) -> None:
        accountant = current_model_budget_accountant()
        if (
            accountant is None
            or audit is None
            or audit.budget_reservation is None
        ):
            return
        estimated_cost = (
            self._settings.pricing.estimate_microusd(usage)
            if usage is not None and self._settings.pricing is not None
            else None
        )
        if (
            usage is not None
            and self._settings.pricing is not None
            and estimated_cost is None
        ):
            uncertain = True
        accountant.settle(
            audit.budget_reservation,
            input_tokens=usage.total_input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            estimated_cost_microusd=estimated_cost,
            response_status=audit.response_status,
            duration_ms=audit.duration_ms,
            uncertain=uncertain,
        )

    def _classify_response(
        self,
        response: httpx.Response,
        audit: ModelHttpAudit,
    ) -> SafeError:
        status = response.status_code
        if status == 401:
            code = ErrorCode.MODEL_AUTHENTICATION_FAILED
            message = "模型 API 密钥无效"
            retryable = False
        elif status == 403:
            code = ErrorCode.MODEL_PERMISSION_DENIED
            message = "模型 API 权限不足"
            retryable = False
        elif status in {408, 409, 429}:
            code = ErrorCode.MODEL_RATE_LIMITED
            message = "模型 API 暂时限流或请求冲突"
            retryable = True
        elif status == 524:
            code = ErrorCode.MODEL_SERVER_ERROR
            message = "模型中转站网关等待超时（524）"
            retryable = True
        elif status >= 500:
            code = ErrorCode.MODEL_SERVER_ERROR
            message = "模型 API 服务端暂时不可用"
            retryable = True
        else:
            code = ErrorCode.MODEL_REQUEST_REJECTED
            message = "模型 API 拒绝了当前请求"
            retryable = False
        details = self._audit_details(audit)
        if status == 524:
            details["upstream_timeout"] = True
        unsupported = _unsupported_parameters_from_response(response, status)
        if unsupported:
            details["unsupported_parameters"] = sorted(unsupported)
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            details["retry_after_seconds"] = int(retry_after)
        return SafeError(
            code=code,
            safe_message=message,
            retryable=retryable,
            details=details,
        )

    def _audit_details(self, audit: ModelHttpAudit) -> dict[str, object]:
        return {
            "provider": self.provider.value,
            "api_protocol": self.api_protocol.value,
            "model": self._settings.model,
            "status_code": audit.response_status,
            "provider_request_id": audit.provider_request_id,
            "duration_ms": audit.duration_ms,
        }

    def _error(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool,
        details: dict[str, object] | None = None,
    ) -> SafeApplicationError:
        safe_details: dict[str, object] = {
            "provider": self.provider.value,
            "api_protocol": self.api_protocol.value,
            "model": self._settings.model,
        }
        if details:
            safe_details.update(details)
        return SafeApplicationError(
            SafeError(
                code=code,
                safe_message=message,
                retryable=retryable,
                details=safe_details,
            )
        )

    def _parse_structured_text(self, text: str) -> ModelReviewOutput:
        if not text or len(text.encode("utf-8")) > self._settings.max_response_bytes:
            raise self._error(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "模型没有返回有效的结构化审查内容",
                retryable=False,
            )
        try:
            decoded = json.loads(text)
        except (json.JSONDecodeError, UnicodeError, TypeError) as exc:
            raise self._contract_error(
                ({"path": "$", "code": "invalid_json", "message": "输出不是有效 JSON"},)
            ) from exc
        if not isinstance(decoded, dict):
            raise self._contract_error(
                ({"path": "$", "code": "object_required", "message": "顶层必须是 JSON 对象"},)
            )
        missing = tuple(field for field in _REQUIRED_OUTPUT_FIELDS if field not in decoded)
        if missing:
            raise self._contract_error(
                tuple(
                    {
                        "path": field,
                        "code": "missing",
                        "message": f"缺少 {field} 字段",
                    }
                    for field in missing
                )
            )
        try:
            output = ModelReviewOutput.model_validate(decoded)
        except ValidationError as exc:
            raise self._contract_error(_validation_issues(exc)) from exc
        if output.verdict is None or output.summary is None:
            invalid = []
            if output.verdict is None:
                invalid.append(
                    {"path": "verdict", "code": "invalid", "message": "verdict 不能为空"}
                )
            if output.summary is None:
                invalid.append(
                    {"path": "summary", "code": "invalid", "message": "summary 不能为空"}
                )
            raise self._contract_error(tuple(invalid))
        return output

    def _contract_error(
        self,
        issues: tuple[dict[str, str], ...],
    ) -> SafeApplicationError:
        bounded = issues[:_MAX_VALIDATION_ISSUES] or (
            {"path": "$", "code": "invalid", "message": "输出结构不符合要求"},
        )
        return self._error(
            ErrorCode.MODEL_INVALID_RESPONSE,
            f"模型结构化输出不符合审查契约：{bounded[0]['message']}",
            retryable=False,
            details={
                "contract_validation": True,
                "validation_issue_count": len(issues),
                "validation_issues": list(bounded),
            },
        )

    @staticmethod
    def _attach_failed_usage(
        error: SafeApplicationError,
        usage: ModelTokenUsage,
    ) -> SafeApplicationError:
        if error.error.details.get("contract_validation") is not True:
            return error
        return SafeApplicationError(
            SafeError(
                code=error.error.code,
                safe_message=error.error.safe_message,
                retryable=error.error.retryable,
                details={
                    **dict(error.error.details),
                    **_failed_usage_details(usage),
                },
            )
        )


class OpenAIResponsesReviewer(_StructuredModelReviewer):
    """OpenAI Responses API 适配器。"""

    provider = ModelProvider.OPENAI
    api_protocol = ModelApiProtocol.RESPONSES
    request_path = "/v1/responses"

    def _request_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }

    def _request_body(self, prompt: ReviewPrompt) -> dict[str, object]:
        body: dict[str, object] = {
            "model": self._settings.model,
            "max_output_tokens": self._settings.max_output_tokens,
            "stream": True,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": prompt.system}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt.user}],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "openreviewer_findings",
                    "strict": True,
                    "schema": model_review_output_schema(),
                }
            },
        }
        endpoint_host = urlsplit(self._settings.resolved_api_base_url).hostname
        if self._settings.api_base_url is None or endpoint_host in {
            "api.openai.com",
            # Unit tests use a public-looking mock hostname to exercise the
            # official request shape without making a network call.
            "api.openai.test",
        }:
            body["store"] = False
        if self._settings.reasoning_effort.value != "none":
            body["reasoning"] = {
                "effort": self._settings.reasoning_effort.value,
            }
        return body

    def _parse_success(
        self,
        payload: dict[str, object],
    ) -> tuple[ModelReviewOutput, ModelTokenUsage, str | None]:
        status = payload.get("status")
        if status == "incomplete":
            incomplete_details = payload.get("incomplete_details")
            reason = (
                incomplete_details.get("reason")
                if isinstance(incomplete_details, dict)
                else None
            )
            details = _failed_response_details(
                payload,
                self.api_protocol,
                incomplete=True,
                incomplete_reason=reason,
            )
            if reason == "content_filter":
                raise self._error(
                    ErrorCode.MODEL_OUTPUT_REFUSED,
                    "OpenAI 模型拒绝了当前审查请求",
                    retryable=False,
                    details=details,
                )
            raise self._error(
                ErrorCode.MODEL_OUTPUT_TRUNCATED,
                (
                    "OpenAI 模型输出达到 Token 上限"
                    if reason == "max_output_tokens"
                    else "OpenAI 模型输出未完整结束"
                ),
                retryable=False,
                details=details,
            )
        if status != "completed":
            raise self._error(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "OpenAI Responses 返回了未知完成状态",
                retryable=False,
            )
        raw_output = payload.get("output")
        if not isinstance(raw_output, list):
            raise self._invalid_openai_response()
        text_parts: list[str] = []
        for item in raw_output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                raise self._invalid_openai_response()
            for block in content:
                if not isinstance(block, dict):
                    raise self._invalid_openai_response()
                if block.get("type") == "refusal":
                    raise self._error(
                        ErrorCode.MODEL_OUTPUT_REFUSED,
                        "OpenAI 模型拒绝了当前审查请求",
                        retryable=False,
                    )
                if block.get("type") == "output_text":
                    raw_text = block.get("text")
                    if not isinstance(raw_text, str):
                        raise self._invalid_openai_response()
                    text_parts.append(raw_text)
        if not text_parts:
            raise self._invalid_openai_response()
        raw_usage = payload.get("usage")
        if not isinstance(raw_usage, dict):
            raise self._invalid_openai_response()
        total_input = _required_nonnegative_int(raw_usage, "input_tokens")
        output_tokens = _required_nonnegative_int(raw_usage, "output_tokens")
        input_details = raw_usage.get("input_tokens_details")
        cached_tokens = (
            _optional_nonnegative_int(input_details, "cached_tokens")
            if isinstance(input_details, dict)
            else 0
        )
        output_details = raw_usage.get("output_tokens_details")
        reasoning_tokens = (
            _optional_nonnegative_int(output_details, "reasoning_tokens")
            if isinstance(output_details, dict)
            else 0
        )
        if cached_tokens > total_input or reasoning_tokens > output_tokens:
            raise self._invalid_openai_response()
        usage = ModelTokenUsage(
            input_tokens=total_input - cached_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cached_tokens,
            reasoning_output_tokens=reasoning_tokens,
        )
        try:
            output = self._parse_structured_text("".join(text_parts))
        except SafeApplicationError as exc:
            raise self._attach_failed_usage(exc, usage) from exc
        return output, usage, _optional_identifier(payload.get("id"))

    def _invalid_openai_response(self) -> SafeApplicationError:
        return self._error(
            ErrorCode.MODEL_INVALID_RESPONSE,
            "OpenAI Responses 响应格式无效",
            retryable=False,
        )


class OpenAIChatCompletionsReviewer(_StructuredModelReviewer):
    """OpenAI Chat Completions API 适配器。"""

    provider = ModelProvider.OPENAI
    api_protocol = ModelApiProtocol.CHAT_COMPLETIONS
    request_path = "/v1/chat/completions"

    def _request_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }

    def _request_body(self, prompt: ReviewPrompt) -> dict[str, object]:
        body: dict[str, object] = {
            "model": self._settings.model,
            "max_completion_tokens": self._settings.max_output_tokens,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "openreviewer_findings",
                    "strict": True,
                    "schema": model_review_output_schema(),
                },
            },
        }
        # 官方 Responses/Chat 端点支持 ``store=false``，但大量 OpenAI 兼容
        # 中转站使用更窄的请求模型，遇到这个无关字段会直接拒绝请求。中转
        # 地址不需要显式关闭存储（代理应按自身策略处理），因此只对官方端点
        # 发送它，避免把一次本可成功的审查变成 400/500。
        endpoint_host = urlsplit(self._settings.resolved_api_base_url).hostname
        if self._settings.api_base_url is None or endpoint_host in {
            "api.openai.com",
            # Unit tests use a public-looking mock hostname to exercise the
            # official request shape without making a network call.
            "api.openai.test",
        }:
            body["store"] = False
        if self._settings.reasoning_effort.value != "none":
            body["reasoning_effort"] = self._settings.reasoning_effort.value
        return body

    def _parse_success(
        self,
        payload: dict[str, object],
    ) -> tuple[ModelReviewOutput, ModelTokenUsage, str | None]:
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise self._invalid_chat_completions_response()
        choice = choices[0]
        if not isinstance(choice, dict):
            raise self._invalid_chat_completions_response()
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise self._error(
                ErrorCode.MODEL_OUTPUT_TRUNCATED,
                "OpenAI 模型输出达到 Token 上限",
                retryable=False,
                details=_failed_response_details(
                    payload,
                    self.api_protocol,
                    finish_reason=finish_reason,
                ),
            )
        if finish_reason == "content_filter":
            raise self._error(
                ErrorCode.MODEL_OUTPUT_REFUSED,
                "OpenAI 模型拒绝了当前审查请求",
                retryable=False,
            )
        if finish_reason != "stop":
            raise self._error(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "OpenAI Chat Completions 返回了未知结束原因",
                retryable=False,
            )
        message = choice.get("message")
        if not isinstance(message, dict):
            raise self._invalid_chat_completions_response()
        refusal = message.get("refusal")
        if refusal is not None:
            if not isinstance(refusal, str):
                raise self._invalid_chat_completions_response()
            raise self._error(
                ErrorCode.MODEL_OUTPUT_REFUSED,
                "OpenAI 模型拒绝了当前审查请求",
                retryable=False,
            )
        content = message.get("content")
        if not isinstance(content, str):
            raise self._invalid_chat_completions_response()
        raw_usage = payload.get("usage")
        if not isinstance(raw_usage, dict):
            raise self._invalid_chat_completions_response()
        total_input = _required_nonnegative_int(raw_usage, "prompt_tokens")
        output_tokens = _required_nonnegative_int(raw_usage, "completion_tokens")
        input_details = raw_usage.get("prompt_tokens_details")
        cached_tokens = (
            _optional_nonnegative_int(input_details, "cached_tokens")
            if isinstance(input_details, dict)
            else 0
        )
        output_details = raw_usage.get("completion_tokens_details")
        reasoning_tokens = (
            _optional_nonnegative_int(output_details, "reasoning_tokens")
            if isinstance(output_details, dict)
            else 0
        )
        if cached_tokens > total_input or reasoning_tokens > output_tokens:
            raise self._invalid_chat_completions_response()
        usage = ModelTokenUsage(
            input_tokens=total_input - cached_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cached_tokens,
            reasoning_output_tokens=reasoning_tokens,
        )
        try:
            output = self._parse_structured_text(content)
        except SafeApplicationError as exc:
            raise self._attach_failed_usage(exc, usage) from exc
        return output, usage, _optional_identifier(payload.get("id"))

    def _invalid_chat_completions_response(self) -> SafeApplicationError:
        return self._error(
            ErrorCode.MODEL_INVALID_RESPONSE,
            "OpenAI Chat Completions 响应格式无效",
            retryable=False,
        )


class AnthropicModelReviewer(_StructuredModelReviewer):
    """Anthropic Messages API 适配器。"""

    provider = ModelProvider.ANTHROPIC
    api_protocol = ModelApiProtocol.MESSAGES
    request_path = "/v1/messages"

    def _request_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._settings.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _request_body(self, prompt: ReviewPrompt) -> dict[str, object]:
        output_config: dict[str, object] = {
            "format": {
                "type": "json_schema",
                "schema": model_review_output_schema(),
            }
        }
        if self._settings.reasoning_effort.value != "none":
            output_config["effort"] = self._settings.reasoning_effort.value
        return {
            "model": self._settings.model,
            "max_tokens": self._settings.max_output_tokens,
            "system": prompt.system,
            "messages": [{"role": "user", "content": prompt.user}],
            "output_config": output_config,
        }

    def _parse_success(
        self,
        payload: dict[str, object],
    ) -> tuple[ModelReviewOutput, ModelTokenUsage, str | None]:
        stop_reason = payload.get("stop_reason")
        if stop_reason == "max_tokens":
            raise self._error(
                ErrorCode.MODEL_OUTPUT_TRUNCATED,
                "Anthropic 模型输出达到 Token 上限",
                retryable=False,
                details=_failed_response_details(
                    payload,
                    self.api_protocol,
                    stop_reason=stop_reason,
                ),
            )
        if stop_reason == "refusal":
            raise self._error(
                ErrorCode.MODEL_OUTPUT_REFUSED,
                "Anthropic 模型拒绝了当前审查请求",
                retryable=False,
            )
        if stop_reason != "end_turn":
            raise self._error(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "Anthropic Messages 返回了未知结束原因",
                retryable=False,
            )
        content = payload.get("content")
        if not isinstance(content, list):
            raise self._invalid_anthropic_response()
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                raise self._invalid_anthropic_response()
            if block.get("type") == "text":
                raw_text = block.get("text")
                if not isinstance(raw_text, str):
                    raise self._invalid_anthropic_response()
                text_parts.append(raw_text)
        if not text_parts:
            raise self._invalid_anthropic_response()
        raw_usage = payload.get("usage")
        if not isinstance(raw_usage, dict):
            raise self._invalid_anthropic_response()
        usage = ModelTokenUsage(
            input_tokens=_required_nonnegative_int(raw_usage, "input_tokens"),
            output_tokens=_required_nonnegative_int(raw_usage, "output_tokens"),
            cache_read_input_tokens=_optional_nonnegative_int(
                raw_usage,
                "cache_read_input_tokens",
            ),
            cache_write_input_tokens=_optional_nonnegative_int(
                raw_usage,
                "cache_creation_input_tokens",
            ),
        )
        try:
            output = self._parse_structured_text("".join(text_parts))
        except SafeApplicationError as exc:
            raise self._attach_failed_usage(exc, usage) from exc
        return output, usage, _optional_identifier(payload.get("id"))

    def _invalid_anthropic_response(self) -> SafeApplicationError:
        return self._error(
            ErrorCode.MODEL_INVALID_RESPONSE,
            "Anthropic Messages 响应格式无效",
            retryable=False,
        )


def create_model_reviewer(
    settings: ModelServiceSettings,
    *,
    client: httpx.Client | None = None,
    monotonic: Callable[[], float] | None = None,
    telemetry: TelemetryRegistry | None = None,
) -> ModelReviewer:
    """按启动配置选择适配器，Worker 的其余代码无需供应商分支。"""

    if settings.provider is ModelProvider.OPENAI:
        adapter = (
            OpenAIResponsesReviewer
            if settings.resolved_api_protocol is ModelApiProtocol.RESPONSES
            else OpenAIChatCompletionsReviewer
        )
        return adapter(
            settings,
            client=client,
            monotonic=monotonic,
            telemetry=telemetry,
        )
    return AnthropicModelReviewer(
        settings,
        client=client,
        monotonic=monotonic,
        telemetry=telemetry,
    )


def _summary_prompt_input(review_input: ModelReviewInput) -> ModelReviewInput:
    """为汇总模型构造不含原始补丁的提示词快照。

    ``FixedAgentWorkflow`` 已经在正常路径清空这些字段；这里再做一次边界
    防护，避免其他调用方直接复用汇总适配器时意外发送大 PR 内容。
    """

    if review_input.review_agent is not ReviewAgent.SUMMARY:
        return review_input
    if not review_input.rules and not review_input.units:
        return review_input
    return review_input.model_copy(
        update={
            "rules": (),
            "units": (),
            "total_estimated_input_bytes": 0,
        }
    )


def _required_nonnegative_int(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_nonnegative_int(payload: dict[str, object], name: str) -> int:
    value = payload.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _validation_issues(error: ValidationError) -> tuple[dict[str, str], ...]:
    """把 Pydantic 错误压缩成不含模型原文的字段级诊断。"""

    issues: list[dict[str, str]] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        path = _safe_validation_path(item.get("loc", ()))
        raw_type = item.get("type")
        issue_type = raw_type if isinstance(raw_type, str) else "invalid"
        if issue_type == "missing":
            code = "missing"
            message = f"缺少 {path} 字段"
        elif issue_type == "extra_forbidden":
            code = "extra"
            message = f"{path} 是不允许的额外字段"
        elif issue_type == "enum":
            code = "enum"
            message = f"{path} 使用了不支持的枚举值"
        elif issue_type.startswith("json_"):
            code = "invalid_json"
            message = "输出不是有效 JSON"
        elif path == "$":
            code = "inconsistent"
            message = "verdict 与 findings 的组合不一致"
        else:
            code = "invalid"
            message = f"{path} 的类型或取值不符合要求"
        issues.append({"path": path, "code": code, "message": message})
        if len(issues) >= _MAX_VALIDATION_ISSUES:
            break
    return tuple(issues)


def _safe_validation_path(location: object) -> str:
    if not isinstance(location, (list, tuple)):
        return "$"
    parts: list[str] = []
    for item in location[:12]:
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            parts.append(f"[{item}]")
        elif isinstance(item, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", item):
            parts.append(("." if parts else "") + item)
        else:
            parts.append(("." if parts else "") + "<field>")
    return "".join(parts) or "$"


def _validation_issue_paths(details: object) -> tuple[str, ...]:
    if not isinstance(details, dict):
        return ()
    raw = details.get("validation_issues")
    if not isinstance(raw, (list, tuple)):
        return ()
    paths = []
    for item in raw[:_MAX_VALIDATION_ISSUES]:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if isinstance(path, str) and 0 < len(path) <= 256:
            paths.append(path)
    return tuple(dict.fromkeys(paths))


def _failed_usage_details(usage: ModelTokenUsage) -> dict[str, int]:
    return {
        "failed_input_tokens": usage.input_tokens,
        "failed_output_tokens": usage.output_tokens,
        "failed_cache_read_tokens": usage.cache_read_input_tokens,
        "failed_cache_write_tokens": usage.cache_write_input_tokens,
        "failed_reasoning_tokens": usage.reasoning_output_tokens,
    }


def _failed_response_details(
    payload: dict[str, object],
    protocol: ModelApiProtocol,
    **extra: object,
) -> dict[str, object]:
    """提取截断响应中的有限诊断字段和可信用量。"""

    details: dict[str, object] = {
        key: value
        for key, value in extra.items()
        if value is not None
        and (
            not isinstance(value, str)
            or (value and len(value) <= 64 and "\n" not in value)
        )
    }
    usage = _usage_from_payload(payload, protocol)
    if usage is not None:
        details.update(_failed_usage_details(usage))
    return details


def _usage_from_payload(
    payload: dict[str, object],
    protocol: ModelApiProtocol,
) -> ModelTokenUsage | None:
    """从各协议响应提取用量；字段不完整时返回 None 而不抛出新错误。"""

    raw_usage = payload.get("usage")
    if not isinstance(raw_usage, dict):
        return None
    try:
        if protocol is ModelApiProtocol.RESPONSES:
            total_input = _required_nonnegative_int(raw_usage, "input_tokens")
            output_tokens = _required_nonnegative_int(raw_usage, "output_tokens")
            input_details = raw_usage.get("input_tokens_details")
            cached_tokens = (
                _optional_nonnegative_int(input_details, "cached_tokens")
                if isinstance(input_details, dict)
                else 0
            )
            output_details = raw_usage.get("output_tokens_details")
            reasoning_tokens = (
                _optional_nonnegative_int(output_details, "reasoning_tokens")
                if isinstance(output_details, dict)
                else 0
            )
            if cached_tokens > total_input or reasoning_tokens > output_tokens:
                return None
            return ModelTokenUsage(
                input_tokens=total_input - cached_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cached_tokens,
                reasoning_output_tokens=reasoning_tokens,
            )
        if protocol is ModelApiProtocol.CHAT_COMPLETIONS:
            total_input = _required_nonnegative_int(raw_usage, "prompt_tokens")
            output_tokens = _required_nonnegative_int(raw_usage, "completion_tokens")
            input_details = raw_usage.get("prompt_tokens_details")
            cached_tokens = (
                _optional_nonnegative_int(input_details, "cached_tokens")
                if isinstance(input_details, dict)
                else 0
            )
            output_details = raw_usage.get("completion_tokens_details")
            reasoning_tokens = (
                _optional_nonnegative_int(output_details, "reasoning_tokens")
                if isinstance(output_details, dict)
                else 0
            )
            if cached_tokens > total_input or reasoning_tokens > output_tokens:
                return None
            return ModelTokenUsage(
                input_tokens=total_input - cached_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cached_tokens,
                reasoning_output_tokens=reasoning_tokens,
            )
        return ModelTokenUsage(
            input_tokens=_required_nonnegative_int(raw_usage, "input_tokens"),
            output_tokens=_required_nonnegative_int(raw_usage, "output_tokens"),
            cache_read_input_tokens=_optional_nonnegative_int(
                raw_usage,
                "cache_read_input_tokens",
            ),
            cache_write_input_tokens=_optional_nonnegative_int(
                raw_usage,
                "cache_creation_input_tokens",
            ),
        )
    except (TypeError, ValueError, ValidationError):
        return None


def _known_failed_usage(details: object) -> ModelTokenUsage | None:
    """仅在错误详情包含一组完整可信的用量时返回它。"""

    if not isinstance(details, dict):
        return None
    names = (
        "failed_input_tokens",
        "failed_output_tokens",
        "failed_cache_read_tokens",
        "failed_cache_write_tokens",
        "failed_reasoning_tokens",
    )
    if any(
        name not in details
        or isinstance(details[name], bool)
        or not isinstance(details[name], int)
        or details[name] < 0
        for name in names
    ):
        return None
    return _failed_usage(details)


def _failed_usage(details: object) -> ModelTokenUsage:
    if not isinstance(details, dict):
        return ModelTokenUsage(input_tokens=0, output_tokens=0)

    def value(name: str) -> int:
        candidate = details.get(name, 0)
        return (
            candidate
            if isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and candidate >= 0
            else 0
        )

    return ModelTokenUsage(
        input_tokens=value("failed_input_tokens"),
        output_tokens=value("failed_output_tokens"),
        cache_read_input_tokens=value("failed_cache_read_tokens"),
        cache_write_input_tokens=value("failed_cache_write_tokens"),
        reasoning_output_tokens=value("failed_reasoning_tokens"),
    )


def _combine_usage(first: ModelTokenUsage, second: ModelTokenUsage) -> ModelTokenUsage:
    return ModelTokenUsage(
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        cache_read_input_tokens=(
            first.cache_read_input_tokens + second.cache_read_input_tokens
        ),
        cache_write_input_tokens=(
            first.cache_write_input_tokens + second.cache_write_input_tokens
        ),
        reasoning_output_tokens=(
            first.reasoning_output_tokens + second.reasoning_output_tokens
        ),
    )


def _join_request_ids(first: str | None, second: str | None) -> str | None:
    values = tuple(dict.fromkeys(item for item in (first, second) if item))
    if not values:
        return None
    joined = ",".join(values)
    return joined if len(joined) <= 200 else values[-1]


def _unsupported_parameters_from_response(
    response: httpx.Response,
    status_code: int,
) -> set[str]:
    """从受限错误片段中识别可安全降级的可选参数。

    供应商错误正文只在内存中读取最多 16 KiB，且最终只保留参数名称；正文、
    URL 和任何潜在凭据都不会进入 ``SafeError.details``。没有明确“不支持”语义
    时返回空集合，调用方不会重试。
    """

    # Compatibility errors are occasionally surfaced by a relay as 5xx (the
    # upstream adapter rejects the request while translating it).  Do not
    # inspect or retry authentication, throttling, timeout, or conflict
    # responses; only ordinary validation and explicit relay failures qualify.
    if not 400 <= status_code <= 599 or status_code in {401, 403, 408, 409, 429}:
        return set()
    raw = bytearray()
    try:
        for chunk in response.iter_bytes():
            if not chunk:
                continue
            remaining = _COMPATIBILITY_ERROR_BODY_LIMIT - len(raw)
            if remaining <= 0:
                break
            raw.extend(chunk[:remaining])
            if len(raw) >= _COMPATIBILITY_ERROR_BODY_LIMIT:
                break
    except (httpx.HTTPError, RuntimeError, ValueError):
        return set()
    if not raw:
        return set()
    text = bytes(raw).decode("utf-8", errors="ignore")
    extracted: list[str] = []
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, UnicodeDecodeError):
        decoded = None
    if decoded is not None:
        _collect_error_tokens(decoded, extracted)
    source = f"{text} {' '.join(extracted)}".casefold()
    normalized = re.sub(r"[^a-z0-9一-鿿]+", "_", source).strip("_")
    explicit_unsupported = any(
        marker in source
        for marker in (
            *_UNSUPPORTED_MARKERS,
            "不支持",
            "未知参数",
            "未知字段",
            "未识别参数",
        )
    ) or any(
        token.casefold() in {
            "unsupported_parameter",
            "unsupported_field",
            "unknown_parameter",
            "unknown_field",
            "unrecognized_parameter",
            "unrecognized_field",
        }
        for token in extracted
    )
    # A number of relays return e.g. ``Invalid value: 'json_schema'.
    # Supported values are: 'text', 'json_object'.``.  Treat that wording as
    # an incompatibility only when a known request parameter is present below;
    # a bare 400 with arbitrary "invalid value" text must remain non-retryable.
    supported_value_error = _has_invalid_supported_value_markers(source)
    if not explicit_unsupported and not supported_value_error:
        return set()

    found: set[str] = set()
    for canonical, aliases in _COMPATIBILITY_PARAMETER_ALIASES:
        for alias in aliases:
            alias_lower = alias.casefold()
            if re.search(
                rf"(?<![a-z0-9]){re.escape(alias_lower)}(?![a-z0-9])",
                source,
            ) or alias_lower in normalized:
                found.add(canonical)
                break
    # ``reasoning_effort`` also contains the shorter word ``reasoning``; the
    # former is more precise and lets the fallback touch only the actual field.
    if "reasoning_effort" in found:
        found.discard("reasoning")
    if "json_schema" in found:
        # Keep both names: one endpoint nests the schema under response_format,
        # another reports the nested field directly.
        found.add("response_format")
    return found


def _has_invalid_supported_value_markers(source: str) -> bool:
    """判断错误是否明确表示“当前值无效、只支持另一组值”。

    错误正文来自第三方，可能包含任意自然语言；这里仅做大小写不敏感的
    有界标记匹配，不把正文写入持久化错误，也不对普通 ``invalid`` 直接
    触发兼容重试。
    """

    has_invalid = any(marker in source for marker in _INVALID_SUPPORTED_VALUE_MARKERS)
    has_supported = any(marker in source for marker in _SUPPORTED_VALUE_MARKERS)
    return has_invalid and has_supported


def _collect_error_tokens(value: object, output: list[str], depth: int = 0) -> None:
    """有界提取供应商错误中的参数名，不保留任意响应内容。"""

    if depth > 6 or len(output) >= 64:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if len(output) >= 64:
                return
            if isinstance(item, str) and str(key).casefold() in {
                "param",
                "parameter",
                "field",
                "path",
                "code",
                "type",
                "error_type",
                "message",
            }:
                output.append(item[:512])
            _collect_error_tokens(item, output, depth + 1)
    elif isinstance(value, list):
        for item in value[:64]:
            _collect_error_tokens(item, output, depth + 1)


def _looks_like_sse(content: bytes | bytearray) -> bool:
    """识别少数未正确设置 Content-Type 的 Responses SSE 响应。"""

    prefix = bytes(content[:256]).lstrip()
    return prefix.startswith(b"data:") or prefix.startswith(b"event:")


def _parse_responses_sse(content: bytes) -> dict[str, object]:
    """把 Responses SSE 事件收敛成现有的完整响应对象。

    官方终态事件 ``response.completed`` 自带完整 response；部分兼容中转站
    只发送文本增量和用量，因此在信息足够时构造最小 completed 响应。事件正文
    只在当前请求内存中处理，并受 ``max_response_bytes`` 的上限保护。
    """

    text = content.decode("utf-8")
    data_lines: list[str] = []
    event_name: str | None = None
    event_payloads: list[tuple[str | None, str]] = []

    def flush_event() -> None:
        nonlocal event_name
        if not data_lines:
            event_name = None
            return
        event_payloads.append((event_name, "\n".join(data_lines)))
        data_lines.clear()
        event_name = None

    # SSE 允许 CRLF、LF 或 CR；空行标记一条事件结束。
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line == "":
            flush_event()
            continue
        if line.startswith(":"):
            continue
        field, separator, field_value = line.partition(":")
        if not separator:
            continue
        if field == "event":
            # 合规 SSE 用空行分隔事件；兼容部分中转站省略空行时，新的
            # event 字段仍可作为安全的边界，避免把两个 JSON 拼在一起。
            flush_event()
            event_name = field_value
            continue
        if field == "data":
            data_lines.append(
                field_value[1:] if field_value.startswith(" ") else field_value
            )
    flush_event()
    if not event_payloads:
        raise ValueError("Responses SSE 没有数据事件")

    completed: dict[str, object] | None = None
    delta_parts: list[str] = []
    done_text: str | None = None
    latest_usage: dict[str, object] | None = None
    response_id: str | None = None

    for event_name, raw_event in event_payloads:
        if raw_event == "[DONE]":
            continue
        try:
            event = json.loads(raw_event)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Responses SSE 数据事件不是有效 JSON") from exc
        if not isinstance(event, dict):
            raise ValueError("Responses SSE 数据事件必须是 JSON 对象")

        payload_type = event.get("type")
        event_type = (
            payload_type
            if isinstance(payload_type, str)
            else event_name
        )
        candidate_usage = event.get("usage")
        if candidate_usage is not None:
            if not isinstance(candidate_usage, dict):
                raise ValueError("Responses SSE 用量对象无效")
            latest_usage = dict(candidate_usage)
        candidate_event_id = event.get("id")
        if candidate_event_id is not None:
            if not isinstance(candidate_event_id, str):
                raise ValueError("Responses SSE 响应 ID 无效")
            response_id = candidate_event_id
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise ValueError("Responses SSE 文本增量无效")
            delta_parts.append(delta)
            continue
        if event_type == "response.output_text.done":
            value: object = event.get("text")
            if value is not None and not isinstance(value, str):
                raise ValueError("Responses SSE 完成文本无效")
            if isinstance(value, str):
                done_text = value
            continue

        response_value = event.get("response")
        if event_type in {"response.completed", "response.done", "response.failed"}:
            if not isinstance(response_value, dict):
                raise ValueError("Responses SSE 终态事件缺少 response 对象")
            completed = dict(response_value)
            candidate_id = completed.get("id")
            if candidate_id is not None:
                if not isinstance(candidate_id, str):
                    raise ValueError("Responses SSE 响应 ID 无效")
                response_id = candidate_id
            candidate_usage = completed.get("usage")
            if candidate_usage is not None:
                if not isinstance(candidate_usage, dict):
                    raise ValueError("Responses SSE 用量对象无效")
                latest_usage = dict(candidate_usage)
            continue

        # 兼容直接把完整 response 放在 data 中、但省略 type 的中转站。
        if (
            isinstance(event.get("status"), str)
            and ("output" in event or "usage" in event)
        ):
            completed = dict(event)
            candidate_id = completed.get("id")
            if candidate_id is not None:
                if not isinstance(candidate_id, str):
                    raise ValueError("Responses SSE 响应 ID 无效")
                response_id = candidate_id
            candidate_usage = completed.get("usage")
            if isinstance(candidate_usage, dict):
                latest_usage = dict(candidate_usage)

    if completed is not None:
        if "usage" not in completed and latest_usage is not None:
            completed["usage"] = latest_usage
        if "output" not in completed:
            text_value = done_text if done_text is not None else "".join(delta_parts)
            if text_value:
                completed["output"] = [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text_value}],
                    }
                ]
        return completed

    text_value = done_text if done_text is not None else "".join(delta_parts)
    if not text_value or latest_usage is None:
        raise ValueError("Responses SSE 缺少完整响应或必要的文本/用量")
    return {
        "id": response_id,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text_value}],
            }
        ],
        "usage": latest_usage,
    }


def _optional_identifier(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError("provider response ID is invalid")
    return value
