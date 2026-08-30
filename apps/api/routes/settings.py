"""模型、Agent 与审查预算设置路由。"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, status

from apps.api.schemas import (
    AiAgentEnabledRequest,
    AiAgentSettingsResponse,
    AiAgentUpdateRequest,
    AiProviderUpdateRequest,
    AiRevisionRequest,
    AiSettingsResponse,
    ConfigurationAuditListResponse,
    ConfigurationAuditResponse,
    ReviewPolicyUpdateRequest,
)
from domain.enums import ModelProvider, ReviewAgent
from services.agent_settings import AgentSettingsService
from services.ai_settings import (
    AiConnectionTestError,
    AiProviderNotReadyError,
    AiSettingsConfigurationError,
    AiSettingsConflictError,
    AiSettingsPersistenceError,
    AiSettingsService,
    AiSettingsValidationError,
    ReviewPolicyDraft,
)
from services.auth import SessionPrincipal


def register_settings_routes(
    application: FastAPI,
    *,
    get_ai_settings_service: Callable[[], AiSettingsService],
    get_agent_settings_service: Callable[[], AgentSettingsService],
    ai_settings_response: Callable[[], AiSettingsResponse],
    require_settings_manager: Callable[..., SessionPrincipal],
    require_same_origin: Callable[..., None],
    translate_ai_settings_error: Callable[[Exception], HTTPException],
) -> None:
    """注册脱敏配置读取、写入、连接测试和审计端点。"""

    @application.get(
        "/api/v1/settings/ai",
        response_model=AiSettingsResponse,
    )
    def get_ai_settings(
        _: Annotated[SessionPrincipal, Depends(require_settings_manager)],
    ) -> AiSettingsResponse:
        return ai_settings_response()

    @application.get(
        "/api/v1/settings/ai/agents",
        response_model=AiAgentSettingsResponse,
    )
    def get_agent_settings(
        _: Annotated[SessionPrincipal, Depends(require_settings_manager)],
    ) -> AiAgentSettingsResponse:
        try:
            return AiAgentSettingsResponse.from_view(
                get_agent_settings_service().get()
            )
        except (AiSettingsPersistenceError, AiSettingsConfigurationError) as exc:
            raise translate_ai_settings_error(exc) from exc

    @application.put(
        "/api/v1/settings/ai/agents/{agent}",
        response_model=AiAgentSettingsResponse,
    )
    def update_agent_settings(
        agent: ReviewAgent,
        request_body: AiAgentUpdateRequest,
        principal: Annotated[
            SessionPrincipal,
            Depends(require_settings_manager),
        ],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AiAgentSettingsResponse:
        try:
            view = get_agent_settings_service().update(
                agent,
                request_body.to_draft(),
                expected_revision=request_body.expected_revision,
                actor=principal.username,
                api_key=request_body.api_key,
                clear_api_key=request_body.clear_api_key,
            )
        except (
            AiSettingsConfigurationError,
            AiSettingsConflictError,
            AiSettingsPersistenceError,
            AiSettingsValidationError,
        ) as exc:
            raise translate_ai_settings_error(exc) from exc
        return AiAgentSettingsResponse.from_view(view)

    @application.post(
        "/api/v1/settings/ai/agents/{agent}/test",
        response_model=AiAgentSettingsResponse,
    )
    def test_agent_settings(
        agent: ReviewAgent,
        request_body: AiRevisionRequest,
        principal: Annotated[
            SessionPrincipal,
            Depends(require_settings_manager),
        ],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AiAgentSettingsResponse:
        try:
            return AiAgentSettingsResponse.from_view(
                get_agent_settings_service().test(
                    agent,
                    expected_revision=request_body.expected_revision,
                    actor=principal.username,
                )
            )
        except AiConnectionTestError as exc:
            raise HTTPException(
                status_code=(
                    status.HTTP_503_SERVICE_UNAVAILABLE
                    if exc.retryable
                    else status.HTTP_422_UNPROCESSABLE_CONTENT
                ),
                detail=str(exc),
            ) from exc
        except (
            AiSettingsConfigurationError,
            AiSettingsConflictError,
            AiSettingsPersistenceError,
            AiSettingsValidationError,
        ) as exc:
            raise translate_ai_settings_error(exc) from exc

    @application.post(
        "/api/v1/settings/ai/agents/{agent}/enabled",
        response_model=AiAgentSettingsResponse,
    )
    def set_agent_enabled(
        agent: ReviewAgent,
        request_body: AiAgentEnabledRequest,
        principal: Annotated[
            SessionPrincipal,
            Depends(require_settings_manager),
        ],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AiAgentSettingsResponse:
        try:
            return AiAgentSettingsResponse.from_view(
                get_agent_settings_service().set_enabled(
                    agent,
                    request_body.enabled,
                    expected_revision=request_body.expected_revision,
                    actor=principal.username,
                )
            )
        except (
            AiSettingsConfigurationError,
            AiSettingsConflictError,
            AiSettingsPersistenceError,
            AiSettingsValidationError,
        ) as exc:
            raise translate_ai_settings_error(exc) from exc

    @application.put(
        "/api/v1/settings/ai/providers/{provider}",
        response_model=AiSettingsResponse,
    )
    def update_ai_provider(
        provider: ModelProvider,
        request_body: AiProviderUpdateRequest,
        principal: Annotated[
            SessionPrincipal,
            Depends(require_settings_manager),
        ],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AiSettingsResponse:
        try:
            view = get_ai_settings_service().update_provider(
                provider,
                request_body.to_draft(),
                expected_revision=request_body.expected_revision,
                actor=principal.username,
                api_key=request_body.api_key,
                clear_api_key=request_body.clear_api_key,
            )
        except (
            AiSettingsConfigurationError,
            AiSettingsConflictError,
            AiSettingsPersistenceError,
            AiSettingsValidationError,
        ) as exc:
            raise translate_ai_settings_error(exc) from exc
        return AiSettingsResponse.from_view(view)

    @application.post(
        "/api/v1/settings/ai/providers/{provider}/test",
        response_model=AiSettingsResponse,
    )
    def test_ai_provider(
        provider: ModelProvider,
        request_body: AiRevisionRequest,
        principal: Annotated[
            SessionPrincipal,
            Depends(require_settings_manager),
        ],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AiSettingsResponse:
        try:
            view = get_ai_settings_service().test_provider(
                provider,
                expected_revision=request_body.expected_revision,
                actor=principal.username,
            )
        except AiConnectionTestError as exc:
            raise HTTPException(
                status_code=(
                    status.HTTP_503_SERVICE_UNAVAILABLE
                    if exc.retryable
                    else status.HTTP_422_UNPROCESSABLE_CONTENT
                ),
                detail=str(exc),
            ) from exc
        except (
            AiProviderNotReadyError,
            AiSettingsConfigurationError,
            AiSettingsConflictError,
            AiSettingsPersistenceError,
            AiSettingsValidationError,
        ) as exc:
            raise translate_ai_settings_error(exc) from exc
        return AiSettingsResponse.from_view(view)

    @application.post(
        "/api/v1/settings/ai/providers/{provider}/activate",
        response_model=AiSettingsResponse,
    )
    def activate_ai_provider(
        provider: ModelProvider,
        request_body: AiRevisionRequest,
        principal: Annotated[
            SessionPrincipal,
            Depends(require_settings_manager),
        ],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AiSettingsResponse:
        try:
            view = get_ai_settings_service().activate_provider(
                provider,
                expected_revision=request_body.expected_revision,
                actor=principal.username,
            )
        except (
            AiProviderNotReadyError,
            AiSettingsConfigurationError,
            AiSettingsConflictError,
            AiSettingsPersistenceError,
        ) as exc:
            raise translate_ai_settings_error(exc) from exc
        return AiSettingsResponse.from_view(view)

    @application.put(
        "/api/v1/settings/ai/review-policy",
        response_model=AiSettingsResponse,
    )
    def update_review_policy(
        request_body: ReviewPolicyUpdateRequest,
        principal: Annotated[
            SessionPrincipal,
            Depends(require_settings_manager),
        ],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AiSettingsResponse:
        service = get_ai_settings_service()
        budget_fields = {
            "max_model_http_calls",
            "max_model_input_tokens",
            "max_model_output_tokens",
            "max_model_cost_microusd",
            "max_model_duration_seconds",
        }
        missing_budget_fields = budget_fields.difference(request_body.model_fields_set)
        needs_compatibility_snapshot = bool(missing_budget_fields) or any(
            getattr(request_body, field) is None
            for field in budget_fields.difference({"max_model_cost_microusd"})
        )
        try:
            current = service.get() if needs_compatibility_snapshot else None
            max_model_http_calls = request_body.max_model_http_calls
            max_model_input_tokens = request_body.max_model_input_tokens
            max_model_output_tokens = request_body.max_model_output_tokens
            max_model_duration_seconds = request_body.max_model_duration_seconds
            if current is not None:
                max_model_http_calls = (
                    max_model_http_calls
                    if max_model_http_calls is not None
                    else current.max_model_http_calls
                )
                max_model_input_tokens = (
                    max_model_input_tokens
                    if max_model_input_tokens is not None
                    else current.max_model_input_tokens
                )
                max_model_output_tokens = (
                    max_model_output_tokens
                    if max_model_output_tokens is not None
                    else current.max_model_output_tokens
                )
                max_model_duration_seconds = (
                    max_model_duration_seconds
                    if max_model_duration_seconds is not None
                    else current.max_model_duration_seconds
                )
            if (
                max_model_http_calls is None
                or max_model_input_tokens is None
                or max_model_output_tokens is None
                or max_model_duration_seconds is None
            ):
                raise AiSettingsValidationError("模型预算字段不能为空")
            max_model_cost_microusd = request_body.max_model_cost_microusd
            if (
                current is not None
                and "max_model_cost_microusd" not in request_body.model_fields_set
            ):
                max_model_cost_microusd = current.max_model_cost_microusd
            view = service.update_review_policy(
                ReviewPolicyDraft(
                    max_units=request_body.max_units,
                    max_scope_depth=request_body.max_scope_depth,
                    max_unit_input_bytes=request_body.max_unit_input_bytes,
                    max_total_input_bytes=request_body.max_total_input_bytes,
                    max_model_http_calls=max_model_http_calls,
                    max_model_input_tokens=max_model_input_tokens,
                    max_model_output_tokens=max_model_output_tokens,
                    max_model_cost_microusd=max_model_cost_microusd,
                    max_model_duration_seconds=max_model_duration_seconds,
                ),
                expected_revision=request_body.expected_revision,
                actor=principal.username,
            )
        except (
            AiSettingsConfigurationError,
            AiSettingsConflictError,
            AiSettingsPersistenceError,
            AiSettingsValidationError,
        ) as exc:
            raise translate_ai_settings_error(exc) from exc
        return AiSettingsResponse.from_view(view)

    @application.get(
        "/api/v1/settings/audits",
        response_model=ConfigurationAuditListResponse,
    )
    def list_configuration_audits(
        _: Annotated[SessionPrincipal, Depends(require_settings_manager)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> ConfigurationAuditListResponse:
        try:
            audits = get_ai_settings_service().audits(limit)
        except AiSettingsPersistenceError as exc:
            raise translate_ai_settings_error(exc) from exc
        return ConfigurationAuditListResponse(
            items=tuple(
                ConfigurationAuditResponse.from_view(item) for item in audits
            )
        )
