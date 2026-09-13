"""仓库外发策略读取与无正文的拒绝审计；不持有跨 HTTP 的事务。"""

from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from inspect import signature

from sqlalchemy import select

from domain.repository_policy import RepositoryPolicy
from persistence.models import RepositoryPolicyRecord
from persistence.platform_common import platform_audit
from services.egress import egress_scope


@contextmanager
def repository_egress(sessions, repository: str, object_id: str = "egress"):
    with sessions() as session:
        payload = session.scalar(select(RepositoryPolicyRecord.policy).where(
            RepositoryPolicyRecord.repository_key == repository.casefold(),
        ))
    policy = RepositoryPolicy.model_validate(payload or {}).egress

    def record(details):
        with sessions() as session, session.begin():
            platform_audit(session, "platform.egress.denied", object_id, repository,
                           "system", datetime.now(UTC), details=details)

    with egress_scope(policy, record):
        yield


def index_egress(method):
    """检索和评测各读取一次仓库策略；内部候选/策略循环不重复查库。"""
    parameters = signature(method)

    @wraps(method)
    def guarded(self, *args, **kwargs):
        values = parameters.bind(self, *args, **kwargs).arguments
        index = self.repository.get(values["index_id"], values.get("scope"))
        with repository_egress(self.repository.sessions, index.repository, index.id):
            return method(self, *args, **kwargs)

    return guarded
