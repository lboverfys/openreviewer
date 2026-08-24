"""审查契约共享的稳定枚举值。"""

from enum import Enum


class PullRequestAction(str, Enum):
    """可以启动或刷新审查的 GitHub Pull Request 动作。"""

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


class WorkerStatus(str, Enum):
    """队列 Worker 进程使用的精简可观察生命周期。"""

    STARTING = "starting"
    IDLE = "idle"
    BUSY = "busy"
    STOPPING = "stopping"


class ExternalActionState(str, Enum):
    """一次幂等外部副作用使用的持久化生命周期。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


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
