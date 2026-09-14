"""代码索引、混合检索设置、执行记录与评测结果。"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from domain.pagination import CursorPage
from domain.retrieval import (
    IndexTarget,
    IndexView,
    RetrievalComparisonRequest,
    RetrievalEvaluationCase,
    RetrievalEvaluationReport,
    RetrievalOperations,
    RetrievalSettings,
    RetrievalSettingsView,
    RetrievalTrace,
    SearchHistoryItem,
    SearchQuery,
)
from persistence.retrieval_runtime import RetrievalRuntimeRepository
from services.ai_settings import AiSettingsConfigurationError
from services.auth import SessionPrincipal
from services.retrieval import HybridRetrievalService
from services.retrieval_gateway import provider_lane_key
from services.retrieval_providers import RetrievalError


class RetrievalSettingsUpdate(BaseModel):
    settings: RetrievalSettings
    expected_revision: int = Field(ge=0)
    api_key: str | None = Field(default=None, max_length=65536, repr=False, json_schema_extra={"writeOnly": True})


class CodeIndexCreate(BaseModel):
    review_run_id: str = Field(min_length=1, max_length=100)


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(404, "索引或审查记录不存在")
    if isinstance(exc, ValueError):
        return HTTPException(422, str(exc))
    if isinstance(exc, RetrievalError):
        return HTTPException(503 if exc.retryable else 409, str(exc))
    return HTTPException(503, "检索服务暂时不可用")


def register_retrieval_routes(
    application: FastAPI, *,
    get_service: Callable[[], HybridRetrievalService],
    require_manager: Callable[..., SessionPrincipal],
    require_viewer: Callable[..., SessionPrincipal],
    require_same_origin: Callable[..., None],
) -> None:
    errors = (RetrievalError, LookupError, ValueError, SQLAlchemyError, AiSettingsConfigurationError)

    @application.get("/api/v1/retrieval/targets", response_model=CursorPage[IndexTarget])
    def targets(
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> CursorPage[IndexTarget]:
        try:
            return get_service().repository.target_page(principal.resource_scope, limit, cursor)
        except errors as exc:
            raise _translate(exc) from exc

    @application.get("/api/v1/retrieval/operations", response_model=RetrievalOperations)
    def operations(principal: Annotated[SessionPrincipal, Depends(require_manager)]) -> RetrievalOperations:
        try:
            service = get_service()
            result = service.repository.operations(principal.resource_scope)
            view, key = service.settings.runtime()
            busy, blocked = RetrievalRuntimeRepository(service.repository.sessions).circuit_state(provider_lane_key(view.settings, key)) if key else (False, False)
            return result.model_copy(update={"provider_busy": busy, "circuit_open": blocked})
        except errors as exc:
            raise _translate(exc) from exc

    @application.get("/api/v1/retrieval/settings", response_model=RetrievalSettingsView)
    def settings(_: Annotated[SessionPrincipal, Depends(require_manager)]) -> RetrievalSettingsView:
        try:
            return get_service().settings.get()
        except errors as exc:
            raise _translate(exc) from exc

    @application.put("/api/v1/retrieval/settings", response_model=RetrievalSettingsView)
    def update_settings(
        body: RetrievalSettingsUpdate,
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> RetrievalSettingsView:
        try:
            return get_service().settings.update(body.settings, body.expected_revision, principal.username, body.api_key)
        except errors as exc:
            raise _translate(exc) from exc

    @application.post("/api/v1/retrieval/settings/test", response_model=RetrievalSettingsView)
    def test_settings(
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> RetrievalSettingsView:
        try:
            return get_service().test_connection()
        except errors as exc:
            raise _translate(exc) from exc

    @application.get("/api/v1/retrieval/indexes", response_model=CursorPage[IndexView])
    def indexes(
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> CursorPage[IndexView]:
        try:
            return get_service().repository.index_page(principal.resource_scope, limit, cursor)
        except errors as exc:
            raise _translate(exc) from exc

    @application.post("/api/v1/retrieval/indexes", response_model=IndexView, status_code=202)
    def create_index(
        body: CodeIndexCreate,
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> IndexView:
        try:
            service = get_service()
            target = service.repository.target_for_review(body.review_run_id, principal.resource_scope)
            index_id = service.enqueue(target)
            return service.repository.get(index_id, principal.resource_scope)
        except errors as exc:
            raise _translate(exc) from exc

    @application.get("/api/v1/retrieval/indexes/{index_id}", response_model=IndexView)
    def index(index_id: str, principal: Annotated[SessionPrincipal, Depends(require_manager)]) -> IndexView:
        try:
            return get_service().repository.get(index_id, principal.resource_scope)
        except errors as exc:
            raise _translate(exc) from exc

    @application.post("/api/v1/retrieval/indexes/{index_id}/retry", status_code=202)
    def retry_index(
        index_id: str, principal: Annotated[SessionPrincipal, Depends(require_manager)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> dict[str, str]:
        try:
            get_service().retry_index(index_id, principal.resource_scope)
            return {"status": "queued"}
        except errors as exc:
            raise _translate(exc) from exc

    @application.post("/api/v1/retrieval/indexes/{index_id}/search", response_model=RetrievalTrace)
    def search(
        index_id: str, body: SearchQuery,
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> RetrievalTrace:
        try:
            return get_service().search(index_id, body, principal.resource_scope)
        except errors as exc:
            raise _translate(exc) from exc

    @application.get("/api/v1/retrieval/indexes/{index_id}/history", response_model=CursorPage[SearchHistoryItem])
    def history(index_id: str, principal: Annotated[SessionPrincipal, Depends(require_manager)],
                query: Annotated[str, Query(max_length=4000)] = "",
                strategy: Annotated[str, Query(max_length=32)] = "",
                limit: Annotated[int, Query(ge=1, le=50)] = 10,
                cursor: Annotated[str | None, Query(max_length=512)] = None) -> CursorPage[SearchHistoryItem]:
        try:
            return get_service().repository.search_history(index_id, principal.resource_scope,
                query=query, strategy=strategy, limit=limit, cursor=cursor)
        except errors as exc:
            raise _translate(exc) from exc

    @application.get("/api/v1/retrieval/history/{identifier}", response_model=RetrievalTrace)
    def history_detail(identifier: str, principal: Annotated[SessionPrincipal, Depends(require_manager)]) -> RetrievalTrace:
        try:
            return get_service().repository.search_record(identifier, principal.resource_scope)
        except errors as exc:
            raise _translate(exc) from exc

    @application.post("/api/v1/retrieval/indexes/{index_id}/compare", response_model=RetrievalEvaluationReport)
    def compare(index_id: str, body: RetrievalComparisonRequest,
                principal: Annotated[SessionPrincipal, Depends(require_manager)],
                _: Annotated[None, Depends(require_same_origin)]) -> RetrievalEvaluationReport:
        try:
            service = get_service()
            service.repository.get(index_id, principal.resource_scope)
            known = service.repository.match_symbols(index_id, body.relevant_symbols)
            if not set(body.relevant_symbols) <= known:
                raise ValueError("应该找到的代码名称不在这个提交的索引中，请核对完整符号名")
            return service.evaluate(index_id, (RetrievalEvaluationCase(id="manual-reference",
                query=body.query, relevant_symbols=body.relevant_symbols),),
                dataset_version="网页核对：" + body.query[:80], annotation_source="single_reviewer",
                strategies=body.strategies, k=body.k, scope=principal.resource_scope)
        except errors as exc:
            raise _translate(exc) from exc

    @application.post("/api/v1/retrieval/indexes/{index_id}/enrich", status_code=202)
    def enrich(index_id: str, principal: Annotated[SessionPrincipal, Depends(require_manager)],
               _: Annotated[None, Depends(require_same_origin)]) -> dict[str, str]:
        try:
            get_service().retry_index(index_id, principal.resource_scope, include_vectors=True)
            return {"status": "queued"}
        except errors as exc:
            raise _translate(exc) from exc

    @application.get("/api/v1/reviews/{review_run_id}/retrieval", response_model=tuple[RetrievalTrace, ...])
    def review_retrieval(
        review_run_id: str, principal: Annotated[SessionPrincipal, Depends(require_viewer)],
    ) -> tuple[RetrievalTrace, ...]:
        try:
            service = get_service()
            service.repository.target_for_review(review_run_id, principal.resource_scope)
            return service.repository.traces(review_run_id, principal.resource_scope)
        except errors as exc:
            raise _translate(exc) from exc

    @application.get("/api/v1/retrieval/evaluations", response_model=CursorPage[RetrievalEvaluationReport])
    def evaluations(
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        index_id: Annotated[str | None, Query(max_length=64)] = None,
    ) -> CursorPage[RetrievalEvaluationReport]:
        try:
            return get_service().repository.evaluation_page(principal.resource_scope, limit, cursor, index_id)
        except errors as exc:
            raise _translate(exc) from exc
