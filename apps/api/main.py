"""FastAPI entry point for OpenReviewer."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from threading import RLock
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from domain.enums import ExecutionStatus, WorkerStatus
from domain.models import ReviewRequest
from persistence.dashboard import SqlAlchemyDashboardRepository
from persistence.database import Database, DatabaseConfigurationError
from persistence.repositories import SqlAlchemyReviewRepository
from services.auth import (
    AuthConfigurationError,
    AuthService,
    AuthSettings,
    InvalidSessionError,
    LoginAttemptLimiter,
    LoginRateLimitError,
    SessionPrincipal,
)
from services.dashboard import (
    DashboardPersistenceError,
    DashboardService,
    DashboardSnapshot,
    ReviewListItem,
)
from services.reviews import (
    IdempotencyConflictError,
    ReviewPersistenceError,
    ReviewService,
)


class HealthResponse(BaseModel):
    """Public liveness response without configuration details."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    service: Literal["openreviewer"] = "openreviewer"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=512)


class AuthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    authenticated: Literal[True] = True
    username: str
    expires_at: datetime


class ReviewAcceptedResponse(BaseModel):
    """Stable acknowledgement for an asynchronously queued review."""

    model_config = ConfigDict(frozen=True)

    review_run_id: str
    review_task_id: str
    review_version_key: str
    execution_status: ExecutionStatus
    accepted_at: datetime
    created: bool


class ReviewItemResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    review_run_id: str
    review_task_id: str
    repository: str
    pull_request_number: int
    head_sha: str
    execution_status: ExecutionStatus
    attempt_count: int
    max_attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_item(cls, item: ReviewListItem) -> "ReviewItemResponse":
        return cls(**{field: getattr(item, field) for field in cls.model_fields})


class ReviewListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    items: tuple[ReviewItemResponse, ...]


class WorkerResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    configured: bool
    online: bool
    worker_id: str | None
    status: WorkerStatus | None
    current_task_id: str | None
    started_at: datetime | None
    last_seen_at: datetime | None


class DashboardResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    total_reviews: int
    status_counts: dict[ExecutionStatus, int]
    worker: WorkerResponse
    recent_reviews: tuple[ReviewItemResponse, ...]

    @classmethod
    def from_snapshot(cls, snapshot: DashboardSnapshot) -> "DashboardResponse":
        return cls(
            generated_at=snapshot.generated_at,
            total_reviews=snapshot.total_reviews,
            status_counts=dict(snapshot.status_counts),
            worker=WorkerResponse(
                **{
                    field: getattr(snapshot.worker, field)
                    for field in WorkerResponse.model_fields
                }
            ),
            recent_reviews=tuple(
                ReviewItemResponse.from_item(item)
                for item in snapshot.recent_reviews
            ),
        )


