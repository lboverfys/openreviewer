"""持久化成员认证；复用原有资源范围和跨副本会话表。"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from persistence.models import TeamMemberRecord
from services.auth import AuthPersistenceError, AuthSettings, UserCredential
from services.rbac import AccessRole, ResourceScope


def scope_payload(scope: ResourceScope) -> dict[str, object]:
    return {
        "installation_ids": sorted(scope.installation_ids),
        "organizations": sorted(scope.organizations),
        "repositories": sorted(scope.repositories),
        "unrestricted": scope.unrestricted,
    }


class SqlAlchemyMemberStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def bootstrap(self, settings: AuthSettings) -> None:
        """最多 31 个旧成员一次导入；更新、停用后不会被配置文件覆盖。"""
        if not settings.additional_users:
            return
        now = datetime.now(UTC)
        values = [
            {
                "username": user.username,
                "username_key": user.username.casefold(),
                "password_hash": user.password_hash,
                "role": user.role.value,
                "enabled": True,
                "resource_scope": scope_payload(user.resource_scope),
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "updated_by": settings.username,
            }
            for user in settings.additional_users
        ]
        with self.sessions() as session:
            try:
                insert = (
                    postgresql_insert
                    if session.get_bind().dialect.name == "postgresql"
                    else sqlite_insert
                )
                session.execute(
                    insert(TeamMemberRecord).values(values).on_conflict_do_nothing()
                )
                session.commit()
            except SQLAlchemyError as exc:
                raise AuthPersistenceError("existing members could not be imported") from exc

    def find_user(self, username: str) -> UserCredential | None:
        with self.sessions() as session:
            try:
                row = session.execute(
                    select(
                        TeamMemberRecord.username,
                        TeamMemberRecord.password_hash,
                        TeamMemberRecord.role,
                        TeamMemberRecord.resource_scope,
                        TeamMemberRecord.revision,
                    ).where(
                        TeamMemberRecord.username_key == username.casefold(),
                        TeamMemberRecord.enabled.is_(True),
                    )
                ).one_or_none()
            except SQLAlchemyError as exc:
                raise AuthPersistenceError("member authentication is unavailable") from exc
        if row is None:
            return None
        role = AccessRole(row.role)
        return UserCredential(
            username=row.username,
            password_hash=row.password_hash,
            role=role,
            revision=row.revision,
            resource_scope=(
                ResourceScope.unrestricted_scope()
                if role is AccessRole.ADMINISTRATOR
                and row.resource_scope.get("unrestricted") is True
                else ResourceScope.from_mapping({
                    key: value for key, value in row.resource_scope.items()
                    if key != "unrestricted"
                })
            ),
        )
