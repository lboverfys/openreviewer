"""Pydantic models for the first version of the review contract."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain.enums import (
    CoverageStatus,
    ExecutionStatus,
    FileDisposition,
    FindingCategory,
    LocationSide,
    PullRequestAction,
    ReviewConclusion,
    Severity,
    VerificationStatus,
)
from domain.identifiers import build_review_version_key, normalize_sha


class ContractModel(BaseModel):
    """Base model with strict fields and predictable string handling."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReviewVersion(ContractModel):
    repository_id: int = Field(gt=0)
    repository: str = Field(min_length=1, max_length=255)
    pull_request_number: int = Field(gt=0)
    base_sha: str = Field(min_length=40, max_length=64)
    head_sha: str = Field(min_length=40, max_length=64)

    @field_validator("base_sha", "head_sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        return normalize_sha(value)

    @property
    def review_version_key(self) -> str:
        return build_review_version_key(
            self.repository_id,
            self.pull_request_number,
            self.head_sha,
        )

    def is_current(self, current_head_sha: str) -> bool:
        return self.head_sha == normalize_sha(current_head_sha)


class PullRequestWebhook(ContractModel):
    """Minimal trusted data extracted from an accepted GitHub event."""

    event_type: Literal["pull_request"] = "pull_request"
    action: PullRequestAction
    delivery_id: str = Field(min_length=1, max_length=200)
    installation_id: int = Field(gt=0)
    repository_id: int = Field(gt=0)
    repository: str = Field(min_length=1, max_length=255)
    pull_request_number: int = Field(gt=0)
    head_sha: str = Field(min_length=40, max_length=64)

    @field_validator("head_sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        return normalize_sha(value)

    @property
    def review_version_key(self) -> str:
        return build_review_version_key(
            self.repository_id,
            self.pull_request_number,
            self.head_sha,
        )

    @property
    def deduplication_key(self) -> str:
        return self.delivery_id


class FindingLocation(ContractModel):
    file: str = Field(min_length=1, max_length=1024)
    blob_sha: str | None = Field(default=None, min_length=40, max_length=64)
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)
    side: LocationSide = LocationSide.RIGHT
    in_diff: bool = False
    symbol: str | None = Field(default=None, max_length=512)

    @field_validator("blob_sha")
    @classmethod
    def validate_blob_sha(cls, value: str | None) -> str | None:
        return normalize_sha(value) if value is not None else None

    @field_validator("file")
    @classmethod
    def validate_relative_file(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        parts = normalized.split("/")
        if normalized.startswith("/") or ".." in parts:
            raise ValueError("file must be a repository-relative path")
        return normalized

    @model_validator(mode="after")
    def validate_line_range(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ReviewFinding(ContractModel):
    fingerprint: str = Field(min_length=1, max_length=256)
    head_sha: str = Field(min_length=40, max_length=64)
    severity: Severity
    category: FindingCategory
    location: FindingLocation | None = None
    title: str = Field(min_length=1, max_length=300)
    evidence: str = Field(min_length=1, max_length=10000)
    impact: str = Field(min_length=1, max_length=10000)
    suggestion: str = Field(min_length=1, max_length=10000)
    required_test: str | None = Field(default=None, max_length=10000)
    confidence: float = Field(ge=0, le=1)
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    rule_reference: str | None = Field(default=None, max_length=512)

    @field_validator("head_sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        return normalize_sha(value)

    def can_publish_inline(
        self,
        current_head_sha: str,
        confidence_threshold: float = 0.90,
    ) -> bool:
        """Whether this finding is a candidate for an inline comment."""

        if not 0 <= confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be between 0 and 1")
        return (
            self.verification_status is VerificationStatus.VERIFIED
            and self.location is not None
            and self.location.in_diff
            and self.location.side is LocationSide.RIGHT
            and self.head_sha == normalize_sha(current_head_sha)
            and self.confidence >= confidence_threshold
            and self.category is not FindingCategory.TEST_GAP
        )


class FileCoverageItem(ContractModel):
    file: str = Field(min_length=1, max_length=1024)
    disposition: FileDisposition
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("file")
    @classmethod
    def validate_relative_file(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("file must be a repository-relative path")
        return normalized


class ReviewRunState(ContractModel):
    review_run_id: str = Field(min_length=1, max_length=128)
    version: ReviewVersion
    execution_status: ExecutionStatus
    review_conclusion: ReviewConclusion | None = None
    coverage_status: CoverageStatus = CoverageStatus.UNKNOWN

    @model_validator(mode="after")
    def completed_run_has_conclusion(self) -> Self:
        if (
            self.execution_status is ExecutionStatus.COMPLETED
            and self.review_conclusion is None
        ):
            raise ValueError("completed review runs must have a conclusion")
        return self
