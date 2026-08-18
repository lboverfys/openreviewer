"""Minimal FastAPI entry point for OpenReviewer."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    """Public liveness response without configuration details."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    service: Literal["openreviewer"] = "openreviewer"


def create_app() -> FastAPI:
    """Create an API instance without external service dependencies."""

    application = FastAPI(
        title="OpenReviewer API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.get(
        "/healthz",
        response_model=HealthResponse,
        include_in_schema=False,
    )
    async def healthz() -> HealthResponse:
        return HealthResponse()

    return application


app = create_app()
