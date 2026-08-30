"""外部副作用的持久化审计接口。

该模块只定义服务层协议，具体事务实现位于 ``persistence``，避免 GitHub
适配器直接依赖 ORM。HTTP 调用本身永远在协议方法之外执行。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from services.github import GitHubApiResult, GitHubCallAudit


class ExternalActionBusyError(RuntimeError):
    """同一个幂等动作仍由另一个发布尝试持有租约。"""


class ExternalActionStore(Protocol):
    """记录一次外部动作的领取、成功和失败状态。"""

    def acquire(
        self,
        *,
        action_key: str,
        review_run_id: str,
        action_type: str,
        owner: str,
        request_method: str,
        request_path: str,
    ) -> str | None:
        """在执行 HTTP 请求前取得动作租约。

        返回已成功动作的远端 ID 时，调用方必须跳过重复 HTTP 请求；返回
        ``None`` 表示本次调用已取得租约，需要继续执行外部请求。
        """

    def succeed(
        self,
        *,
        action_key: str,
        owner: str,
        remote_id: str,
        audit: GitHubCallAudit,
    ) -> None:
        """保存远端成功对象和调用审计。"""

    def fail(
        self,
        *,
        action_key: str,
        owner: str,
        error_code: str,
        error_message: str,
        retryable: bool,
        details: Mapping[str, object] | None = None,
        audit: GitHubCallAudit | None = None,
    ) -> None:
        """保存失败状态；错误内容必须已经是安全文本。"""


def remote_id_from_result(result: GitHubApiResult, target: str) -> str:
    """从 GitHub 成功响应提取并校验远端对象 ID。"""

    from domain.security import ErrorCode, SafeApplicationError, SafeError

    payload = result.payload
    if isinstance(payload, dict) and isinstance(payload.get("id"), int):
        return str(payload["id"])
    raise SafeApplicationError(
        SafeError(
            code=ErrorCode.GITHUB_INVALID_RESPONSE,
            safe_message=f"GitHub {target}发布返回了无效结果",
            retryable=False,
            details={"status_code": result.audit.response_status},
        )
    )
