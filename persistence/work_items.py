"""人工待办与审批投影；所有来源和写入均应用仓库范围。"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ExecutionStatus
from domain.pagination import CursorPage, encode_cursor
from domain.platform import (
    ApprovalTodo,
    PlatformConflictError,
    PlatformNotFoundError,
    WorkItemCreate,
    WorkItemUpdate,
    WorkItemView,
)
from domain.security import redact_sensitive
from persistence.models import FindingWorkItemRecord as Work
from persistence.models import ReviewFindingRecord, ReviewRunRecord, TeamMemberRecord
from persistence.pagination import apply_cursor
from persistence.platform_common import dialect_insert, platform_audit
from persistence.resource_scope import resource_predicate
from services.rbac import AccessRole, Permission, ResourceScope, has_permission


def work_scope(model, scope: ResourceScope):
    return resource_predicate(
        scope,
        installation_column=model.installation_id,
        repository_column=model.repository,
        repository_key_column=model.repository_key,
    )


class WorkItemRepository:
    def __init__(self, sessions: sessionmaker[Session], configured_administrator: str):
        self.sessions, self.administrator = (
            sessions,
            configured_administrator.casefold(),
        )

    def _assignee(
        self,
        session: Session,
        username: str | None,
        installation_id: int,
        repository: str,
    ):
        if username is None:
            return None
        username = username.strip().casefold()
        if username == self.administrator:
            return username
        member = session.execute(
            select(TeamMemberRecord.role, TeamMemberRecord.resource_scope).where(
                TeamMemberRecord.username_key == username,
                TeamMemberRecord.enabled.is_(True),
            )
        ).one_or_none()
        if member is None or not has_permission(
            AccessRole(member.role), Permission.ADJUDICATE_FINDINGS
        ):
            raise ValueError("负责人不存在、已停用或没有问题处理权限")
        scope = ResourceScope.from_mapping(member.resource_scope)
        if not scope.allows(installation_id, repository):
            raise ValueError("负责人没有该仓库的访问权限")
        return username

    def create(
        self, draft: WorkItemCreate, actor: str, scope: ResourceScope
    ) -> WorkItemView:
        now = datetime.now(UTC)
        with self.sessions() as session, session.begin():
            row = session.execute(
                select(
                    ReviewFindingRecord.id,
                    ReviewFindingRecord.review_run_id,
                    ReviewFindingRecord.title,
                    ReviewFindingRecord.severity,
                    ReviewRunRecord.installation_id,
                    ReviewRunRecord.repository,
                    ReviewRunRecord.repository_key,
                    ReviewRunRecord.pull_request_number,
                )
                .join(
                    ReviewRunRecord,
                    ReviewRunRecord.id == ReviewFindingRecord.review_run_id,
                )
                .where(
                    ReviewFindingRecord.id == draft.finding_id,
                    work_scope(ReviewRunRecord, scope),
                )
            ).one_or_none()
            if row is None:
                raise PlatformNotFoundError("审查问题不存在")
            assignee = self._assignee(
                session, draft.assignee, row.installation_id, row.repository
            )
            identifier = str(uuid4())
            created = session.scalar(
                dialect_insert(session, Work)
                .values(
                    id=identifier,
                    source_run_id=row.review_run_id,
                    source_finding_id=row.id,
                    installation_id=row.installation_id,
                    repository=row.repository,
                    repository_key=row.repository_key,
                    pull_request_number=row.pull_request_number,
                    title=row.title,
                    severity=row.severity,
                    status="open",
                    assignee=assignee,
                    due_at=draft.due_at,
                    note="",
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["source_finding_id"])
                .returning(Work.id)
            )
            if created:
                platform_audit(
                    session,
                    "platform.work.created",
                    identifier,
                    row.repository,
                    actor,
                    now,
                    revision=1,
                    details={"installation_id": row.installation_id},
                )
            result = (
                session.execute(
                    select(
                        *(getattr(Work, name) for name in WorkItemView.model_fields)
                    ).where(
                        Work.source_finding_id == row.id,
                        work_scope(Work, scope),
                    )
                )
                .mappings()
                .one()
            )
            return WorkItemView.model_validate(result)

    def update(
        self, identifier: str, draft: WorkItemUpdate, actor: str, scope: ResourceScope
    ) -> WorkItemView:
        now = datetime.now(UTC)
        with self.sessions() as session, session.begin():
            target = session.execute(
                select(Work.installation_id, Work.repository).where(
                    Work.id == identifier,
                    work_scope(Work, scope),
                )
            ).one_or_none()
            if target is None:
                raise PlatformNotFoundError("工作项不存在")
            assignee = self._assignee(
                session, draft.assignee, target.installation_id, target.repository
            )
            values = draft.model_dump(exclude={"expected_revision", "assignee"})
            values.update(
                assignee=assignee,
                revision=draft.expected_revision + 1,
                updated_at=now,
                note=str(redact_sensitive(draft.note)),
            )
            row = (
                session.execute(
                    update(Work)
                    .where(
                        Work.id == identifier,
                        Work.revision == draft.expected_revision,
                        work_scope(Work, scope),
                    )
                    .values(**values)
                    .returning(
                        *(getattr(Work, name) for name in WorkItemView.model_fields)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise PlatformConflictError("工作项已被其他成员修改，请刷新后重试")
            platform_audit(
                session,
                "platform.work.updated",
                identifier,
                target.repository,
                actor,
                now,
                revision=draft.expected_revision + 1,
                details={
                    "installation_id": target.installation_id,
                    "status": draft.status,
                    "assignee": assignee,
                    "fix_pull_request_number": draft.fix_pull_request_number,
                },
            )
            return WorkItemView.model_validate(row)

    def learning_source(
        self, identifier: str, revision: int, scope: ResourceScope
    ) -> WorkItemView:
        with self.sessions() as session:
            row = (
                session.execute(
                    select(
                        *(getattr(Work, name) for name in WorkItemView.model_fields)
                    ).where(
                        Work.id == identifier,
                        work_scope(Work, scope),
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise PlatformNotFoundError("工作项不存在")
        if row["revision"] != revision:
            raise PlatformConflictError("工作项已变化，请刷新后整理经验")
        return WorkItemView.model_validate(row)

    def list(
        self,
        scope: ResourceScope,
        *,
        assignee: str | None = None,
        status: str | None = None,
        overdue: bool = False,
        limit: int = 10,
        cursor: str | None = None,
    ) -> CursorPage[WorkItemView]:
        statement = select(
            *(getattr(Work, name) for name in WorkItemView.model_fields)
        ).where(work_scope(Work, scope))
        if assignee:
            statement = statement.where(Work.assignee == assignee.casefold())
        if status:
            statement = statement.where(Work.status == status)
        if overdue:
            statement = statement.where(
                Work.status.in_(("open", "in_progress")),
                Work.due_at < datetime.now(UTC),
            )
        statement = apply_cursor(statement, Work.created_at, Work.id, cursor).limit(
            limit + 1
        )
        with self.sessions() as session:
            rows = session.execute(statement).mappings().all()
        items = tuple(WorkItemView.model_validate(row) for row in rows[:limit])
        return CursorPage(
            items=items,
            next_cursor=encode_cursor(items[-1].created_at, items[-1].id)
            if len(rows) > limit and items
            else None,
        )

    def approvals(
        self,
        scope: ResourceScope,
        actor: str,
        *,
        mine: bool = True,
        overdue: bool = False,
        limit: int = 10,
        cursor: str | None = None,
    ) -> CursorPage[ApprovalTodo]:
        run = ReviewRunRecord
        legacy_assignee = func.lower(run.repository_policy["approver"].as_string())
        statement = select(
            run.id,
            run.repository,
            run.pull_request_number,
            func.coalesce(run.approval_assignee, legacy_assignee).label("assignee"),
            run.approval_requested_at.label("requested_at"),
            run.approval_due_at.label("due_at"),
            run.created_at,
        ).where(
            run.workflow_status == ExecutionStatus.AWAITING_APPROVAL.value,
            work_scope(run, scope),
        )
        if mine:
            statement = statement.where(
                or_(
                    run.approval_assignee == actor.casefold(),
                    and_(
                        run.approval_assignee.is_(None),
                        or_(
                            legacy_assignee == actor.casefold(),
                            legacy_assignee.is_(None),
                        ),
                    ),
                )
            )
        if overdue:
            statement = statement.where(run.approval_due_at < datetime.now(UTC))
        statement = apply_cursor(statement, run.created_at, run.id, cursor).limit(
            limit + 1
        )
        with self.sessions() as session:
            rows = session.execute(statement).mappings().all()
        items = tuple(ApprovalTodo.model_validate(row) for row in rows[:limit])
        return CursorPage(
            items=items,
            next_cursor=encode_cursor(items[-1].created_at, items[-1].id)
            if len(rows) > limit and items
            else None,
        )
