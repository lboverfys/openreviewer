"""团队成员和仓库策略仅由设置管理员维护。"""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query

from domain.pagination import CursorPage
from domain.security import SafeApplicationError
from services.auth import SessionPrincipal
from services.repository_connection import RepositoryConnector
from services.team import (
    TEAM_ERRORS,
    MemberPage,
    MemberView,
    MemberWrite,
    RepositoryView,
    RepositoryWrite,
    TeamAuditView,
    TeamService,
    translate_team_error,
)


def register_team_routes(
    application: FastAPI,
    *,
    get_service: Callable[[], TeamService],
    get_connector: Callable[[], RepositoryConnector],
    require_manager: Callable[..., SessionPrincipal],
    require_same_origin: Callable[..., None],
) -> None:
    @application.get("/api/v1/team/github/installations")
    def installations(_: Annotated[SessionPrincipal, Depends(require_manager)],
                      page: Annotated[int, Query(ge=1, le=1000)] = 1) -> dict[str, Any]:
        try:
            return get_connector().installations(page)
        except (SafeApplicationError, ValueError) as exc:
            raise HTTPException(503, "现有 App 的授权列表暂时无法读取，请稍后重试") from exc

    @application.get("/api/v1/team/github/installations/{installation_id}/repositories")
    def authorized_repositories(installation_id: int,
            _: Annotated[SessionPrincipal, Depends(require_manager)],
            page: Annotated[int, Query(ge=1, le=1000)] = 1) -> dict[str, Any]:
        try:
            return get_connector().repositories(installation_id, page)
        except (SafeApplicationError, ValueError) as exc:
            raise HTTPException(503, "无法读取仓库授权，请检查现有 App 权限后重试") from exc

    @application.get("/api/v1/team/members", response_model=MemberPage)
    def list_members(
        _: Annotated[SessionPrincipal, Depends(require_manager)],
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> MemberPage:
        try:
            return get_service().members(limit, cursor)
        except TEAM_ERRORS as exc:
            raise HTTPException(*translate_team_error(exc)) from exc

    @application.put("/api/v1/team/members/{username}", response_model=MemberView)
    def save_member(
        username: str, body: MemberWrite,
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> MemberView:
        try:
            return get_service().save_member(username, body, principal.username)
        except TEAM_ERRORS as exc:
            raise HTTPException(*translate_team_error(exc)) from exc

    @application.get("/api/v1/team/repositories", response_model=CursorPage[RepositoryView])
    def list_repositories(
        _: Annotated[SessionPrincipal, Depends(require_manager)],
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        repository: Annotated[str | None, Query(max_length=255)] = None,
    ) -> CursorPage[RepositoryView]:
        try:
            return get_service().repositories(limit, cursor, repository)
        except TEAM_ERRORS as exc:
            raise HTTPException(*translate_team_error(exc)) from exc

    @application.post(
        "/api/v1/team/repositories", response_model=RepositoryView, status_code=201,
    )
    def create_repository(
        body: RepositoryWrite,
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> RepositoryView:
        try:
            return get_service().save_repository(body, principal.username)
        except TEAM_ERRORS as exc:
            raise HTTPException(*translate_team_error(exc)) from exc

    @application.put(
        "/api/v1/team/repositories/{repository_id}", response_model=RepositoryView,
    )
    def save_repository(
        repository_id: str, body: RepositoryWrite,
        principal: Annotated[SessionPrincipal, Depends(require_manager)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> RepositoryView:
        try:
            return get_service().save_repository(body, principal.username, repository_id)
        except TEAM_ERRORS as exc:
            raise HTTPException(*translate_team_error(exc)) from exc

    @application.post("/api/v1/team/repositories/{repository_id}/check", response_model=RepositoryView)
    def check_connection(repository_id: str,
            principal: Annotated[SessionPrincipal, Depends(require_manager)],
            _: Annotated[None, Depends(require_same_origin)]) -> RepositoryView:
        try:
            return get_service().check_connection(repository_id, principal.username)
        except TEAM_ERRORS as exc:
            raise HTTPException(*translate_team_error(exc)) from exc

    @application.get("/api/v1/team/audits", response_model=CursorPage[TeamAuditView])
    def list_audits(
        _: Annotated[SessionPrincipal, Depends(require_manager)],
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> CursorPage[TeamAuditView]:
        try:
            return get_service().audits(limit, cursor)
        except TEAM_ERRORS as exc:
            raise HTTPException(*translate_team_error(exc)) from exc
