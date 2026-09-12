"""审查详情、人工动作与任务创建路由。"""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status

from apps.api.schemas import (
    ReviewAcceptedResponse,
    ReviewActionRequest,
    ReviewActionResponse,
    ReviewChangeTokenResponse,
    ReviewDetailsResponse,
    ReviewEventResponse,
    ReviewFindingDecisionRequest,
    ReviewFindingResponse,
)
from domain.models import ReviewRequest
from domain.pagination import CursorPage
from domain.review_progress import BatchSnapshot
from services.auth import SessionPrincipal
from services.github_access import GitHubAccessPolicy
from services.rbac import Permission
from services.review_management import (
    FindingNotFoundError,
    ReviewAction,
    ReviewActionConflictError,
    ReviewIdentitySyncConflictError,
    ReviewIdentitySyncUnavailableError,
    ReviewManagementPersistenceError,
    ReviewManagementService,
    ReviewNotFoundError,
    ReviewPublishUnavailableError,
)
from services.review_quota import ReviewQuotaExceededError
from services.reviews import (
    IdempotencyConflictError,
    ReviewPersistenceError,
    ReviewService,
)


def register_review_routes(
    application: FastAPI,
    *,
    get_review_management_service: Callable[[], ReviewManagementService],
    get_review_service: Callable[[], ReviewService],
    get_github_access_policy: Callable[[], GitHubAccessPolicy],
    require_review_viewer: Callable[..., SessionPrincipal],
    require_review_manager: Callable[..., SessionPrincipal],
    require_adjudicator: Callable[..., SessionPrincipal],
    require_same_origin: Callable[..., None],
    ensure_permission: Callable[[SessionPrincipal, Permission], None],
) -> None:
    """注册详情读取、人工控制、Finding 裁决和任务创建端点。"""

    def review_details_response(
        review_run_id: str,
        principal: SessionPrincipal,
        finding_limit: int = 10,
        finding_cursor: str | None = None,
        view: str = "full",
    ) -> ReviewDetailsResponse:
        """读取单条任务详情并统一转换存储异常。"""

        try:
            details = get_review_management_service().details(
                review_run_id,
                finding_limit=finding_limit,
                finding_cursor=finding_cursor,
                scope=principal.resource_scope,
                view=view,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="finding cursor is invalid",
            ) from exc
        except ReviewNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="review task not found",
            ) from exc
        except ReviewManagementPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="review details are temporarily unavailable",
            ) from exc
        return ReviewDetailsResponse.from_details(details)

    @application.get("/api/v1/reviews/{review_run_id}/findings", response_model=CursorPage[ReviewFindingResponse])
    def list_findings(review_run_id: str, principal: Annotated[SessionPrincipal, Depends(require_review_viewer)],
                      limit: Annotated[int, Query(ge=1, le=100)] = 10,
                      cursor: Annotated[str | None, Query(max_length=512)] = None,
                      severity: str | None = None, adjudication_status: str | None = None,
                      q: Annotated[str, Query(max_length=200)] = "") -> CursorPage[ReviewFindingResponse]:
        try:
            page = get_review_management_service().finding_page(review_run_id, limit=limit, cursor=cursor,
                severity=severity, adjudication_status=adjudication_status, query=q.strip(), scope=principal.resource_scope)
        except ReviewNotFoundError as exc:
            raise HTTPException(404, "review task not found") from exc
        except ValueError as exc:
            raise HTTPException(422, "finding cursor is invalid") from exc
        except ReviewManagementPersistenceError as exc:
            raise HTTPException(503, "review findings are temporarily unavailable") from exc
        return CursorPage(items=tuple(ReviewFindingResponse(**{name: getattr(item, name) for name in ReviewFindingResponse.model_fields}) for item in page.items), next_cursor=page.next_cursor)

    @application.get("/api/v1/reviews/{review_run_id}/events", response_model=CursorPage[ReviewEventResponse])
    def list_events(review_run_id: str, principal: Annotated[SessionPrincipal, Depends(require_review_viewer)],
                    limit: Annotated[int, Query(ge=1, le=100)] = 10,
                    cursor: Annotated[str | None, Query(max_length=512)] = None,
                    event_filter: Literal["all", "model", "workflow", "errors"] = "all") -> CursorPage[ReviewEventResponse]:
        try:
            page = get_review_management_service().event_page(review_run_id, limit=limit, cursor=cursor,
                event_filter=event_filter, scope=principal.resource_scope)
        except ReviewNotFoundError as exc:
            raise HTTPException(404, "review task not found") from exc
        except ValueError as exc:
            raise HTTPException(422, "event cursor is invalid") from exc
        except ReviewManagementPersistenceError as exc:
            raise HTTPException(503, "review events are temporarily unavailable") from exc
        return CursorPage(items=tuple(ReviewEventResponse(**{name: getattr(item, name) for name in ReviewEventResponse.model_fields}) for item in page.items), next_cursor=page.next_cursor)

    @application.get(
        "/api/v1/reviews/{review_run_id}/change-token",
        response_model=ReviewChangeTokenResponse,
    )
    def get_review_change_token(
        review_run_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_review_viewer)],
    ) -> ReviewChangeTokenResponse:
        """用一次索引查询返回详情变化令牌，不加载子资源。"""

        try:
            change_token = get_review_management_service().change_token(
                review_run_id,
                scope=principal.resource_scope,
            )
        except ReviewNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="review task not found",
            ) from exc
        except ReviewManagementPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="review change token is temporarily unavailable",
            ) from exc
        return ReviewChangeTokenResponse(change_token=change_token)

    @application.get("/api/v1/reviews/{review_run_id}/batches", response_model=CursorPage[BatchSnapshot])
    def list_batches(review_run_id: str,
                     principal: Annotated[SessionPrincipal, Depends(require_review_viewer)],
                     agent: Literal["security", "convention", "logic", "summary"],
                     after: Annotated[int, Query(ge=0, le=3000)] = 0,
                     limit: Annotated[int, Query(ge=1, le=100)] = 10) -> CursorPage[BatchSnapshot]:
        try:
            return get_review_management_service().batch_page(review_run_id, agent,
                after=after, limit=limit, scope=principal.resource_scope)
        except ReviewNotFoundError as exc:
            raise HTTPException(404, "review task not found") from exc
        except ReviewManagementPersistenceError as exc:
            raise HTTPException(503, "review batches are temporarily unavailable") from exc

    @application.get(
        "/api/v1/reviews/{review_run_id}",
        response_model=ReviewDetailsResponse,
    )
    def get_review_details(
        review_run_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_review_viewer)],
        finding_limit: Annotated[int, Query(ge=1, le=100)] = 10,
        finding_cursor: Annotated[str | None, Query(max_length=512)] = None,
        view: Literal["full", "overview", "findings", "agents"] = "full",
    ) -> ReviewDetailsResponse:
        """返回任务的阶段、模型结果、Finding、CI 和结构化事件日志。"""

        return review_details_response(
            review_run_id,
            principal,
            finding_limit,
            finding_cursor,
            view,
        )

    @application.post(
        "/api/v1/reviews/{review_run_id}/identity/sync",
        response_model=ReviewDetailsResponse,
    )
    def sync_review_identity(
        review_run_id: str,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=200),
        ],
        principal: Annotated[SessionPrincipal, Depends(require_review_manager)],
        __: Annotated[None, Depends(require_same_origin)],
    ) -> ReviewDetailsResponse:
        """从 GitHub 回查并补全历史任务的 PR 作者、链接和分支信息。"""

        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Idempotency-Key must not be blank",
            )
        try:
            details = get_review_management_service().sync_identity(
                review_run_id,
                actor=principal.username,
                request_id=normalized_key,
                loader=application.state.identity_loader,
                scope=principal.resource_scope,
            )
        except ReviewNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="review task not found",
            ) from exc
        except ReviewIdentitySyncUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GitHub PR identity sync is not configured",
            ) from exc
        except ReviewIdentitySyncConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except ReviewManagementPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="review identity sync is temporarily unavailable",
            ) from exc
        return ReviewDetailsResponse.from_details(details)

    @application.post(
        "/api/v1/reviews/{review_run_id}/actions",
        response_model=ReviewActionResponse,
    )
    def apply_review_action(
        review_run_id: str,
        request_body: ReviewActionRequest,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=200),
        ],
        principal: Annotated[SessionPrincipal, Depends(require_review_viewer)],
        __: Annotated[None, Depends(require_same_origin)],
    ) -> ReviewActionResponse:
        """执行可审计的加速、重试、取消或重新审查动作。"""

        action_permission = (
            Permission.APPROVE_REVIEWS
            if request_body.action in {ReviewAction.APPROVE, ReviewAction.REJECT}
            else Permission.PUBLISH_REVIEWS
            if request_body.action is ReviewAction.PUBLISH
            else Permission.MANAGE_REVIEWS
        )
        ensure_permission(principal, action_permission)
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Idempotency-Key must not be blank",
            )
        try:
            management = get_review_management_service()
            new_run_id, task_id, execution_status = management.apply_action(
                review_run_id,
                request_body.action,
                actor=principal.username,
                request_id=normalized_key,
                target_stage=(
                    request_body.target_stage.value
                    if request_body.target_stage is not None
                    else None
                ),
                retry_scope=request_body.retry_scope,
                agent=(request_body.agent.value if request_body.agent is not None else None),
                batch_number=request_body.batch_number,
                state_version=request_body.state_version,
                head_sha=request_body.head_sha,
                scope=principal.resource_scope,
            )
            # ``execution_status`` 是旧队列兼容字段；人工节点（尤其批准后）
            # 的真实状态只存在于固定 DAG 的 workflow_status 中。动作提交后
            # 重新读取一次已提交快照，避免用旧状态集合推断并返回 null。
            workflow_status = management.details(
                new_run_id,
                scope=principal.resource_scope,
            ).stored.workflow_status
        except ReviewNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="review task not found",
            ) from exc
        except ReviewActionConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except ReviewManagementPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="review action is temporarily unavailable",
            ) from exc
        except ReviewPublishUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GitHub publish is not configured or temporarily unavailable",
            ) from exc
        return ReviewActionResponse(
            action=request_body.action,
            review_run_id=new_run_id,
            review_task_id=task_id,
            execution_status=execution_status,
            workflow_status=workflow_status,
        )

    @application.post(
        "/api/v1/reviews/{review_run_id}/findings/{finding_id}",
        response_model=ReviewDetailsResponse,
    )
    def decide_review_finding(
        review_run_id: str,
        finding_id: str,
        request_body: ReviewFindingDecisionRequest,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=200),
        ],
        principal: Annotated[SessionPrincipal, Depends(require_adjudicator)],
        __: Annotated[None, Depends(require_same_origin)],
    ) -> ReviewDetailsResponse:
        """保存 Finding 的“确认问题/忽略”裁决并返回最新详情。"""

        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Idempotency-Key must not be blank",
            )
        try:
            details = get_review_management_service().review_finding(
                review_run_id,
                finding_id,
                request_body.decision,
                actor=principal.username,
                request_id=normalized_key,
                scope=principal.resource_scope,
            )
        except (ReviewNotFoundError, FindingNotFoundError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="review finding not found",
            ) from exc
        except ReviewManagementPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="finding decision is temporarily unavailable",
            ) from exc
        return ReviewDetailsResponse.from_details(details)

    @application.post(
        "/api/v1/reviews",
        response_model=ReviewAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_review(
        request_body: ReviewRequest,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=200,
            ),
        ],
        principal: Annotated[SessionPrincipal, Depends(require_review_manager)],
        __: Annotated[None, Depends(require_same_origin)],
    ) -> ReviewAcceptedResponse:
        """校验幂等键并接受一个异步审查任务。

        请求必须先通过会话和同源检查；仓储层保证运行、任务和 Outbox 事件在同
        一个事务中持久化。相同键重复提交返回原任务，不同内容复用同一键则返回
        409，数据库暂时不可用则返回可安全重试的 503。

        参数：
            request_body: 已通过 Pydantic 严格字段校验的审查请求。
            idempotency_key: HTTP ``Idempotency-Key``，长度 1 到 200；函数会再去掉
                首尾空白，空白键返回 422。
            _: 管理员会话依赖结果，仅用于确认调用方已登录。
            __: 同源依赖结果，仅用于阻止带恶意 Origin 的浏览器副作用请求。

        返回：
            202 响应，包含运行/任务 ID、版本键、当前执行状态、首次接受时间和
            ``created`` 标志。重复请求的状态可能已经从 ``queued`` 推进到其他状态。

        异常：
            HTTPException(401/403/422/409/503): 分别对应会话无效、Origin 不匹配、
            请求或幂等键不合法、键指向不同内容、持久化不可用。
        """
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Idempotency-Key must not be blank",
            )
        if not principal.resource_scope.allows(
            request_body.installation_id,
            request_body.repository,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="GitHub installation or repository is not allowed",
            )
        denial_reason = get_github_access_policy().denial_reason(
            request_body.installation_id,
            request_body.repository,
        )
        if denial_reason is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="GitHub installation or repository is not allowed",
            )
        try:
            result = get_review_service().submit(
                request_body,
                normalized_key,
                actor=principal.username,
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency-Key was already used for a different request",
            ) from exc
        except ReviewPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="review persistence is temporarily unavailable",
            ) from exc
        except ReviewQuotaExceededError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="review creation quota exceeded",
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc

        return ReviewAcceptedResponse(
            review_run_id=result.review_run_id,
            review_task_id=result.review_task_id,
            review_version_key=result.review_version_key,
            execution_status=result.execution_status,
            accepted_at=result.accepted_at,
            created=result.created,
        )
