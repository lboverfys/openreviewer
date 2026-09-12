"""仓库策略及随任务冻结的版本快照。"""

from fnmatch import fnmatchcase

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain.paths import normalize_repository_path
from domain.security import ErrorCode, SafeApplicationError, SafeError


class RepositoryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    target_branches: tuple[str, ...] = Field(default=(), max_length=20)
    # None 继承知识库；空元组明确不使用知识库。AGENTS.md 始终按目录加载。
    knowledge_sources: tuple[str, ...] | None = Field(default=None, max_length=100)
    max_model_requests: int | None = Field(default=None, ge=1, le=10_000)
    approver: str | None = Field(default=None, min_length=1, max_length=100)
    monthly_budget_microusd: int | None = Field(default=None, ge=1, le=1_000_000_000_000)
    budget_warning_percent: int = Field(default=80, ge=1, le=100)
    max_concurrent_reviews: int | None = Field(default=None, ge=1, le=100)
    approval_timeout_hours: int = Field(default=24, ge=1, le=720)
    review_profile_id: str | None = Field(default=None, min_length=1, max_length=36)

    @field_validator("target_branches")
    @classmethod
    def validate_branches(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value or len(value) > 255 or value != value.strip()
            or any(ord(character) < 32 for character in value)
            for value in values
        ):
            raise ValueError("目标分支不能为空或包含控制字符")
        return tuple(dict.fromkeys(values))

    @field_validator("knowledge_sources")
    @classmethod
    def validate_sources(cls, values: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if values is None:
            return None
        return tuple(dict.fromkeys(normalize_repository_path(value) for value in values))

    def allows_branch(self, branch: str | None) -> bool:
        return not self.target_branches or (
            branch is not None
            and any(fnmatchcase(branch, pattern) for pattern in self.target_branches)
        )


class RepositoryPolicySnapshot(RepositoryPolicy):
    repository: str
    revision: int = Field(ge=1)


class RepositoryPolicyDeniedError(ValueError):
    """仓库暂停接收新任务，调用方应返回可解释的拒绝。"""


class RepositoryRequestLimitError(SafeApplicationError):
    def __init__(self) -> None:
        super().__init__(SafeError(
            code=ErrorCode.MODEL_BUDGET_EXCEEDED,
            safe_message="已达到仓库的模型请求上限，请调整策略后重新审查",
            retryable=False,
            details={"budget_reason": "repository_request_limit"},
        ))
