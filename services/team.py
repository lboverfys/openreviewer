"""单团队的成员、仓库策略和变更审计。"""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from argon2 import PasswordHasher
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from domain.pagination import CursorPage, decode_cursor, encode_cursor
from domain.repository_policy import RepositoryPolicy
from persistence.models import (
    AdminSessionRecord,
    KnowledgeDocumentRecord,
    OutboxEventRecord,
    RepositoryPolicyRecord,
    TeamMemberRecord,
)
from services.rbac import AccessRole, Permission, ResourceScope, has_permission


class TeamConflictError(ValueError):
    pass


class MemberScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_ids: tuple[int, ...] = Field(default=(), max_length=1000)
    organizations: tuple[str, ...] = Field(default=(), max_length=1000)
    repositories: tuple[str, ...] = Field(default=(), max_length=1000)
    unrestricted: bool = False

    def resource_scope(self) -> ResourceScope:
        scope = ResourceScope.from_mapping(self.model_dump(exclude={"unrestricted"}))
        if self.unrestricted:
            return ResourceScope(
                unrestricted=True, installation_ids=scope.installation_ids,
                organizations=scope.organizations, repositories=scope.repositories,
            )
        return scope


class MemberWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    role: AccessRole
    enabled: bool = True
    scope: MemberScope = Field(default_factory=MemberScope)
    password: SecretStr | None = None

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not 12 <= len(value.get_secret_value()) <= 128:
            raise ValueError("密码长度应为 12 至 128 个字符")
        return value


class MemberView(BaseModel):
    username: str
    role: AccessRole
    enabled: bool
    scope: MemberScope
    revision: int
    created_at: datetime
    updated_at: datetime
    updated_by: str


class MemberPage(CursorPage[MemberView]):
    configured_administrator: str


class RepositoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    repository: str = Field(
        min_length=3, max_length=255, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
    )
    policy: RepositoryPolicy = Field(default_factory=RepositoryPolicy)


class RepositoryView(BaseModel):
    id: str
    repository: str
    policy: RepositoryPolicy
    revision: int
    created_at: datetime
    updated_at: datetime
    updated_by: str


class TeamAuditView(BaseModel):
    id: str
    event_type: str
    payload: dict[str, object]
    occurred_at: datetime


_MEMBER_COLUMNS = (
    TeamMemberRecord.username, TeamMemberRecord.role, TeamMemberRecord.enabled,
    TeamMemberRecord.resource_scope.label("scope"), TeamMemberRecord.revision,
    TeamMemberRecord.created_at, TeamMemberRecord.updated_at, TeamMemberRecord.updated_by,
)
_REPOSITORY_COLUMNS = (
    RepositoryPolicyRecord.id, RepositoryPolicyRecord.repository,
    RepositoryPolicyRecord.policy, RepositoryPolicyRecord.revision,
    RepositoryPolicyRecord.created_at, RepositoryPolicyRecord.updated_at,
    RepositoryPolicyRecord.updated_by,
)


def _page_after(statement, time_column, id_column, cursor: str | None):
    if cursor:
        created_at, identifier = decode_cursor(cursor)
        statement = statement.where(or_(
            time_column < created_at,
            and_(time_column == created_at, id_column < identifier),
        ))
    return statement.order_by(time_column.desc(), id_column.desc())


