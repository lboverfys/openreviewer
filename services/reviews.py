"""接受异步审查请求的应用服务。"""

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from domain.enums import ExecutionStatus
from domain.models import ReviewRequest


class IdempotencyConflictError(ValueError):
    """同一个幂等键被复用于不同的请求内容。"""


class ReviewPersistenceError(RuntimeError):
    """审查请求无法持久化保存。"""


@dataclass(frozen=True, slots=True)
class ReviewSubmissionResult:
    review_run_id: str
    review_task_id: str
    review_version_key: str
    execution_status: ExecutionStatus
    accepted_at: datetime
    created: bool


class ReviewRepository(Protocol):
    """审查提交用例使用的持久化边界。"""

    def create_or_get(
        self,
        request: ReviewRequest,
        idempotency_key: str,
        request_fingerprint: str,
        actor: str = "system",
    ) -> ReviewSubmissionResult:
        """按幂等键创建任务，或返回已经存在的同一请求结果。

        具体实现负责在一个事务中写入审查运行、任务和 Outbox 事件，并在并发
        请求撞上唯一约束时重新读取已有记录。应用服务只依赖这个协议，不关心
        底层是 PostgreSQL 还是测试用的其他数据库。

        参数：
            request: 已通过 Pydantic 契约校验的审查请求。
            idempotency_key: 去除首尾空白、长度不超过 200 的调用幂等键。
            request_fingerprint: 规范请求体的 SHA-256 十六进制摘要。

        返回：
            新建记录或已有记录对应的 ``ReviewSubmissionResult``；``created``
            字段用于区分两种情况。

        异常：
            IdempotencyConflictError: 同一键已绑定不同请求指纹。
            ReviewPersistenceError: 无法原子写入或读取持久化数据。
        """
        ...


class ReviewService:
    def __init__(self, repository: ReviewRepository) -> None:
        """保存任务提交所需的持久化适配器。

        ``repository`` 通过协议注入，便于把业务规则和数据库细节分开，也便于
        单元测试使用内存替身。

        参数：
            repository: 负责幂等查询和事务写入的持久化边界。

        构造函数不访问数据库；真正的持久化只会在 :meth:`submit` 中发生。
        """
        self._repository = repository

    def submit(
        self,
        request: ReviewRequest,
        idempotency_key: str,
        *,
        actor: str = "system",
    ) -> ReviewSubmissionResult:
        """校验幂等键并计算请求指纹，然后提交到持久化层。

        请求体会以排序后的 JSON 形式序列化，再计算 SHA-256 指纹。这样同一幂等
        键重复提交时可以判断内容是否完全一致：一致则安全返回旧任务，不一致则
        由仓储层报告冲突。这个方法本身不执行审查，只负责可靠地接受异步任务。

        参数：
            request: 已验证且字符串已清理的内部审查请求。
            idempotency_key: 调用方为一次逻辑提交生成的稳定键；首尾空白会被忽略。

        返回：
            仓储返回的任务接受结果。新请求初始状态为 ``queued``；重复请求可能
            返回该运行已经推进到的当前状态。

        异常：
            ValueError: 幂等键清理后为空或超过 200 个字符。
            IdempotencyConflictError: 同一幂等键被用于不同的规范请求体。
            ReviewPersistenceError: 持久化层无法保证读取或写入结果。

        指纹只覆盖请求体，不包含幂等键本身。JSON 键排序和紧凑分隔符保证相同
        结构不会因字段顺序或空白差异产生不同摘要。
        """
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
            actor,
        )
