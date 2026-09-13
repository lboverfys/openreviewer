"""团队待办、用量、审查方案和诊断接口。"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from apps.api.platform_services import PlatformServices
from domain.evaluation_workbench import EvaluationNotFoundError
from domain.pagination import CursorPage
from domain.platform import (
    ApprovalTodo,
    DiagnosticReport,
    KnowledgeProposalView,
    KnowledgeProposalWrite,
    PlatformAudit,
    PlatformConflictError,
    PlatformNotFoundError,
    ProfileActivate,
    ProfileCreate,
    ProfileQuality,
    ProfileView,
    UsageBreakdown,
    UsageMonth,
    UsageRequest,
    WorkItemCreate,
    WorkItemUpdate,
    WorkItemView,
    WorkStatus,
)
from domain.project_evidence import ProjectEvidence, project_evidence
from domain.static_analysis import (
    StaticFindingView,
    StaticReportUpload,
    StaticReportView,
)
from domain.workers import WorkerListState, WorkerNodePage
from persistence.evaluation_reports import comparison_report
from persistence.static_analysis import StaticAnalysisRepository
from persistence.workers import WorkerRepository
from services.ai_settings import AiSettingsError
from services.auth import SessionPrincipal
from services.operations import OperationsSettings
from services.rag import KnowledgePersistenceError
from services.rbac import Permission, has_permission
from services.review_learning import ReviewLearningService
from services.review_profiles import ReviewProfileService


def _guard[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except (PlatformNotFoundError, EvaluationNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except (PlatformConflictError, IntegrityError) as exc:
        raise HTTPException(
            409,
            str(exc)
            if isinstance(exc, PlatformConflictError)
            else "记录已变化，请刷新后重试",
        ) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (SQLAlchemyError, AiSettingsError, KnowledgePersistenceError) as exc:
        raise HTTPException(503, "团队管理暂时不可用，请稍后重试") from exc


def register_platform_routes(
    application: FastAPI,
    *,
    get_service: Callable[[], PlatformServices],
    get_profile_creator: Callable[[], ReviewProfileService],
    get_learning: Callable[[], ReviewLearningService],
    require_knowledge_manager: Callable[..., SessionPrincipal],
    require_viewer: Callable[..., SessionPrincipal],
    require_editor: Callable[..., SessionPrincipal],
    require_manager: Callable[..., SessionPrincipal],
    require_same_origin: Callable[..., None],
) -> None:
    def static_service():
        return StaticAnalysisRepository(get_service().profiles.sessions)

    @application.get("/api/v1/platform/workers", response_model=WorkerNodePage)
    def worker_nodes(
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        state: WorkerListState = "online",
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=1536)] = None,
    ):
        return _guard(lambda: WorkerRepository(get_service().profiles.sessions).page(
            principal.resource_scope, state=state, limit=limit, cursor=cursor,
            retention_days=OperationsSettings.from_environment().worker_heartbeat_retention.days,
        ))

    @application.get("/api/v1/platform/evidence", response_model=ProjectEvidence)
    def evidence_report(
        principal: Annotated[SessionPrincipal, Depends(require_viewer)],
        dataset_id: Annotated[str | None, Query(max_length=36)] = None,
    ):
        def build():
            with get_service().profiles.sessions() as session:
                report = comparison_report(session, dataset_id, principal.resource_scope, "validation") if dataset_id else None
                return project_evidence(report)
        return _guard(build)

    @application.get("/api/v1/platform/reviews/{run_id}/static-report", response_model=StaticReportView | None)
    def static_report(run_id: str, principal: Annotated[SessionPrincipal, Depends(require_viewer)]):
        return _guard(lambda: static_service().get(run_id, principal.resource_scope))

    @application.post("/api/v1/platform/reviews/{run_id}/static-report", response_model=StaticReportView)
    def import_static_report(run_id: str, body: StaticReportUpload,
        principal: Annotated[SessionPrincipal, Depends(require_editor)],
        _: Annotated[None, Depends(require_same_origin)]):
        return _guard(lambda: static_service().upload(run_id, body, principal.username, principal.resource_scope))

    @application.get("/api/v1/platform/reviews/{run_id}/static-findings", response_model=CursorPage[StaticFindingView])
    def static_findings(run_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_viewer)],
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None):
        return _guard(lambda: static_service().findings(run_id, principal.resource_scope, limit=limit, cursor=cursor))

    @application.get("/api/v1/platform/usage", response_model=CursorPage[UsageMonth])
    def usage(
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        month: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ):
        return _guard(
            lambda: get_service().usage.months(
                principal.resource_scope,
                month or datetime.now(UTC).strftime("%Y-%m"),
                limit=limit,
                cursor=cursor,
            )
        )

    @application.get(
        "/api/v1/platform/usage/{month_id}/requests",
        response_model=CursorPage[UsageRequest],
    )
    def requests(
        month_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ):
        return _guard(
            lambda: get_service().usage.requests(
                principal.resource_scope, month_id, limit=limit, cursor=cursor
            )
        )

    @application.get(
        "/api/v1/platform/usage/{month_id}/breakdown",
        response_model=tuple[UsageBreakdown, ...],
    )
    def breakdown(
        month_id: str, principal: Annotated[SessionPrincipal, Depends(require_manager)]
    ):
        return _guard(
            lambda: get_service().usage.breakdown(principal.resource_scope, month_id)
        )

    @application.get(
        "/api/v1/platform/work-items", response_model=CursorPage[WorkItemView]
    )
    def work_items(
        principal: Annotated[SessionPrincipal, Depends(require_viewer)],
        mine: bool = True,
        status: WorkStatus | None = None,
        overdue: bool = False,
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ):
        return _guard(
            lambda: get_service().work_items.list(
                principal.resource_scope,
                assignee=principal.username if mine else None,
                status=status,
                overdue=overdue,
                limit=limit,
                cursor=cursor,
            )
        )

    @application.post("/api/v1/platform/work-items", response_model=WorkItemView)
    def create_work(
        body: WorkItemCreate,
        principal: Annotated[SessionPrincipal, Depends(require_editor)],
        _: Annotated[None, Depends(require_same_origin)],
    ):
        return _guard(
            lambda: get_service().work_items.create(
                body, principal.username, principal.resource_scope
            )
        )

    @application.put(
        "/api/v1/platform/work-items/{identifier}", response_model=WorkItemView
    )
    def update_work(
        identifier: str,
        body: WorkItemUpdate,
        principal: Annotated[SessionPrincipal, Depends(require_editor)],
        _: Annotated[None, Depends(require_same_origin)],
    ):
        return _guard(
            lambda: get_service().work_items.update(
                identifier, body, principal.username, principal.resource_scope
            )
        )

    @application.get(
        "/api/v1/platform/approvals", response_model=CursorPage[ApprovalTodo]
    )
    def approvals(
        principal: Annotated[SessionPrincipal, Depends(require_viewer)],
        mine: bool = True,
        overdue: bool = False,
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ):
        if not has_permission(principal.role, Permission.APPROVE_REVIEWS):
            raise HTTPException(403, "当前账号没有审批权限")
        return _guard(
            lambda: get_service().work_items.approvals(
                principal.resource_scope,
                principal.username,
                mine=mine,
                overdue=overdue,
                limit=limit,
                cursor=cursor,
            )
        )

    @application.post(
        "/api/v1/platform/work-items/{identifier}/knowledge",
        response_model=KnowledgeProposalView,
    )
    def propose_knowledge(
        identifier: str,
        body: KnowledgeProposalWrite,
        principal: Annotated[SessionPrincipal, Depends(require_knowledge_manager)],
        _: Annotated[None, Depends(require_same_origin)],
    ):
        return _guard(
            lambda: get_learning().propose(
                identifier, body, principal.username, principal.resource_scope
            )
        )

    @application.get(
        "/api/v1/platform/profiles", response_model=CursorPage[ProfileView]
    )
    def profiles(
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        repository: Annotated[str | None, Query(max_length=255)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ):
        return _guard(
            lambda: get_service().profiles.list(
                principal.resource_scope,
                repository=repository,
                limit=limit,
                cursor=cursor,
            )
        )

    @application.post(
        "/api/v1/platform/profiles", response_model=ProfileView, status_code=201
    )
    def create_profile(
        body: ProfileCreate,
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        _: Annotated[None, Depends(require_same_origin)],
    ):
        return _guard(
            lambda: get_profile_creator().create(
                body, principal.username, principal.resource_scope
            )
        )

    @application.get("/api/v1/platform/profiles/{identifier}/quality", response_model=ProfileQuality)
    def profile_quality(
        identifier: str,
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        dataset_id: Annotated[str | None, Query(max_length=36)] = None,
    ):
        return _guard(lambda: get_service().profiles.quality(identifier, principal.resource_scope, dataset_id))

    @application.post(
        "/api/v1/platform/profiles/{identifier}/activate", response_model=dict[str, int]
    )
    def activate_profile(
        identifier: str,
        body: ProfileActivate,
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        _: Annotated[None, Depends(require_same_origin)],
    ):
        revision = _guard(
            lambda: get_service().profiles.activate(
                identifier,
                body.expected_repository_revision,
                principal.username,
                principal.resource_scope,
                dataset_id=body.evaluation_dataset_id, evidence_token=body.evidence_token,
                reason=body.reason,
            )
        )
        return {"revision": revision}

    @application.get("/api/v1/platform/diagnostics", response_model=DiagnosticReport)
    def diagnostics(
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        days: Annotated[int, Query(ge=1, le=30)] = 7,
    ):
        return _guard(
            lambda: get_service().queries.diagnostics(
                principal.resource_scope, days=days
            )
        )

    @application.get(
        "/api/v1/platform/audits", response_model=CursorPage[PlatformAudit]
    )
    def audits(
        principal: Annotated[SessionPrincipal, Depends(require_viewer)],
        object_id: Annotated[str | None, Query(max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ):
        return _guard(
            lambda: get_service().queries.audits(
                principal.resource_scope,
                object_id=object_id,
                limit=limit,
                cursor=cursor,
            )
        )
