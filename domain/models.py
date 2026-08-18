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
        """规范化 ``ReviewVersion`` 中的 base/head SHA。

        参数：
            value: Pydantic 正在校验的 ``base_sha`` 或 ``head_sha`` 原始文本。

        返回：
            由 :func:`normalize_sha` 清理后的小写完整 SHA。

        异常：
            ValueError: SHA 不是 40 到 64 位十六进制字符串。Pydantic 会把它包装
            进字段校验错误，并阻止创建无效的 ``ReviewVersion``。
        """
        return normalize_sha(value)

    @property
    def review_version_key(self) -> str:
        """计算当前 PR 提交版本的稳定键。

        返回：
            由仓库数字 ID、PR 编号和已经规范化的 ``head_sha`` 组成的字符串。

        该属性每次读取时即时计算，不在对象内重复存储，因此不会与字段内容失去同步。
        """
        return build_review_version_key(
            self.repository_id,
            self.pull_request_number,
            self.head_sha,
        )

    def is_current(self, current_head_sha: str) -> bool:
        """判断该版本是否仍对应调用方看到的 PR 最新提交。

        参数：
            current_head_sha: 从 GitHub 等外部来源刚读取到的 PR head SHA。

        返回：
            外部 SHA 规范化后与本对象 ``head_sha`` 完全相同时返回 ``True``。

        异常：
            ValueError: ``current_head_sha`` 格式非法。非法输入不会被当成“旧提交”。
        """
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
        """校验并规范化 Webhook 携带的 head SHA。

        参数：
            value: 从已验签 Webhook 负载提取出的提交 SHA。

        返回：
            可用于版本键和数据库比较的小写完整 SHA。

        异常：
            ValueError: 值不是 40 到 64 位十六进制字符串。
        """
        return normalize_sha(value)

    @property
    def review_version_key(self) -> str:
        """返回该 Webhook 所指向 PR 提交的稳定版本键。

        返回：
            ``repository_id``、``pull_request_number`` 和 ``head_sha`` 的组合键。

        该键描述的是“哪一个提交版本”，不包含 Delivery ID，因此重复投递不会
        产生不同版本键。
        """
        return build_review_version_key(
            self.repository_id,
            self.pull_request_number,
            self.head_sha,
        )

    @property
    def deduplication_key(self) -> str:
        """返回 GitHub Delivery ID，供持久化层识别重复投递。

        返回：
            构造模型时提供的 ``delivery_id`` 原值（仅经过基类的首尾空白清理）。

        这里不访问数据库也不执行去重；真正的唯一约束属于后续 Webhook 持久化层。
        """
        return self.delivery_id


