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


class ModelProvider(str, Enum):
    """模型调用适配器支持的供应商。"""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class ModelApiProtocol(str, Enum):
    """供应商调用使用的稳定 HTTP API 协议。"""

    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"
    MESSAGES = "messages"


class ModelCallStatus(str, Enum):
    """一次模型阶段完成记录的状态。"""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"


class ModelBatchStatus(str, Enum):
    """持久化模型批次的恢复状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReviewAgent(str, Enum):
    """固定 DAG 中的四个模型节点。"""

    SECURITY = "security"
    CONVENTION = "convention"
    LOGIC = "logic"
    SUMMARY = "summary"


class ModelReasoningEffort(str, Enum):
    """模型推理强度；``none`` 表示不发送可选推理参数。"""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


class ExecutionStatus(str, Enum):
    QUEUED = "queued"
    CI = "ci"
    PLANNING = "planning"
    AGENT_BATCHES = "agent_batches"
    AGGREGATING = "aggregating"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    AWAITING_PUBLISH = "awaiting_publish"
    PUBLISHING = "publishing"
    PAUSED = "paused"
    WAITING_FOR_CI = "waiting_for_ci"
    RUNNING = "running"
    READY_FOR_REVIEW = "ready_for_review"
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


class ReviewFileDecision(str, Enum):
    """模型调用前，规划器为每个 changed file 给出的明确去向。"""

    PLANNED = "planned"
    BINARY = "binary"
    GENERATED = "generated"
    UNSUPPORTED = "unsupported"
    PATCH_MISSING = "patch_missing"
    PATCH_TOO_LARGE = "patch_too_large"
    RULES_INCOMPLETE = "rules_incomplete"
    OMITTED_BY_BUDGET = "omitted_by_budget"


class RepositoryRuleIssueKind(str, Enum):
    """AGENTS.md 批量读取不能完整用于某些文件的确定性原因。"""

    CANDIDATE_LIMIT = "candidate_limit"
    SCOPE_DEPTH_LIMIT = "scope_depth_limit"
    RESPONSE_TOO_LARGE = "response_too_large"
    BINARY = "binary"
    TOO_LARGE = "too_large"
    CONTENT_UNAVAILABLE = "content_unavailable"
    TOTAL_LIMIT = "total_limit"


class LocationSide(str, Enum):
    LEFT = "left"
    RIGHT = "right"


class PullRequestState(str, Enum):
    """GitHub Pull Request 当前是否仍可继续审查。"""

    OPEN = "open"
    CLOSED = "closed"


class ChangedFileStatus(str, Enum):
    """GitHub changed files 接口返回的稳定文件状态。"""

    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    RENAMED = "renamed"
    COPIED = "copied"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class PatchState(str, Enum):
    """一个变更文件的补丁是否可供后续审查。"""

    AVAILABLE = "available"
    BINARY = "binary"
    MISSING = "missing"
    TOO_LARGE = "too_large"


class CiState(str, Enum):
    """与某个精确 head SHA 绑定的 CI 汇总状态。"""

    UNKNOWN = "unknown"
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"


class CiCheckKind(str, Enum):
    """组成 CI 汇总结果的 GitHub 状态来源。"""

    CHECK_RUN = "check_run"
    COMMIT_STATUS = "commit_status"
