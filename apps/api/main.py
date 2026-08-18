"""FastAPI entry point for OpenReviewer."""

from contextlib import asynccontextmanager
from datetime import datetime
from threading import Lock
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict

from domain.enums import ExecutionStatus
from domain.models import ReviewRequest
from persistence.database import Database, DatabaseConfigurationError
from persistence.repositories import SqlAlchemyReviewRepository
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


class ReviewAcceptedResponse(BaseModel):
    """Stable acknowledgement for an asynchronously queued review."""

    model_config = ConfigDict(frozen=True)

    review_run_id: str
    review_task_id: str
    review_version_key: str
    execution_status: ExecutionStatus
    accepted_at: datetime
    created: bool


def create_app(review_service: ReviewService | None = None) -> FastAPI:
    """Create an API instance with an injectable review use case."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        database: Database | None = application.state.owned_database
        if database is not None:
            database.dispose()

    application = FastAPI(
        title="OpenReviewer API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.review_service = review_service
    application.state.owned_database = None
    service_initialization_lock = Lock()

    def get_review_service() -> ReviewService:
        configured_service: ReviewService | None = application.state.review_service
        if configured_service is not None:
            return configured_service

        with service_initialization_lock:
            configured_service = application.state.review_service
            if configured_service is not None:
                return configured_service
            try:
                database = Database.from_environment()
            except DatabaseConfigurationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="review persistence is not configured",
                ) from exc
            configured_service = ReviewService(
                SqlAlchemyReviewRepository(database.sessions)
            )
            application.state.owned_database = database
            application.state.review_service = configured_service
            return configured_service

    @application.get(
        "/healthz",
        response_model=HealthResponse,
        include_in_schema=False,
    )
    async def healthz() -> HealthResponse:
        return HealthResponse()

    @application.post(
        "/api/v1/reviews",
        response_model=ReviewAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_review(
        request: ReviewRequest,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=200,
            ),
        ],
    ) -> ReviewAcceptedResponse:
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Idempotency-Key must not be blank",
            )
        try:
            result = get_review_service().submit(request, normalized_key)
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
