"""Dashboard、审查列表与实时事件路由。"""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from apps.api.schemas import DashboardResponse, ReviewListResponse
from services.auth import (
    AuthPersistenceError,
    AuthService,
    InvalidSessionError,
    SessionPrincipal,
)
from services.dashboard_stream import (
    DashboardStreamCoordinator,
    DashboardStreamUnavailable,
)


def register_dashboard_routes(
    application: FastAPI,
    *,
    dashboard_snapshot: Callable[..., DashboardResponse],
    get_dashboard_stream: Callable[..., DashboardStreamCoordinator[DashboardResponse]],
    get_auth_service: Callable[[], AuthService],
    require_review_viewer: Callable[..., SessionPrincipal],
) -> None:
    """注册 Dashboard、分页列表和共享 SSE 端点。"""

    def load_snapshot(
        principal: SessionPrincipal,
        limit: int,
        cursor: str | None,
    ) -> DashboardResponse:
        """按主体资源范围读取快照；旧的注入回调继续支持全量调用。"""

        scope = principal.resource_scope
        if scope.unrestricted:
            return dashboard_snapshot(limit, cursor)
        return dashboard_snapshot(limit, cursor, scope)

    def stream_for(principal: SessionPrincipal) -> DashboardStreamCoordinator[DashboardResponse]:
        scope = principal.resource_scope
        if scope.unrestricted:
            return get_dashboard_stream()
        return get_dashboard_stream(scope)

    @application.get(
        "/api/v1/dashboard",
        response_model=DashboardResponse,
    )
    def get_dashboard(
        principal: Annotated[SessionPrincipal, Depends(require_review_viewer)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> DashboardResponse:
        """返回认证后的任务统计、最近任务和 Worker 状态。

        参数：
            _: 仅用于触发管理员会话校验的依赖结果。
            limit: 最近任务数量，范围 1 到 100，默认 50。

        返回：
            当前数据库快照；Worker 没有心跳时会明确标记为未配置/离线，而不是
            猜测健康状态。

        异常：
            HTTPException(401): 会话无效。
            HTTPException(503): Dashboard 数据无法读取。
        """
        return load_snapshot(principal, limit, cursor)

    @application.get(
        "/api/v1/reviews",
        response_model=ReviewListResponse,
    )
    def list_reviews(
        principal: Annotated[SessionPrincipal, Depends(require_review_viewer)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ReviewListResponse:
        """返回认证后的最近审查任务列表。

        参数：
            _: 管理员会话依赖。
            limit: 返回条数，范围 1 到 100，默认 50。

        返回：
            包含数据库中的总运行数和按创建时间倒序排列的最近任务；任务状态、
            尝试次数和最后错误来自同一次 Dashboard 读取。

        异常：
            HTTPException(401): 会话无效。
            HTTPException(503): 查询失败。
        """
        snapshot = load_snapshot(principal, limit, cursor)
        return ReviewListResponse(
            total=snapshot.total_reviews,
            items=snapshot.recent_reviews,
            next_cursor=snapshot.next_cursor,
        )

    @application.get("/api/v1/reviews/stream")
    async def stream_reviews(
        request: Request,
        principal: Annotated[SessionPrincipal, Depends(require_review_viewer)],
    ) -> StreamingResponse:
        """建立认证后的 Server-Sent Events 实时 Dashboard 流。

        同一 API 进程内的连接共享两秒轮询和快照缓存；只有任务或 Worker 可见状态
        变化时才重新加载完整快照。读取暂时失败只发送 ``unavailable`` 事件，不把
        错误数据伪装成正常快照；响应头关闭 Nginx 缓冲。

        参数：
            request: 用于检测浏览器是否已断开连接。
            principal: 建立流之前验证得到的管理员会话主体。

        返回：
            ``text/event-stream`` 响应。每条正常事件包含哈希事件 ID、事件名和
            完整 Dashboard JSON；暂时读取失败时发送稳定的 ``unavailable`` 事件。

        注意：
            长连接每 30 秒重新查询一次服务端会话状态，并单独检查绝对到期时间；
            会话过期或被注销后发送 ``auth-expired`` 事件并结束连接。
        """
        service = get_auth_service()
        session_token = request.cookies.get(service.settings.cookie_name)

        async def events():
            """每两秒检查共享 Dashboard 状态，直到浏览器断开。

            生成器先检查 ``request.is_disconnected``，避免客户端离开后继续查询
            数据库；正常快照的 JSON 内容同时用于计算短事件 ID，便于浏览器识别
            重复数据。Dashboard 临时不可用时只发送不含内部异常的 ``unavailable``
            事件，随后等待下一轮恢复，不会结束整个连接。
            """
            next_auth_check = time.monotonic()
            last_change_token = request.headers.get("last-event-id")
            last_keepalive_at = time.monotonic()
            unavailable_sent = False
            while not await request.is_disconnected():
                if datetime.now(UTC) >= principal.expires_at:
                    yield 'event: auth-expired\ndata: {"detail":"authentication required"}\n\n'
                    break
                if time.monotonic() >= next_auth_check:
                    try:
                        await run_in_threadpool(service.verify_session, session_token)
                    except (InvalidSessionError, AuthPersistenceError):
                        yield 'event: auth-expired\ndata: {"detail":"authentication required"}\n\n'
                        break
                    next_auth_check = time.monotonic() + 30
                try:
                    update = await run_in_threadpool(stream_for(principal).poll)
                    if update.change_token != last_change_token:
                        payload = update.snapshot.model_dump_json()
                        yield (
                            f"id: {update.change_token}\n"
                            "event: dashboard\n"
                            f"data: {payload}\n\n"
                        )
                        last_change_token = update.change_token
                        last_keepalive_at = time.monotonic()
                        unavailable_sent = False
                    elif time.monotonic() - last_keepalive_at >= 15:
                        yield ": keepalive\n\n"
                        last_keepalive_at = time.monotonic()
                except (HTTPException, DashboardStreamUnavailable):
                    if not unavailable_sent:
                        yield (
                            "event: unavailable\n"
                            'data: {"detail":"dashboard temporarily unavailable"}\n\n'
                        )
                        unavailable_sent = True
                    elif time.monotonic() - last_keepalive_at >= 15:
                        yield ": keepalive\n\n"
                        last_keepalive_at = time.monotonic()
                await asyncio.sleep(2)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
