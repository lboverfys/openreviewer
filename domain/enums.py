"""Stable values shared by the review contract."""

from enum import Enum


class PullRequestAction(str, Enum):
    """GitHub pull request actions that can start or refresh a review."""

    OPENED = "opened"
    SYNCHRONIZE = "synchronize"
    REOPENED = "reopened"
    READY_FOR_REVIEW = "ready_for_review"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingCategory(str, Enum):
    ARCHITECTURE = "architecture"
    AUTHORIZATION = "authorization"
    SECURITY = "security"
    DATABASE = "database"
    BUSINESS_CONTRACT = "business_contract"
    TEST_GAP = "test_gap"
    RELIABILITY = "reliability"


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ExecutionStatus(str, Enum):
    QUEUED = "queued"
    WAITING_FOR_CI = "waiting_for_ci"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class ReviewConclusion(str, Enum):
    NO_CONFIRMED_FINDINGS = "no_confirmed_findings"
    FINDINGS_PRESENT = "findings_present"
    NEEDS_HUMAN = "needs_human"
    INDETERMINATE = "indeterminate"
    NOT_APPLICABLE = "not_applicable"


class CoverageStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    STALE = "stale"


class FileDisposition(str, Enum):
    MODEL_REVIEWED = "model_reviewed"
    DETERMINISTIC_ONLY = "deterministic_only"
    GENERATED = "generated"
    BINARY = "binary"
    UNSUPPORTED = "unsupported"
    OMITTED_BY_LIMIT = "omitted_by_limit"


class LocationSide(str, Enum):
    LEFT = "left"
    RIGHT = "right"
