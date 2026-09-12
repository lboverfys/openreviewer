"""按职责集中维护的 identity 数据记录。"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from persistence.models.base import Base, utc_now


class GitHubInstallationRecord(Base):
    """通过已验签投递观察到的 GitHub App 安装记录。"""

    __tablename__ = "github_installations"
    __table_args__ = (
        CheckConstraint("id > 0", name="id_positive"),
        Index("ix_github_installations_last_seen", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AdminSessionRecord(Base):
    """可吊销的管理员会话；只保存随机会话 ID 的 SHA-256。"""

    __tablename__ = "admin_sessions"
    __table_args__ = (
        Index("ix_admin_sessions_expires_at", "expires_at"),
        Index("ix_admin_sessions_active", "revoked_at", "expires_at"),
        Index("ix_admin_sessions_username", "username"),
    )

    session_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(
        String(32), nullable=False, default="administrator"
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TeamMemberRecord(Base):
    """复用现有角色和资源范围；账号修改以 revision 撤销旧会话。"""

    __tablename__ = "team_members"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
        CheckConstraint(
            "role IN ('viewer', 'adjudicator', 'publisher', 'administrator')",
            name="role_value",
        ),
        Index("ix_team_members_created", "created_at", "username"),
    )

    username: Mapped[str] = mapped_column(String(100), primary_key=True)
    username_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    resource_scope: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False)


class RepositoryPolicyRecord(Base):
    __tablename__ = "repository_policies"
    __table_args__ = (
        CheckConstraint("revision > 0", name="revision_positive"),
        UniqueConstraint("repository_key"),
        Index("ix_repository_policies_created", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    repository_key: Mapped[str] = mapped_column(String(255), nullable=False)
    policy: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False)


class LoginRateLimitRecord(Base):
    """跨 API 副本共享的固定窗口登录尝试计数。"""

    __tablename__ = "login_rate_limits"
    __table_args__ = (
        CheckConstraint("attempt_count > 0", name="attempt_count_positive"),
        Index("ix_login_rate_limits_updated_at", "updated_at"),
    )

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
