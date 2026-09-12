"""按职责集中维护的 github 数据记录。"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from domain.enums import (
    ChangedFileStatus,
    CiCheckKind,
    CiState,
    ExternalActionState,
    PatchState,
    PullRequestState,
)
from persistence.models.base import Base, enum_values, utc_now


class PullRequestVersionRecord(Base):
    """平台观察到的一份不可变 Pull Request head SHA 版本。"""

    __tablename__ = "pull_request_versions"
    __table_args__ = (
        Index(
            "ix_pull_request_versions_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
            info={"postgresql_only": True},
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_pull_request_versions_author_login_trgm",
            "author_login",
            postgresql_using="gin",
            postgresql_ops={"author_login": "gin_trgm_ops"},
            info={"postgresql_only": True},
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_pull_request_versions_head_ref_trgm",
            "head_ref",
            postgresql_using="gin",
            postgresql_ops={"head_ref": "gin_trgm_ops"},
            info={"postgresql_only": True},
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_pull_request_versions_base_ref_trgm",
            "base_ref",
            postgresql_using="gin",
            postgresql_ops={"base_ref": "gin_trgm_ops"},
            info={"postgresql_only": True},
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_pull_request_versions_head_repository_trgm",
            "head_repository",
            postgresql_using="gin",
            postgresql_ops={"head_repository": "gin_trgm_ops"},
            info={"postgresql_only": True},
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_pull_request_versions_base_repository_trgm",
            "base_repository",
            postgresql_using="gin",
            postgresql_ops={"base_repository": "gin_trgm_ops"},
            info={"postgresql_only": True},
        ).ddl_if(dialect="postgresql"),
        CheckConstraint("installation_id > 0", name="installation_id_positive"),
        CheckConstraint("repository_id > 0", name="repository_id_positive"),
        CheckConstraint("pull_request_number > 0", name="pull_request_number_positive"),
        UniqueConstraint("review_version_key"),
        Index(
            "ix_pull_request_versions_repository_pr_seen",
            "repository_id",
            "pull_request_number",
            "last_seen_at",
        ),
        CheckConstraint(
            "changed_files_count IS NULL OR changed_files_count >= 0",
            name="changed_files_count_nonnegative",
        ),
        CheckConstraint(
            f"pr_state IS NULL OR pr_state IN ({enum_values(PullRequestState)})",
            name="pr_state_value",
        ),
        CheckConstraint(
            f"ci_state IS NULL OR ci_state IN ({enum_values(CiState)})",
            name="ci_state_value",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_version_key: Mapped[str] = mapped_column(String(360), nullable=False)
    installation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "github_installations.id",
            name="fk_pr_versions_installation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    base_sha: Mapped[str | None] = mapped_column(String(64))
    author_login: Mapped[str | None] = mapped_column(String(100))
    html_url: Mapped[str | None] = mapped_column(String(2048))
    head_repository: Mapped[str | None] = mapped_column(String(255))
    head_ref: Mapped[str | None] = mapped_column(String(1024))
    base_repository: Mapped[str | None] = mapped_column(String(255))
    base_ref: Mapped[str | None] = mapped_column(String(1024))
    identity_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    pr_state: Mapped[str | None] = mapped_column(String(16))
    is_draft: Mapped[bool | None] = mapped_column(Boolean)
    title: Mapped[str | None] = mapped_column(String(1000))
    changed_files_count: Mapped[int | None] = mapped_column(Integer)
    files_complete: Mapped[bool | None] = mapped_column(Boolean)
    diff_complete: Mapped[bool | None] = mapped_column(Boolean)
    context_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pr_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ci_state: Mapped[str | None] = mapped_column(String(32))
    ci_checks_complete: Mapped[bool | None] = mapped_column(Boolean)
    ci_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class PullRequestFileRecord(Base):
    """某个不可变 PR head SHA 下的有界变更文件快照。"""

    __tablename__ = "pull_request_files"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({enum_values(ChangedFileStatus)})",
            name="status_value",
        ),
        CheckConstraint(
            f"patch_state IN ({enum_values(PatchState)})",
            name="patch_state_value",
        ),
        CheckConstraint("additions >= 0", name="additions_nonnegative"),
        CheckConstraint("deletions >= 0", name="deletions_nonnegative"),
        CheckConstraint("changes >= 0", name="changes_nonnegative"),
        UniqueConstraint("pull_request_version_id", "path"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pull_request_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "pull_request_versions.id",
            name="fk_pr_files_version",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    previous_path: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    blob_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    additions: Mapped[int] = mapped_column(Integer, nullable=False)
    deletions: Mapped[int] = mapped_column(Integer, nullable=False)
    changes: Mapped[int] = mapped_column(Integer, nullable=False)
    patch_state: Mapped[str] = mapped_column(String(32), nullable=False)
    patch: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class PullRequestCiCheckRecord(Base):
    """某个 PR 版本最近一次观察到的 CI 检查或提交状态。"""

    __tablename__ = "pull_request_ci_checks"
    __table_args__ = (
        CheckConstraint(
            f"kind IN ({enum_values(CiCheckKind)})",
            name="kind_value",
        ),
        UniqueConstraint("pull_request_version_id", "kind", "external_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pull_request_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "pull_request_versions.id",
            name="fk_pr_ci_checks_version",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    external_key: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    conclusion: Mapped[str | None] = mapped_column(String(50))
    app_id: Mapped[int | None] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class GitHubWebhookDeliveryRecord(Base):
    """一条已验签并被接受的 GitHub 投递审计记录。"""

    __tablename__ = "github_webhook_deliveries"
    __table_args__ = (Index("ix_github_webhook_deliveries_received", "received_at"),)

    delivery_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    installation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "github_installations.id",
            name="fk_webhook_installation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    pull_request_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "pull_request_versions.id",
            name="fk_webhook_pr_version",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    review_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_runs.id",
            name="fk_webhook_review_run",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    review_task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_tasks.id",
            name="fk_webhook_review_task",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ExternalActionRecord(Base):
    """GitHub 外部副作用使用的幂等与审计状态。"""

    __tablename__ = "external_actions"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({enum_values(ExternalActionState)})",
            name="state_value",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="duration_ms_nonnegative",
        ),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_shape",
        ),
        UniqueConstraint("action_key"),
        Index("ix_external_actions_run_state", "review_run_id", "state"),
        Index(
            "ix_external_actions_claimable",
            "state",
            "lease_expires_at",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    action_key: Mapped[str] = mapped_column(String(300), nullable=False)
    review_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "review_runs.id",
            name="fk_external_actions_review_run",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    remote_id: Mapped[str | None] = mapped_column(String(200))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_method: Mapped[str | None] = mapped_column(String(10))
    request_path: Mapped[str | None] = mapped_column(String(1000))
    response_status: Mapped[int | None] = mapped_column(Integer)
    github_request_id: Mapped[str | None] = mapped_column(String(200))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    rate_limit_remaining: Mapped[int | None] = mapped_column(Integer)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_retryable: Mapped[bool | None] = mapped_column(Boolean)
    last_error_details: Mapped[dict[str, object] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
