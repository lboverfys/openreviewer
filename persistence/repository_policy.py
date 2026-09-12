"""在创建运行的同一事务内读取仓库策略，避免执行中漂移。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.repository_policy import (
    RepositoryPolicyDeniedError,
    RepositoryPolicySnapshot,
)
from persistence.models import RepositoryPolicyRecord


def repository_policy_snapshot(
    session: Session, repository: str
) -> dict[str, object] | None:
    # 唯一键 repository_key 精确查询；每次任务固定一次，不随文件数增长。
    row = session.execute(
        select(
            RepositoryPolicyRecord.repository,
            RepositoryPolicyRecord.revision,
            RepositoryPolicyRecord.policy,
        ).where(RepositoryPolicyRecord.repository_key == repository.casefold())
    ).one_or_none()
    if row is None:
        return None
    snapshot = RepositoryPolicySnapshot(
        repository=row.repository, revision=row.revision, **row.policy
    )
    if not snapshot.enabled:
        raise RepositoryPolicyDeniedError("该仓库已暂停接收新审查，请在团队管理中启用")
    return snapshot.model_dump(mode="json")