class TeamService:
    def __init__(
        self, sessions: sessionmaker[Session], configured_administrator: str,
        *, password_hasher: PasswordHasher | None = None,
    ) -> None:
        self.sessions = sessions
        self.configured_administrator = configured_administrator
        self._hasher = password_hasher or PasswordHasher()

    def members(self, limit: int = 10, cursor: str | None = None) -> MemberPage:
        statement = _page_after(
            select(*_MEMBER_COLUMNS), TeamMemberRecord.created_at,
            TeamMemberRecord.username, cursor,
        ).limit(limit + 1)
        with self.sessions() as session:
            rows = session.execute(statement).mappings().all()
        items = tuple(MemberView.model_validate(row) for row in rows[:limit])
        return MemberPage(
            items=items, configured_administrator=self.configured_administrator,
            next_cursor=(
                encode_cursor(items[-1].created_at, items[-1].username)
                if len(rows) > limit and items else None
            ),
        )

    def save_member(self, username: str, draft: MemberWrite, actor: str) -> MemberView:
        if (
            not username or len(username) > 100 or not username.isascii()
            or any(not (character.isalnum() or character in "_.@-") for character in username)
        ):
            raise ValueError("用户名仅支持英文字母、数字和 _.@-")
        if username.casefold() == self.configured_administrator.casefold():
            raise ValueError("配置管理员由部署配置维护，不能在这里修改")
        scope = draft.scope.resource_scope()
        if scope.unrestricted and draft.role is not AccessRole.ADMINISTRATOR:
            raise ValueError("只有管理员可以获得全部资源权限")
        # 密码计算在事务外完成；数据库只接收哈希。
        password_hash = (
            self._hasher.hash(draft.password.get_secret_value())
            if draft.password is not None else None
        )
        now = datetime.now(UTC)
        with self.sessions() as session:
            existing = session.execute(select(
                TeamMemberRecord.username, TeamMemberRecord.revision,
            ).where(TeamMemberRecord.username_key == username.casefold())).one_or_none()
            values = {
                "role": draft.role.value, "enabled": draft.enabled,
                "resource_scope": {
                    "installation_ids": sorted(scope.installation_ids),
                    "organizations": sorted(scope.organizations),
                    "repositories": sorted(scope.repositories),
                    "unrestricted": scope.unrestricted,
                },
                "revision": draft.expected_revision + 1,
                "updated_at": now, "updated_by": actor,
            }
            if password_hash is not None:
                values["password_hash"] = password_hash
            if existing is None:
                if draft.expected_revision != 0:
                    raise TeamConflictError("成员已发生变化，请刷新后重试")
                if password_hash is None:
                    raise ValueError("新增成员必须设置密码")
                session.add(TeamMemberRecord(
                    username=username, username_key=username.casefold(),
                    created_at=now, **values,
                ))
                session.flush()
            else:
                username = existing.username
                result = session.execute(update(TeamMemberRecord).where(
                    TeamMemberRecord.username == username,
                    TeamMemberRecord.revision == draft.expected_revision,
                ).values(**values).returning(TeamMemberRecord.username))
                if result.scalar_one_or_none() is None:
                    raise TeamConflictError("成员已被其他管理员修改，请刷新后重试")
                session.execute(update(AdminSessionRecord).where(
                    AdminSessionRecord.username == username,
                    AdminSessionRecord.revoked_at.is_(None),
                ).values(revoked_at=now))
            self._audit(
                session, "team.member.updated", username, actor, now,
                {**{key: value for key, value in values.items()
                    if key in {"role", "enabled", "resource_scope", "revision"}},
                 "password_changed": password_hash is not None},
            )
            row = session.execute(select(*_MEMBER_COLUMNS).where(
                TeamMemberRecord.username == username,
            )).mappings().one()
            view = MemberView.model_validate(row)
            session.commit()
            return view

    def repositories(
        self, limit: int = 10, cursor: str | None = None
    ) -> CursorPage[RepositoryView]:
        statement = _page_after(
            select(*_REPOSITORY_COLUMNS), RepositoryPolicyRecord.created_at,
            RepositoryPolicyRecord.id, cursor,
        ).limit(limit + 1)
        with self.sessions() as session:
            rows = session.execute(statement).mappings().all()
        items = tuple(RepositoryView.model_validate(row) for row in rows[:limit])
        return CursorPage(
            items=items,
            next_cursor=(
                encode_cursor(items[-1].created_at, items[-1].id)
                if len(rows) > limit and items else None
            ),
        )

    def save_repository(
        self, draft: RepositoryWrite, actor: str, identifier: str | None = None,
    ) -> RepositoryView:
        now = datetime.now(UTC)
        with self.sessions() as session:
            self._validate_policy(session, draft)
            values = {
                "policy": draft.policy.model_dump(mode="json"),
                "revision": draft.expected_revision + 1,
                "updated_at": now, "updated_by": actor,
            }
            if identifier is None:
                if draft.expected_revision != 0:
                    raise TeamConflictError("新增仓库的版本应为 0")
                identifier = str(uuid4())
                session.add(RepositoryPolicyRecord(
                    id=identifier, repository=draft.repository,
                    repository_key=draft.repository.casefold(), created_at=now, **values,
                ))
                session.flush()
            else:
                result = session.execute(update(RepositoryPolicyRecord).where(
                    RepositoryPolicyRecord.id == identifier,
                    RepositoryPolicyRecord.repository_key == draft.repository.casefold(),
                    RepositoryPolicyRecord.revision == draft.expected_revision,
                ).values(**values).returning(RepositoryPolicyRecord.id))
                if result.scalar_one_or_none() is None:
                    raise TeamConflictError("仓库策略已变化或仓库名不匹配，请刷新后重试")
            self._audit(
                session, "team.repository.updated", draft.repository, actor, now,
                {"revision": values["revision"], "policy": values["policy"]},
            )
            row = session.execute(select(*_REPOSITORY_COLUMNS).where(
                RepositoryPolicyRecord.id == identifier,
            )).mappings().one()
            view = RepositoryView.model_validate(row)
            session.commit()
            return view

    def _validate_policy(self, session: Session, draft: RepositoryWrite) -> None:
        sources = draft.policy.knowledge_sources
        if sources:
            existing = set(session.scalars(select(KnowledgeDocumentRecord.source).where(
                KnowledgeDocumentRecord.source.in_(sources),
                KnowledgeDocumentRecord.enabled.is_(True),
                KnowledgeDocumentRecord.archived_at.is_(None),
            ).limit(100)).all())
            if set(sources) != existing:
                raise ValueError("所选知识文档不存在、已停用或已归档")
        approver = draft.policy.approver
        if approver and approver != self.configured_administrator:
            member = session.execute(select(
                TeamMemberRecord.role, TeamMemberRecord.resource_scope,
            ).where(
                TeamMemberRecord.username == approver,
                TeamMemberRecord.enabled.is_(True),
            )).one_or_none()
            if member is None or not has_permission(
                AccessRole(member.role), Permission.APPROVE_REVIEWS
            ):
                raise ValueError("审批负责人必须是启用的审批员或管理员")
            scope = MemberScope.model_validate(member.resource_scope).resource_scope()
            if not scope.unrestricted:
                repository_key = draft.repository.casefold()
                if (
                    repository_key not in scope.repositories
                    and repository_key.split("/", 1)[0] not in scope.organizations
                ):
                    raise ValueError("审批负责人尚未获得该仓库的权限")

    def audits(self, limit: int = 10, cursor: str | None = None) -> CursorPage[TeamAuditView]:
        statement = _page_after(
            select(
                OutboxEventRecord.id, OutboxEventRecord.event_type,
                OutboxEventRecord.payload, OutboxEventRecord.occurred_at,
            ).where(OutboxEventRecord.aggregate_type == "team"),
            OutboxEventRecord.occurred_at, OutboxEventRecord.id, cursor,
        ).limit(limit + 1)
        with self.sessions() as session:
            rows = session.execute(statement).mappings().all()
        items = tuple(TeamAuditView.model_validate(row) for row in rows[:limit])
        return CursorPage(
            items=items,
            next_cursor=(
                encode_cursor(items[-1].occurred_at, items[-1].id)
                if len(rows) > limit and items else None
            ),
        )

    @staticmethod
    def _audit(
        session: Session, event_type: str, target: str, actor: str,
        now: datetime, changes: dict[str, object],
    ) -> None:
        identifier = str(uuid4())
        session.add(OutboxEventRecord(
            id=identifier, event_key=f"{event_type}:{identifier}",
            aggregate_type="team",
            aggregate_id=str(uuid5(NAMESPACE_URL, f"{event_type}:{target}")),
            event_type=event_type,
            payload={"actor": actor, "target": target, **changes},
            occurred_at=now, publish_attempts=0,
        ))


def translate_team_error(error: Exception) -> tuple[int, str]:
    if isinstance(error, (TeamConflictError, IntegrityError)):
        return 409, "记录已存在或版本已变化，请刷新后重试"
    if isinstance(error, ValueError):
        return 422, str(error)
    return 503, "团队管理暂时不可用，请稍后重试"


TEAM_ERRORS = (ValueError, SQLAlchemyError)
