"""Application service for accepting asynchronous review requests."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Protocol

from domain.enums import ExecutionStatus
from domain.models import ReviewRequest


class IdempotencyConflictError(ValueError):
    """The same idempotency key was reused for different request content."""


class ReviewPersistenceError(RuntimeError):
    """The review request could not be durably persisted."""


@dataclass(frozen=True, slots=True)
class ReviewSubmissionResult:
    review_run_id: str
    review_task_id: str
    review_version_key: str
    execution_status: ExecutionStatus
    accepted_at: datetime
    created: bool


class ReviewRepository(Protocol):
    """Persistence boundary used by the review submission use case."""

    def create_or_get(
        self,
        request: ReviewRequest,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> ReviewSubmissionResult: ...


class ReviewService:
    def __init__(self, repository: ReviewRepository) -> None:
        self._repository = repository

    def submit(
        self,
        request: ReviewRequest,
        idempotency_key: str,
    ) -> ReviewSubmissionResult:
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise ValueError("idempotency key must not be blank")
        if len(normalized_key) > 200:
            raise ValueError("idempotency key must not exceed 200 characters")

        canonical_payload = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        request_fingerprint = sha256(canonical_payload).hexdigest()
        return self._repository.create_or_get(
            request,
            normalized_key,
            request_fingerprint,
        )