class ReviewRequest(ContractModel):
    """Internal request for one asynchronous pull-request review."""

    installation_id: int = Field(gt=0)
    repository_id: int = Field(gt=0)
    repository: str = Field(
        min_length=3,
        max_length=255,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    pull_request_number: int = Field(gt=0)
    head_sha: str = Field(min_length=40, max_length=64)

    @field_validator("head_sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        """校验并规范化内部任务创建请求中的 head SHA。

        参数：
            value: 管理 API 请求体提供的 SHA 文本。

        返回：
            去掉首尾空白并转为小写的完整 SHA。

        异常：
            ValueError: SHA 长度或字符集合不符合契约；API 最终会返回 422。
        """
        return normalize_sha(value)

    @property
    def review_version_key(self) -> str:
        """计算内部任务请求对应的稳定审查版本键。

        返回：
            ``<repository_id>:<pull_request_number>:<head_sha>`` 形式的字符串。

        幂等键用于识别一次调用，版本键用于识别被审查提交；两者用途不同，
        同一版本可以使用不同幂等键显式创建多次运行。
        """
        return build_review_version_key(
            self.repository_id,
            self.pull_request_number,
            self.head_sha,
        )


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
        """规范化可选的 Git blob SHA。

        参数：
            value: 文件对象 SHA；``None`` 表示调用方没有提供 blob 身份。

        返回：
            ``None``，或经过统一格式校验的小写 SHA。

        异常：
            ValueError: 非空值不是合法的 40 到 64 位十六进制字符串。
        """
        return normalize_sha(value) if value is not None else None

    @field_validator("file")
    @classmethod
    def validate_relative_file(cls, value: str) -> str:
        """规范化 Finding 文件路径并拦截已知的目录穿越形式。

        Finding 的文件路径来自模型或外部适配器，必须限制在仓库根目录内，避免
        后续读取代码或生成评论时误触宿主机文件。当前实现把反斜杠换成正斜杠，
        拒绝以 ``/`` 开头的路径和任意 ``..`` 路径段；它尚未单独识别
        ``C:/...`` 形式的 Windows 盘符绝对路径。

        参数：
            value: Finding 中的原始文件路径。

        返回：
            使用正斜杠的路径文本，便于跨平台比较和生成 GitHub 定位。

        异常：
            ValueError: 路径以 ``/`` 开头或包含完整的 ``..`` 路径段。
        """
        normalized = value.replace("\\", "/")
        parts = normalized.split("/")
        if normalized.startswith("/") or ".." in parts:
            raise ValueError("file must be a repository-relative path")
        return normalized

    @model_validator(mode="after")
    def validate_line_range(self) -> Self:
        """校验 Finding 的起止行号顺序。

        返回：
            校验通过后的当前模型，供 Pydantic 继续完成对象构造。

        异常：
            ValueError: ``end_line`` 小于 ``start_line``。两个字段大于零的约束由
            各自的 ``Field`` 定义负责，这里只检查跨字段关系。
        """
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
        """校验 Finding 所绑定的提交 SHA。

        参数：
            value: 产生该问题时使用的 PR head SHA。

        返回：
            规范化后的小写 SHA，供发布前与 PR 当前提交进行精确比较。

        异常：
            ValueError: SHA 格式不符合契约。
        """
        return normalize_sha(value)

    def can_publish_inline(
        self,
        current_head_sha: str,
        confidence_threshold: float = 0.90,
    ) -> bool:
        """判断 Finding 是否满足发布行内评论的全部安全门槛。

        必须同时满足：已被复核、存在定位、定位在当前 diff 的新增侧、提交没有
        过期、置信度达到阈值，并且不是普通测试缺口。返回 ``True`` 只表示“可以
        作为候选”，最终是否调用 GitHub 评论 API 仍应由策略层决定。

        参数：
            current_head_sha: 发布前从 PR 重新读取的最新 head SHA，用于阻止旧
                提交上的结果覆盖新提交。
            confidence_threshold: 仓库策略要求的最低置信度，必须位于 0 到 1；
                默认值为 0.90。

        返回：
            所有确定性门槛同时满足时返回 ``True``，任一门槛不满足就返回
            ``False``。该方法没有网络或数据库副作用。

        异常：
            ValueError: 阈值越界，或 ``current_head_sha`` 不是合法完整 SHA。
        """

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
        """统一覆盖统计文件的路径格式并拦截目录穿越。

        参数：
            value: changed file 或覆盖统计项中的原始路径。

        返回：
            把反斜杠替换为正斜杠后的路径。

        异常：
            ValueError: 路径以 ``/`` 开头或包含 ``..`` 段。与 Finding 路径校验
            一样，当前实现尚未专门拒绝 Windows 盘符形式。
        """
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
        """阻止没有审查结论的运行被标记为已完成。

        ``completed`` 代表可以对外展示最终审查结果；如果模型失败、CI 缺失或
        覆盖不完整，应使用其他明确状态，而不能把空结论当成“审查通过”。

        返回：
            当前 ``ReviewRunState``；Pydantic 会继续返回构造完成的模型。

        异常：
            ValueError: ``execution_status`` 已是 ``completed``，但
            ``review_conclusion`` 仍为 ``None``。其他执行状态允许暂时没有结论。
        """
        if (
            self.execution_status is ExecutionStatus.COMPLETED
            and self.review_conclusion is None
        ):
            raise ValueError("completed review runs must have a conclusion")
        return self
