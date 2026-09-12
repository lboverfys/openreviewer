"""审查管理 identity 存储职责。"""

from hashlib import sha256

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from domain.github import PullRequestSnapshot
from persistence.management.context import ManagementStorage
from persistence.models import (
    GitHubInstallationRecord,
    OutboxEventRecord,
    PullRequestVersionRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.resource_scope import resource_predicate
from services.rbac import ResourceScope
from services.review_management import (
    ReviewIdentitySyncConflictError,
    ReviewManagementPersistenceError,
    ReviewNotFoundError,
)


def save_pull_request_identity(
    self: ManagementStorage,
    review_run_id: str,
    snapshot: PullRequestSnapshot,
    *,
    actor: str,
    request_id: str,
    scope: ResourceScope | None = None,
) -> None:
    """以幂等短事务保存 GitHub PR 作者、链接和分支信息。"""

    normalized_request_id = request_id.strip()
    if not normalized_request_id:
        raise ReviewIdentitySyncConflictError("操作幂等键不能为空")
    digest = sha256(normalized_request_id.encode("utf-8")).hexdigest()
    event_key = f"review.identity.sync:{review_run_id}:{digest}"
    with self._sessions() as session:
        try:
            existing = session.scalar(
                select(OutboxEventRecord.id)
                .join(
                    ReviewRunRecord,
                    and_(
                        OutboxEventRecord.aggregate_type == "review_run",
                        OutboxEventRecord.aggregate_id == ReviewRunRecord.id,
                    ),
                )
                .where(
                    OutboxEventRecord.event_key == event_key,
                    resource_predicate(
                        scope,
                        installation_column=ReviewRunRecord.installation_id,
                        repository_column=ReviewRunRecord.repository,
                        repository_key_column=ReviewRunRecord.repository_key,
                    ),
                )
            )
            if existing is not None:
                return
            row = session.execute(
                select(ReviewRunRecord, ReviewTaskRecord)
                .join(
                    ReviewTaskRecord,
                    ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
                )
                .where(ReviewRunRecord.id == review_run_id)
                .where(
                    resource_predicate(
                        scope,
                        installation_column=ReviewRunRecord.installation_id,
                        repository_column=ReviewRunRecord.repository,
                        repository_key_column=ReviewRunRecord.repository_key,
                    )
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                raise ReviewNotFoundError("审查任务不存在")
            run, _task = row
            if (
                snapshot.repository_id != run.repository_id
                or snapshot.repository != run.repository
                or snapshot.pull_request_number != run.pull_request_number
            ):
                raise ReviewIdentitySyncConflictError("GitHub PR 身份与任务不一致")
            now = self._clock()
            version = session.scalar(
                select(PullRequestVersionRecord)
                .where(
                    PullRequestVersionRecord.review_version_key
                    == run.review_version_key
                )
                .with_for_update()
            )
            if version is not None and version.identity_fetched_at is not None:
                return
            if version is None:
                installation = session.get(
                    GitHubInstallationRecord,
                    run.installation_id,
                )
                if installation is None:
                    session.add(
                        GitHubInstallationRecord(
                            id=run.installation_id,
                            created_at=now,
                            last_seen_at=now,
                        )
                    )
                    session.flush()
                else:
                    installation.last_seen_at = now
                version = PullRequestVersionRecord(
                    id=str(self._uuid_factory()),
                    review_version_key=run.review_version_key,
                    installation_id=run.installation_id,
                    repository_id=run.repository_id,
                    repository=run.repository,
                    pull_request_number=run.pull_request_number,
                    head_sha=run.head_sha,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(version)
                session.flush()
            version.author_login = snapshot.author_login
            version.html_url = snapshot.html_url
            version.head_repository = snapshot.head_repository
            version.head_ref = snapshot.head_ref
            version.base_repository = snapshot.base_repository
            version.base_ref = snapshot.base_ref
            version.identity_fetched_at = now
            version.last_seen_at = now
            session.add(
                OutboxEventRecord(
                    id=str(self._uuid_factory()),
                    event_key=event_key,
                    aggregate_type="review_run",
                    aggregate_id=review_run_id,
                    event_type="review.github.identity_synced",
                    payload={
                        "actor": actor,
                        "author_login": snapshot.author_login,
                        "html_url": snapshot.html_url,
                        "head_repository": snapshot.head_repository,
                        "head_ref": snapshot.head_ref,
                        "base_repository": snapshot.base_repository,
                        "base_ref": snapshot.base_ref,
                    },
                    occurred_at=now,
                    publish_attempts=0,
                )
            )
            session.commit()
        except (ReviewNotFoundError, ReviewIdentitySyncConflictError):
            session.rollback()
            raise
        except IntegrityError as exc:
            session.rollback()
            # 同一版本的另一个运行可能并发创建版本行；只要对方已经完成身份
            # 同步，本次请求的业务结果也已达成。其他约束冲突仍作为持久化
            # 故障返回，不能把未知 IntegrityError 伪装成成功。
            recovered = session.execute(
                select(
                    OutboxEventRecord.id,
                    PullRequestVersionRecord.identity_fetched_at,
                )
                .select_from(ReviewRunRecord)
                .outerjoin(
                    PullRequestVersionRecord,
                    PullRequestVersionRecord.review_version_key
                    == ReviewRunRecord.review_version_key,
                )
                .outerjoin(
                    OutboxEventRecord,
                    OutboxEventRecord.event_key == event_key,
                )
                .where(ReviewRunRecord.id == review_run_id)
            ).one_or_none()
            if recovered is not None and (
                recovered.id is not None or recovered.identity_fetched_at is not None
            ):
                return
            raise ReviewManagementPersistenceError(
                "review identity could not be saved"
            ) from exc
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            session.rollback()
            raise ReviewManagementPersistenceError(
                "review identity could not be saved"
            ) from exc