def create_app(
    review_service: ReviewService | None = None,
    auth_service: AuthService | None = None,
    dashboard_service: DashboardService | None = None,
    login_limiter: LoginAttemptLimiter | None = None,
) -> FastAPI:
    """Create an API instance with injectable application boundaries."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        database: Database | None = application.state.owned_database
        if database is not None:
            database.dispose()

    application = FastAPI(
        title="OpenReviewer API",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.review_service = review_service
    application.state.auth_service = auth_service
    application.state.dashboard_service = dashboard_service
    application.state.login_limiter = login_limiter or LoginAttemptLimiter()
    application.state.owned_database = None
    initialization_lock = RLock()

    def get_database() -> Database:
        configured_database: Database | None = application.state.owned_database
        if configured_database is not None:
            return configured_database
        with initialization_lock:
            configured_database = application.state.owned_database
            if configured_database is not None:
                return configured_database
            try:
                configured_database = Database.from_environment()
            except DatabaseConfigurationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="review persistence is not configured",
                ) from exc
            application.state.owned_database = configured_database
            return configured_database

    def get_review_service() -> ReviewService:
        configured_service: ReviewService | None = application.state.review_service
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.review_service
            if configured_service is None:
                configured_service = ReviewService(
                    SqlAlchemyReviewRepository(get_database().sessions)
                )
                application.state.review_service = configured_service
            return configured_service

    def get_auth_service() -> AuthService:
        configured_service: AuthService | None = application.state.auth_service
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.auth_service
            if configured_service is None:
                try:
                    configured_service = AuthService(AuthSettings.from_environment())
                except AuthConfigurationError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="administrator authentication is not configured",
                    ) from exc
                application.state.auth_service = configured_service
            return configured_service

    def get_dashboard_service() -> DashboardService:
        configured_service: DashboardService | None = (
            application.state.dashboard_service
        )
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.dashboard_service
            if configured_service is None:
                configured_service = DashboardService(
                    SqlAlchemyDashboardRepository(get_database().sessions)
                )
                application.state.dashboard_service = configured_service
            return configured_service

    def require_principal(request: Request) -> SessionPrincipal:
        service = get_auth_service()
        try:
            return service.verify_session(
                request.cookies.get(service.settings.cookie_name)
            )
        except InvalidSessionError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
                headers={
                    "WWW-Authenticate": "Session",
                    "Cache-Control": "no-store",
                },
            ) from exc

    def require_same_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if not origin:
            return
        forwarded_scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        expected = f"{forwarded_scheme}://{request.headers.get('host', '')}"
        parsed = urlsplit(origin)
        normalized_origin = f"{parsed.scheme}://{parsed.netloc}"
        if normalized_origin != expected:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cross-origin request rejected",
            )

    def dashboard_snapshot(limit: int) -> DashboardResponse:
        try:
            snapshot = get_dashboard_service().snapshot(limit)
        except DashboardPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="dashboard data is temporarily unavailable",
            ) from exc
        return DashboardResponse.from_snapshot(snapshot)

    @application.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/v1/auth"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.get(
        "/healthz",
        response_model=HealthResponse,
        include_in_schema=False,
    )
    async def healthz() -> HealthResponse:
        return HealthResponse()

    @application.post(
        "/api/v1/auth/login",
        response_model=AuthResponse,
    )
    def login(
        credentials: LoginRequest,
        request: Request,
        response: Response,
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AuthResponse:
        client_address = request.headers.get("x-real-ip") or (
            request.client.host if request.client is not None else "unknown"
        )
        limiter_key = f"{client_address}|{credentials.username.casefold()}"
        limiter: LoginAttemptLimiter = application.state.login_limiter
        try:
            limiter.check(limiter_key)
        except LoginRateLimitError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many login attempts; try again later",
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc

        service = get_auth_service()
        if not service.verify_credentials(credentials.username, credentials.password):
            limiter.record_failure(limiter_key)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid username or password",
            )

        limiter.reset(limiter_key)
        token, principal = service.create_session()
        response.set_cookie(
            key=service.settings.cookie_name,
            value=token,
            max_age=int(service.settings.session_ttl.total_seconds()),
            expires=principal.expires_at,
            path="/",
            secure=service.settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return AuthResponse(
            username=principal.username,
            expires_at=principal.expires_at,
        )

    @application.post(
        "/api/v1/auth/logout",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def logout(
        request: Request,
        response: Response,
        _: Annotated[None, Depends(require_same_origin)],
    ) -> None:
        service = get_auth_service()
        response.delete_cookie(
            key=service.settings.cookie_name,
            path="/",
            secure=service.settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )

    @application.get(
        "/api/v1/auth/me",
        response_model=AuthResponse,
    )
    def current_user(
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> AuthResponse:
        return AuthResponse(
            username=principal.username,
            expires_at=principal.expires_at,
        )

    @application.get(
        "/api/v1/dashboard",
        response_model=DashboardResponse,
    )
    def get_dashboard(
        _: Annotated[SessionPrincipal, Depends(require_principal)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> DashboardResponse:
        return dashboard_snapshot(limit)

    @application.get(
        "/api/v1/reviews",
        response_model=ReviewListResponse,
    )
    def list_reviews(
        _: Annotated[SessionPrincipal, Depends(require_principal)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> ReviewListResponse:
        snapshot = dashboard_snapshot(limit)
        return ReviewListResponse(
            total=snapshot.total_reviews,
            items=snapshot.recent_reviews,
        )

    @application.get("/api/v1/reviews/stream")
    async def stream_reviews(
        request: Request,
        _: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> StreamingResponse:
        async def events():
            while not await request.is_disconnected():
                try:
                    response_model = await run_in_threadpool(dashboard_snapshot, 50)
                    payload = response_model.model_dump_json()
                    event_id = sha256(payload.encode("utf-8")).hexdigest()[:16]
                    yield (
                        f"id: {event_id}\n"
                        "event: dashboard\n"
                        f"data: {payload}\n\n"
                    )
                except HTTPException:
                    yield (
                        "event: unavailable\n"
                        'data: {"detail":"dashboard temporarily unavailable"}\n\n'
                    )
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
        _: Annotated[SessionPrincipal, Depends(require_principal)],
        __: Annotated[None, Depends(require_same_origin)],
    ) -> ReviewAcceptedResponse:
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Idempotency-Key must not be blank",
            )
        try:
            result = get_review_service().submit(request_body, normalized_key)
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

        return ReviewAcceptedResponse(
            review_run_id=result.review_run_id,
            review_task_id=result.review_task_id,
            review_version_key=result.review_version_key,
            execution_status=result.execution_status,
            accepted_at=result.accepted_at,
            created=result.created,
        )

    return application


app = create_app()
