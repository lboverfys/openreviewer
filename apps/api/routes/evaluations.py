"""评测工作台：复用审查查看/裁决权限与同源保护。"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from domain.evaluation_workbench import (
    EvaluationArchive,
    EvaluationAuditView,
    EvaluationCaseDetail,
    EvaluationCaseView,
    EvaluationChange,
    EvaluationComparisonReport,
    EvaluationConflictError,
    EvaluationDatasetCreate,
    EvaluationDatasetView,
    EvaluationFindingView,
    EvaluationImportResult,
    EvaluationNotFoundError,
    EvaluationRevision,
    EvaluationRunOption,
    EvaluationSplit,
    EvaluationVariant,
    FindingReviewWrite,
    ObservationDetail,
    ObservationImport,
    ObservationReplace,
    ReferenceReviewWrite,
    ReferenceUpdate,
)
from domain.pagination import CursorPage
from services.auth import SessionPrincipal
from services.evaluation_workbench import EvaluationWorkbench


def _guard[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except EvaluationNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (EvaluationConflictError, IntegrityError) as exc:
        detail = str(exc) if isinstance(exc, EvaluationConflictError) else "记录已存在或已变化，请刷新后重试"
        raise HTTPException(409, detail) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "评测数据暂时不可用，请稍后重试") from exc


def register_evaluation_routes(
    application: FastAPI, *,
    get_service: Callable[[], EvaluationWorkbench],
    require_viewer: Callable[..., SessionPrincipal],
    require_editor: Callable[..., SessionPrincipal],
    require_same_origin: Callable[..., None],
) -> None:
    @application.get("/api/v1/evaluations/sources", response_model=CursorPage[EvaluationRunOption])
    def sources(
        principal: Annotated[SessionPrincipal, Depends(require_viewer)],
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        dataset_id: str | None = None, case_id: str | None = None,
    ):
        return _guard(lambda: get_service().sources(principal.resource_scope, limit=limit,
            cursor=cursor, dataset_id=dataset_id, case_id=case_id))

    @application.get("/api/v1/evaluations/datasets", response_model=CursorPage[EvaluationDatasetView])
    def datasets(
        principal: Annotated[SessionPrincipal, Depends(require_viewer)],
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        include_archived: bool = False,
    ):
        return _guard(lambda: get_service().datasets(principal.resource_scope, limit=limit,
            cursor=cursor, include_archived=include_archived))

    @application.post("/api/v1/evaluations/datasets", response_model=EvaluationDatasetView, status_code=201)
    def create_dataset(
        body: EvaluationDatasetCreate,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
        principal: Annotated[SessionPrincipal, Depends(require_editor)],
        _: Annotated[None, Depends(require_same_origin)],
    ):
        if not idempotency_key.strip():
            raise HTTPException(422, "请求标识不能为空")
        return _guard(lambda: get_service().create_dataset(body, idempotency_key.strip(),
            principal.username, principal.resource_scope))

    @application.get("/api/v1/evaluations/datasets/{dataset_id}", response_model=EvaluationDatasetView)
    def dataset(dataset_id: str, principal: Annotated[SessionPrincipal, Depends(require_viewer)]):
        return _guard(lambda: get_service().dataset(dataset_id, principal.resource_scope))

    @application.post("/api/v1/evaluations/datasets/{dataset_id}/archive", response_model=EvaluationDatasetView)
    def archive(
        dataset_id: str, body: EvaluationArchive,
        principal: Annotated[SessionPrincipal, Depends(require_editor)],
        _: Annotated[None, Depends(require_same_origin)],
    ):
        return _guard(lambda: get_service().archive(dataset_id, body, principal.username, principal.resource_scope))

    @application.get("/api/v1/evaluations/datasets/{dataset_id}/cases", response_model=CursorPage[EvaluationCaseView])
    def cases(
        dataset_id: str, principal: Annotated[SessionPrincipal, Depends(require_viewer)],
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        split: EvaluationSplit | None = None,
    ):
        return _guard(lambda: get_service().cases(dataset_id, principal.resource_scope,
            limit=limit, cursor=cursor, split=split))

    @application.get("/api/v1/evaluations/datasets/{dataset_id}/audits", response_model=CursorPage[EvaluationAuditView])
    def audits(
        dataset_id: str, principal: Annotated[SessionPrincipal, Depends(require_viewer)],
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ):
        return _guard(lambda: get_service().audits(dataset_id, principal.resource_scope, limit=limit, cursor=cursor))

    @application.post("/api/v1/evaluations/datasets/{dataset_id}/observations", response_model=EvaluationImportResult)
    def import_observations(
        dataset_id: str, body: ObservationImport,
        principal: Annotated[SessionPrincipal, Depends(require_editor)],
        _: Annotated[None, Depends(require_same_origin)],
    ):
        return _guard(lambda: get_service().import_observations(dataset_id, body, principal.username, principal.resource_scope))

    @application.get("/api/v1/evaluations/datasets/{dataset_id}/report", response_model=EvaluationComparisonReport)
    def report(
        dataset_id: str, principal: Annotated[SessionPrincipal, Depends(require_viewer)],
        split: EvaluationSplit = "validation",
    ):
        return _guard(lambda: get_service().report(dataset_id, principal.resource_scope, split))

    @application.get("/api/v1/evaluations/cases/{case_id}", response_model=EvaluationCaseDetail)
    def case_detail(case_id: str, principal: Annotated[SessionPrincipal, Depends(require_viewer)]):
        return _guard(lambda: get_service().case(case_id, principal.resource_scope))

    @application.put("/api/v1/evaluations/cases/{case_id}/reference", response_model=EvaluationCaseDetail)
    def update_reference(
        case_id: str, body: ReferenceUpdate,
        principal: Annotated[SessionPrincipal, Depends(require_editor)],
        _: Annotated[None, Depends(require_same_origin)],
    ):
        return _guard(lambda: get_service().update_reference(case_id, body, principal.username, principal.resource_scope))

    @application.post("/api/v1/evaluations/cases/{case_id}/reference/reviews", response_model=EvaluationCaseDetail)
    def review_reference(
        case_id: str, body: ReferenceReviewWrite,
        principal: Annotated[SessionPrincipal, Depends(require_editor)],
        _: Annotated[None, Depends(require_same_origin)],
    ):
        return _guard(lambda: get_service().review_reference(case_id, body, principal.username, principal.resource_scope))

    @application.get("/api/v1/evaluations/cases/{case_id}/observations/{variant}", response_model=ObservationDetail)
    def observation(
        case_id: str, variant: EvaluationVariant,
        principal: Annotated[SessionPrincipal, Depends(require_viewer)],
    ):
        return _guard(lambda: get_service().observation(case_id, variant, principal.resource_scope))

    @application.get("/api/v1/evaluations/cases/{case_id}/observations/{variant}/findings",
                     response_model=CursorPage[EvaluationFindingView])
    def findings(
        case_id: str, variant: EvaluationVariant,
        principal: Annotated[SessionPrincipal, Depends(require_viewer)],
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ):
        return _guard(lambda: get_service().findings(case_id, variant, principal.resource_scope, limit=limit, cursor=cursor))

    @application.put("/api/v1/evaluations/cases/{case_id}/observations/{variant}/findings/{finding_id}/review",
                     response_model=ObservationDetail)
    def review_finding(
        case_id: str, variant: EvaluationVariant, finding_id: str, body: FindingReviewWrite,
        principal: Annotated[SessionPrincipal, Depends(require_editor)],
        _: Annotated[None, Depends(require_same_origin)],
    ):
        return _guard(lambda: get_service().review_finding(case_id, variant, finding_id,
            body, principal.username, principal.resource_scope))

    @application.get("/api/v1/evaluations/cases/{case_id}/observations/{variant}/changes",
                     response_model=CursorPage[EvaluationChange])
    def changes(
        case_id: str, variant: EvaluationVariant,
        principal: Annotated[SessionPrincipal, Depends(require_viewer)],
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ):
        return _guard(lambda: get_service().changes(case_id, variant, principal.resource_scope, limit=limit, cursor=cursor))

    @application.post("/api/v1/evaluations/cases/{case_id}/observations/{variant}/submit",
                      response_model=ObservationDetail)
    def submit_review(
        case_id: str, variant: EvaluationVariant, body: EvaluationRevision,
        principal: Annotated[SessionPrincipal, Depends(require_editor)],
        _: Annotated[None, Depends(require_same_origin)],
    ):
        return _guard(lambda: get_service().submit_review(case_id, variant, body.expected_revision,
            principal.username, principal.resource_scope))

    @application.put("/api/v1/evaluations/cases/{case_id}/observations/{variant}/source",
                     response_model=ObservationDetail)
    def replace_observation(
        case_id: str, variant: EvaluationVariant, body: ObservationReplace,
        principal: Annotated[SessionPrincipal, Depends(require_editor)],
        _: Annotated[None, Depends(require_same_origin)],
    ):
        return _guard(lambda: get_service().replace_observation(case_id, variant, body,
            principal.username, principal.resource_scope))
