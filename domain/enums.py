"""审查契约共享的稳定枚举值。"""

from enum import StrEnum


class PullRequestAction(StrEnum):
    """可以启动或刷新审查的 GitHub Pull Request 动作。"""

    OPENED = "opened"
    SYNCHRONIZE = "synchronize"
    REOPENED = "reopened"
    READY_FOR_REVIEW = "ready_for_review"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingCategory(StrEnum):
    ARCHITECTURE = "architecture"
    AUTHORIZATION = "authorization"
    SECURITY = "security"
    DATABASE = "database"
    BUSINESS_CONTRACT = "business_contract"
    TEST_GAP = "test_gap"
    RELIABILITY = "reliability"


class VerificationStatus(StrEnum):
    """平台对 Finding 定位是否可由当前 Diff 证明的机器校验结果。"""

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ModelProvider(StrEnum):
    """模型调用适配器支持的供应商。"""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class ModelApiProtocol(StrEnum):
    """供应商调用使用的稳定 HTTP API 协议。"""

    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"
    MESSAGES = "messages"


class ModelCallStatus(StrEnum):
    """一次模型阶段完成记录的状态。"""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"


class ModelBatchStatus(StrEnum):
    """持久化模型批次的恢复状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReviewAgent(StrEnum):
    """固定 DAG 中的四个模型节点。"""

    SECURITY = "security"
    CONVENTION = "convention"
    LOGIC = "logic"
    SUMMARY = "summary"


class ModelReviewVerdict(StrEnum):
    """模型对当前可见审查范围给出的有界结论。"""

    ISSUES_FOUND = "issues_found"
    NO_ACTIONABLE_ISSUE = "no_actionable_issue"
    INSUFFICIENT_CONTEXT = "insufficient_context"


class ModelReasoningEffort(StrEnum):
    """模型推理强度；``none`` 表示不发送可选推理参数。"""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


class ExecutionStatus(StrEnum):
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


class WorkerStatus(StrEnum):
    """队列 Worker 进程使用的精简可观察生命周期。"""

    STARTING = "starting"
    IDLE = "idle"
    BUSY = "busy"
    STOPPING = "stopping"


class ExternalActionState(StrEnum):
    """一次幂等外部副作用使用的持久化生命周期。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FindingLifecycleState(StrEnum):
    """同一 PR 中稳定 Finding 指纹的跨提交状态。"""

    PRESENT = "present"
    FIXED = "fixed"


class FindingOccurrenceStatus(StrEnum):
    """某一轮审查中 Finding 相对历史提交的出现方式。"""

    NEW = "new"
    STILL_PRESENT = "still_present"
    REINTRODUCED = "reintroduced"


class FindingEvaluationVerdict(StrEnum):
    """人工裁决转换成的真实评测标签。"""

    VALID = "valid"
    FALSE_POSITIVE = "false_positive"
    DUPLICATE = "duplicate"
    OUT_OF_SCOPE = "out_of_scope"
    KNOWN_ISSUE = "known_issue"


class FindingAdjudicationStatus(StrEnum):
    """人工对 Finding 的独立裁决，不与机器定位校验混用。"""

    UNREVIEWED = "unreviewed"
    VALID = FindingEvaluationVerdict.VALID.value
    FALSE_POSITIVE = FindingEvaluationVerdict.FALSE_POSITIVE.value
    DUPLICATE = FindingEvaluationVerdict.DUPLICATE.value
    OUT_OF_SCOPE = FindingEvaluationVerdict.OUT_OF_SCOPE.value
    KNOWN_ISSUE = FindingEvaluationVerdict.KNOWN_ISSUE.value


class EvidenceVerificationStatus(StrEnum):
    """平台回读源码后得到的自动证据核验状态，不代表人工事实裁决。"""

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"
    NOT_APPLICABLE = "not_applicable"


def evidence_verification_status_for(
    adjudication_status: FindingAdjudicationStatus | str,
) -> EvidenceVerificationStatus:
    """兼容旧评测标签映射；生产流程不会用它覆盖自动源码核验结果。"""

    adjudication = FindingAdjudicationStatus(adjudication_status)
    if adjudication is FindingAdjudicationStatus.VALID:
        return EvidenceVerificationStatus.VERIFIED
    if adjudication is FindingAdjudicationStatus.FALSE_POSITIVE:
        return EvidenceVerificationStatus.REJECTED
    if adjudication is FindingAdjudicationStatus.UNREVIEWED:
        return EvidenceVerificationStatus.UNVERIFIED
    return EvidenceVerificationStatus.NOT_APPLICABLE


class ReviewConclusion(StrEnum):
    NO_CONFIRMED_FINDINGS = "no_confirmed_findings"
    FINDINGS_PRESENT = "findings_present"
    NEEDS_HUMAN = "needs_human"
    INDETERMINATE = "indeterminate"
    NOT_APPLICABLE = "not_applicable"


class CoverageStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    STALE = "stale"


class FileDisposition(StrEnum):
    MODEL_REVIEWED = "model_reviewed"
    DETERMINISTIC_ONLY = "deterministic_only"
    GENERATED = "generated"
    BINARY = "binary"
    UNSUPPORTED = "unsupported"
    OMITTED_BY_LIMIT = "omitted_by_limit"


class ReviewFileDecision(StrEnum):
    """模型调用前，规划器为每个 changed file 给出的明确去向。"""

    PLANNED = "planned"
    BINARY = "binary"
    GENERATED = "generated"
    UNSUPPORTED = "unsupported"
    PATCH_MISSING = "patch_missing"
    PATCH_TOO_LARGE = "patch_too_large"
    RULES_INCOMPLETE = "rules_incomplete"
    OMITTED_BY_BUDGET = "omitted_by_budget"


class RepositoryRuleIssueKind(StrEnum):
    """AGENTS.md 批量读取不能完整用于某些文件的确定性原因。"""

    CANDIDATE_LIMIT = "candidate_limit"
    SCOPE_DEPTH_LIMIT = "scope_depth_limit"
    RESPONSE_TOO_LARGE = "response_too_large"
    BINARY = "binary"
    TOO_LARGE = "too_large"
    CONTENT_UNAVAILABLE = "content_unavailable"
    TOTAL_LIMIT = "total_limit"


class LocationSide(StrEnum):
    LEFT = "left"
    RIGHT = "right"


class PullRequestState(StrEnum):
    """GitHub Pull Request 当前是否仍可继续审查。"""

    OPEN = "open"
    CLOSED = "closed"


class ChangedFileStatus(StrEnum):
    """GitHub changed files 接口返回的稳定文件状态。"""

    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    RENAMED = "renamed"
    COPIED = "copied"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class PatchState(StrEnum):
    """一个变更文件的补丁是否可供后续审查。"""

    AVAILABLE = "available"
    BINARY = "binary"
    MISSING = "missing"
    TOO_LARGE = "too_large"


class CiState(StrEnum):
    """与某个精确 head SHA 绑定的 CI 汇总状态。"""

    NOT_CONFIGURED = "not_configured"
    UNKNOWN = "unknown"
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"


class CiCheckKind(StrEnum):
    """组成 CI 汇总结果的 GitHub 状态来源。"""

    CHECK_RUN = "check_run"
    COMMIT_STATUS = "commit_status"
