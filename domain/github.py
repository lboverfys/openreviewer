"""GitHub PR、变更文件和 CI 快照的严格领域契约。"""

from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain.enums import (
    ChangedFileStatus,
    CiCheckKind,
    CiState,
    PatchState,
    PullRequestState,
)
from domain.identifiers import normalize_sha
from domain.paths import normalize_repository_path


# GitHub 的统一 diff 会受客户端总响应上限保护；单文件保留到这个边界后，
# 后续模型规划器会按 Token 预算继续切片，而不是在上下文阶段直接丢弃文件。
MAX_PATCH_BYTES = 8 * 1024 * 1024


class GitHubContractModel(BaseModel):
    """拒绝未知字段并禁止调用方修改的 GitHub 数据基类。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class PullRequestSnapshot(GitHubContractModel):
    """从 GitHub 重新读取的一份 PR 身份与版本快照。"""

    repository_id: int = Field(gt=0)
    repository: str = Field(
        min_length=3,
        max_length=255,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    pull_request_number: int = Field(gt=0)
    author_login: str | None = Field(default=None, min_length=1, max_length=100)
    html_url: str = Field(min_length=1, max_length=2048)
    head_repository: str | None = Field(
        default=None,
        min_length=3,
        max_length=255,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    head_ref: str = Field(min_length=1, max_length=1024)
    base_repository: str = Field(
        min_length=3,
        max_length=255,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    base_ref: str = Field(min_length=1, max_length=1024)
    base_sha: str = Field(min_length=40, max_length=64)
    head_sha: str = Field(min_length=40, max_length=64)
    state: PullRequestState
    draft: bool
    title: str = Field(min_length=1, max_length=1000)
    changed_files: int = Field(ge=0)
    updated_at: datetime

    @field_validator("base_sha", "head_sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        return normalize_sha(value)

    @field_validator("html_url")
    @classmethod
    def validate_html_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("pull request URL must be an absolute HTTPS URL")
        return value


class PullRequestFile(GitHubContractModel):
    """一个经过路径、大小和状态校验的 PR 变更文件。"""

    path: str = Field(min_length=1, max_length=1024)
    previous_path: str | None = Field(default=None, min_length=1, max_length=1024)
    status: ChangedFileStatus
    blob_sha: str = Field(min_length=40, max_length=64)
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    changes: int = Field(ge=0)
    patch_state: PatchState
    patch: str | None = Field(default=None, max_length=MAX_PATCH_BYTES)

    @field_validator("path", "previous_path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        return normalize_repository_path(value) if value is not None else None

    @field_validator("blob_sha")
    @classmethod
    def validate_blob_sha(cls, value: str) -> str:
        return normalize_sha(value)

    @model_validator(mode="after")
    def validate_patch_state(self) -> "PullRequestFile":
        if self.patch_state is PatchState.AVAILABLE and self.patch is None:
            raise ValueError("available patches must include patch text")
        if self.patch_state is not PatchState.AVAILABLE and self.patch is not None:
            raise ValueError("unavailable patches must not include patch text")
        if self.status is ChangedFileStatus.RENAMED and self.previous_path is None:
            raise ValueError("renamed files must include previous_path")
        return self


class CiCheckSnapshot(GitHubContractModel):
    """一个参与 CI 汇总的 Check Run 或 Commit Status。"""

    kind: CiCheckKind
    external_key: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=500)
    status: str = Field(min_length=1, max_length=50)
    conclusion: str | None = Field(default=None, max_length=50)
    app_id: int | None = Field(default=None, gt=0)


class CiSnapshot(GitHubContractModel):
    """与精确提交绑定、数量有上限的 CI 汇总。"""

    head_sha: str = Field(min_length=40, max_length=64)
    state: CiState
    checks: tuple[CiCheckSnapshot, ...] = Field(max_length=1000)
    complete: bool
    checked_at: datetime

    @field_validator("head_sha")
    @classmethod
    def validate_head_sha(cls, value: str) -> str:
        return normalize_sha(value)


class GitHubReviewContext(GitHubContractModel):
    """Worker 一轮 GitHub 读取产生的完整、可持久化结果。"""

    pull_request: PullRequestSnapshot
    files: tuple[PullRequestFile, ...] | None = None
    files_complete: bool
    diff_complete: bool
    ci: CiSnapshot | None = None
