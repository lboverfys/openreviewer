"""审查管理 publishing 存储职责。"""

from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from domain.enums import ExecutionStatus
from domain.identifiers import normalize_sha
from domain.security import redact_sensitive
from persistence.management.common import (
    _PUBLISH_RECOVERY_AFTER,
    _as_utc,
    _review_change_token,
)
from persistence.management.context import ManagementStorage
from persistence.management.queries import get
from persistence.models import OutboxEventRecord, ReviewRunRecord, ReviewTaskRecord
from persistence.resource_scope import resource_predicate
from services.rbac import ResourceScope
from services.review_management import (
    FindingDecision,
    ReviewActionConflictError,
    ReviewManagementPersistenceError,
    ReviewNotFoundError,
    ReviewPublishUnavailableError,
)


def publish(
    self: ManagementStorage,
    review_run_id: str,
    *,
    actor: str,
    request_id: str,
    state_version: str | None = None,
    head_sha: str | None = None,
    scope: ResourceScope | None = None,
) -> tuple[str, str, ExecutionStatus]:
    """批准后才允许的人工发布；外部 GitHub 调用永远在事务之外。"""

    normalized_request_id = request_id.strip()
    if not normalized_request_id:
        raise ReviewActionConflictError("操作幂等键不能为空")
    request_digest = sha256(normalized_request_id.encode("utf-8")).hexdigest()
    event_key = f"review.publish:{review_run_id}:{request_digest}"
    completed_event_key = f"{event_key}:completed"
    with self._sessions() as session:
        try:
            row = session.execute(
                select(ReviewRunRecord, ReviewTaskRecord)
                .join(
                    ReviewTaskRecord,
                    ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
                )
                .where(
                    ReviewRunRecord.id == review_run_id,
                    resource_predicate(
                        scope,
                        installation_column=ReviewRunRecord.installation_id,
                        repository_column=ReviewRunRecord.repository,
                        repository_key_column=ReviewRunRecord.repository_key,
                    ),
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                raise ReviewNotFoundError("审查任务不存在")
            run, task = row
            if head_sha is not None:
                try:
                    if normalize_sha(head_sha) != run.head_sha:
                        raise ReviewActionConflictError("审查版本已变化，请刷新后重试")
                except ValueError as exc:
                    raise ReviewActionConflictError("head_sha 格式无效") from exc
            if state_version is not None:
                latest_event_id = session.scalar(
                    select(OutboxEventRecord.id)
                    .where(OutboxEventRecord.aggregate_id == review_run_id)
                    .order_by(
                        OutboxEventRecord.occurred_at.desc(),
                        OutboxEventRecord.id.desc(),
                    )
                    .limit(1)
                )
                if state_version != _review_change_token(
                    run.updated_at,
                    task.updated_at,
                    latest_event_id,
                ):
                    raise ReviewActionConflictError("审查状态已变化，请刷新后重试")
            if run.snapshot_review:
                raise ReviewActionConflictError("历史版本复查只在平台内查看，不能发布到 GitHub")
            if self._publisher is None:
                raise ReviewPublishUnavailableError("GitHub 人工发布器尚未配置")
            if run.coverage_status in {"partial", "stale"}:
                raise ReviewActionConflictError(
                    "当前审查覆盖不完整，完成失败节点后才能发布"
                )
            existing_started = session.execute(
                select(OutboxEventRecord.payload).where(
                    OutboxEventRecord.event_key == event_key
                )
            ).scalar_one_or_none()
            existing_completed = session.execute(
                select(OutboxEventRecord.payload).where(
                    OutboxEventRecord.event_key == completed_event_key
                )
            ).scalar_one_or_none()
            if (
                isinstance(existing_completed, dict)
                and existing_completed.get("published") is True
            ):
                return run.id, task.id, ExecutionStatus.COMPLETED
            current = ExecutionStatus(run.workflow_status or run.execution_status)
            now = self._clock()
            if current is ExecutionStatus.PUBLISHING:
                updated_at = _as_utc(run.updated_at)
                if (
                    updated_at is not None
                    and now - updated_at < _PUBLISH_RECOVERY_AFTER
                ):
                    raise ReviewActionConflictError("GitHub 发布正在进行中")
            elif current is not ExecutionStatus.AWAITING_PUBLISH:
                raise ReviewActionConflictError("只有批准后的审查可以发布")
            attempt_token = str(self._uuid_factory())
            run.workflow_status = ExecutionStatus.PUBLISHING.value
            run.publish_attempt_token = attempt_token
            task.workflow_status = ExecutionStatus.PUBLISHING.value
            run.updated_at = now
            task.updated_at = now
            attempt_event_key = (
                event_key
                if existing_started is None
                else f"{event_key}:retry:{attempt_token}"
            )
            session.add(
                OutboxEventRecord(
                    id=str(self._uuid_factory()),
                    event_key=attempt_event_key,
                    aggregate_type="review_run",
                    aggregate_id=review_run_id,
                    event_type="review.manual.publish_started",
                    payload={
                        "actor": actor,
                        "request_id_hash": request_digest,
                        "attempt_token": attempt_token,
                        "recovered": current is ExecutionStatus.PUBLISHING,
                    },
                    occurred_at=now,
                    publish_attempts=0,
                )
            )
            session.commit()
        except (ReviewNotFoundError, ReviewActionConflictError):
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise ReviewManagementPersistenceError(
                "review publish state could not be persisted"
            ) from exc

    try:
        details = get(
            self,
            review_run_id,
            finding_limit=200,
            finding_adjudication_status=FindingDecision.VALID.value,
            scope=scope,
        )
        if details.finding_has_more:
            raise ReviewActionConflictError(
                "有效 Finding 超过 200 条，请先减少发布范围"
            )
        self._publisher(details)
    except ReviewActionConflictError as exc:
        _mark_publish_failed(
            self,
            review_run_id,
            event_key,
            attempt_token,
            actor,
            str(exc),
            scope=scope,
        )
        raise
    except Exception as exc:
        _mark_publish_failed(
            self,
            review_run_id,
            event_key,
            attempt_token,
            actor,
            str(exc),
            scope=scope,
        )
        raise ReviewPublishUnavailableError("GitHub 发布失败，请稍后重试") from exc

    with self._sessions() as session:
        try:
            row = session.execute(
                select(ReviewRunRecord, ReviewTaskRecord)
                .join(
                    ReviewTaskRecord,
                    ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
                )
                .where(
                    ReviewRunRecord.id == review_run_id,
                    resource_predicate(
                        scope,
                        installation_column=ReviewRunRecord.installation_id,
                        repository_column=ReviewRunRecord.repository,
                        repository_key_column=ReviewRunRecord.repository_key,
                    ),
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                raise ReviewNotFoundError("审查任务不存在")
            run, task = row
            completed_event_id = session.scalar(
                select(OutboxEventRecord.id).where(
                    OutboxEventRecord.event_key == completed_event_key
                )
            )
            if completed_event_id is not None:
                return run.id, task.id, ExecutionStatus.COMPLETED
            now = self._clock()
            current_workflow_status = ExecutionStatus(
                run.workflow_status or run.execution_status
            )
            if (
                current_workflow_status is not ExecutionStatus.PUBLISHING
                or run.publish_attempt_token != attempt_token
            ):
                # 外部调用已经返回，但本地状态在调用期间被其他尝试或
                # 新提交替换；绝不能把旧结果写成 completed。令牌匹配时
                # 清掉残留标记，避免管理端误判仍有发布在途。
                if run.publish_attempt_token == attempt_token:
                    run.publish_attempt_token = None
                    session.add(
                        OutboxEventRecord(
                            id=str(self._uuid_factory()),
                            event_key=(
                                f"{completed_event_key}:conflict:{attempt_token}"
                            )[:200],
                            aggregate_type="review_run",
                            aggregate_id=review_run_id,
                            event_type="review.manual.publish_result_conflict",
                            payload={
                                "actor": actor,
                                "attempt_token": attempt_token,
                                "published": True,
                                "current_workflow_status": (
                                    current_workflow_status.value
                                ),
                            },
                            occurred_at=now,
                            publish_attempts=0,
                        )
                    )
                    session.commit()
                else:
                    session.rollback()
                raise ReviewActionConflictError(
                    "发布结果与当前任务状态冲突，请先核对 GitHub 后再处理"
                )
            run.publish_attempt_token = None
            run.workflow_status = ExecutionStatus.COMPLETED.value
            task.workflow_status = ExecutionStatus.COMPLETED.value
            run.updated_at = now
            task.updated_at = now
            session.add(
                OutboxEventRecord(
                    id=str(self._uuid_factory()),
                    event_key=completed_event_key,
                    aggregate_type="review_run",
                    aggregate_id=review_run_id,
                    event_type="review.manual.publish_completed",
                    payload={
                        "actor": actor,
                        "published": True,
                        "attempt_token": attempt_token,
                    },
                    occurred_at=now,
                    publish_attempts=0,
                )
            )
            session.commit()
            return run.id, task.id, ExecutionStatus.COMPLETED
        except ReviewNotFoundError:
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            raise ReviewManagementPersistenceError(
                "review publish result could not be persisted"
            ) from exc


def _mark_publish_failed(
    self: ManagementStorage,
    review_run_id: str,
    event_key: str,
    attempt_token: str,
    actor: str,
    safe_reason: str,
    *,
    scope: ResourceScope | None = None,
) -> None:
    """发布异常只回到待发布，不把外部错误文本原样暴露给客户端。"""

    with self._sessions() as session:
        try:
            row = session.execute(
                select(ReviewRunRecord, ReviewTaskRecord)
                .join(
                    ReviewTaskRecord,
                    ReviewTaskRecord.review_run_id == ReviewRunRecord.id,
                )
                .where(
                    ReviewRunRecord.id == review_run_id,
                    resource_predicate(
                        scope,
                        installation_column=ReviewRunRecord.installation_id,
                        repository_column=ReviewRunRecord.repository,
                        repository_key_column=ReviewRunRecord.repository_key,
                    ),
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                return
            run, task = row
            now = self._clock()
            if (
                ExecutionStatus(run.workflow_status or run.execution_status)
                is not ExecutionStatus.PUBLISHING
                or run.publish_attempt_token != attempt_token
            ):
                return
            run.publish_attempt_token = None
            run.workflow_status = ExecutionStatus.AWAITING_PUBLISH.value
            task.workflow_status = ExecutionStatus.AWAITING_PUBLISH.value
            run.updated_at = now
            task.updated_at = now
            session.add(
                OutboxEventRecord(
                    id=str(self._uuid_factory()),
                    event_key=f"{event_key}:failed:{attempt_token}"[:200],
                    aggregate_type="review_run",
                    aggregate_id=review_run_id,
                    event_type="review.manual.publish_failed",
                    payload={
                        "actor": actor,
                        "attempt_token": attempt_token,
                        "error_message": redact_sensitive(safe_reason)[:300],
                    },
                    occurred_at=now,
                    publish_attempts=0,
                )
            )
            session.commit()
        except SQLAlchemyError:
            session.rollback()
