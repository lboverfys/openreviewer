"""不可变审查方案与仓库绑定的存储事务。"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, true, update
from sqlalchemy.orm import Session, sessionmaker

from domain.pagination import CursorPage, encode_cursor
from domain.platform import PlatformConflictError, PlatformNotFoundError, ProfileView
from domain.repository_policy import RepositoryPolicy
from persistence.models import (
    AiSettingsRecord,
    KnowledgeLibraryRecord,
    RepositoryPolicyRecord,
    RetrievalSettingsRecord,
    ReviewProfileRecord,
)
from persistence.pagination import apply_cursor
from persistence.platform_common import platform_audit
from persistence.platform_queries import repository_visible
from services.model_review import StructuredReviewPromptBuilder
from services.rbac import ResourceScope


def _profile_view(row, prompt_snapshot) -> ProfileView:
    summary = row.summary
    return ProfileView(
        id=row.id,
        name=row.name,
        repository=row.repository,
        note=row.note,
        fingerprint=row.fingerprint,
        ai_revision=row.ai_revision,
        prompt_version=summary["prompt_version"],
        prompt_content_sha256=StructuredReviewPromptBuilder(prompt_snapshot).content_sha256,
        base_profile_id=summary.get("base_profile_id"),
        role_instructions=prompt_snapshot["roles"],
        supplementary_instructions=prompt_snapshot.get("supplementary_instructions", ""),
        models=summary["models"],
        knowledge_versions=summary["knowledge_versions"],
        retrieval_settings=summary["retrieval_settings"],
        created_by=row.created_by,
        created_at=row.created_at,
    )


class ReviewProfileRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    @staticmethod
    def _stamp_query(repository: str, scope: ResourceScope):
        return select(
            RepositoryPolicyRecord.id,
            RepositoryPolicyRecord.repository,
            RepositoryPolicyRecord.revision,
            RepositoryPolicyRecord.policy,
            func.coalesce(
                select(AiSettingsRecord.revision)
                .where(AiSettingsRecord.id == 1)
                .scalar_subquery(),
                0,
            ).label("ai"),
            func.coalesce(
                select(KnowledgeLibraryRecord.revision)
                .where(KnowledgeLibraryRecord.id == 1)
                .scalar_subquery(),
                0,
            ).label("knowledge"),
            func.coalesce(
                select(RetrievalSettingsRecord.revision)
                .where(RetrievalSettingsRecord.id == 1)
                .scalar_subquery(),
                0,
            ).label("retrieval"),
        ).where(
            RepositoryPolicyRecord.repository_key == repository.casefold(),
            repository_visible(scope, RepositoryPolicyRecord.repository_key),
        )

    def stamp(self, repository: str, scope: ResourceScope) -> dict[str, Any]:
        with self.sessions() as session:
            row = (
                session.execute(self._stamp_query(repository, scope))
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise PlatformNotFoundError("请先在团队管理中配置该仓库")
        return dict(row)

    def create(
        self,
        values: dict[str, Any],
        stamp: dict[str, Any],
        actor: str,
        scope: ResourceScope,
    ) -> ProfileView:
        with self.sessions() as session, session.begin():
            current = (
                session.execute(self._stamp_query(values["repository"], scope))
                .mappings()
                .one_or_none()
            )
            if current is None or any(
                current[key] != stamp[key]
                for key in ("revision", "ai", "knowledge", "retrieval")
            ):
                raise PlatformConflictError("捕获期间配置发生变化，请重新保存方案")
            row = ReviewProfileRecord(**values)
            session.add(row)
            platform_audit(
                session,
                "platform.profile.created",
                row.id,
                row.repository,
                actor,
                row.created_at,
                details={"fingerprint": row.fingerprint},
            )
            return _profile_view(row, row.snapshot["prompt"])

    def list(
        self,
        scope: ResourceScope,
        *,
        repository: str | None = None,
        limit: int = 10,
        cursor: str | None = None,
    ) -> CursorPage[ProfileView]:
        model = ReviewProfileRecord
        # API 只取公开快照；加密凭据留在 Worker 专用加载接口。
        statement = select(
            model.id,
            model.name,
            model.repository,
            model.note,
            model.fingerprint,
            model.ai_revision,
            model.summary,
            model.snapshot["prompt"].label("prompt_snapshot"),
            model.created_by,
            model.created_at,
        ).where(repository_visible(scope, model.repository_key))
        if repository:
            statement = statement.where(model.repository_key == repository.casefold())
        statement = apply_cursor(statement, model.created_at, model.id, cursor).limit(
            limit + 1
        )
        with self.sessions() as session:
            rows = session.execute(statement).all()
        items = tuple(_profile_view(row, row.prompt_snapshot) for row in rows[:limit])
        return CursorPage(
            items=items,
            next_cursor=encode_cursor(items[-1].created_at, items[-1].id)
            if len(rows) > limit and items
            else None,
        )

    def load(self, identifier: str, scope: ResourceScope | None = None) -> dict[str, Any]:
        model = ReviewProfileRecord
        with self.sessions() as session:
            row = (
                session.execute(
                    select(
                        model.id,
                        model.repository,
                        model.snapshot,
                        model.ciphertext,
                        model.nonce,
                        model.key_version,
                        model.fingerprint,
                        model.ai_revision,
                    ).where(model.id == identifier,
                            repository_visible(scope, model.repository_key) if scope is not None else true())
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise PlatformNotFoundError("审查方案不存在")
        return dict(row)

    def quality(self, identifier: str, scope: ResourceScope, dataset_id: str | None = None):
        from persistence.profile_quality import profile_quality
        with self.sessions() as session:
            return profile_quality(session, identifier, scope, dataset_id)

    def activate(
        self, identifier: str, expected_revision: int, actor: str, scope: ResourceScope,
        *, dataset_id: str | None = None, evidence_token: str | None = None, reason: str = "",
    ) -> int:
        from domain.security import redact_text
        from persistence.profile_quality import profile_quality
        now = datetime.now(UTC)
        with self.sessions() as session, session.begin():
            profile = session.execute(
                select(
                    ReviewProfileRecord.repository, ReviewProfileRecord.repository_key
                ).where(
                    ReviewProfileRecord.id == identifier,
                    repository_visible(scope, ReviewProfileRecord.repository_key),
                )
            ).one_or_none()
            if profile is None:
                raise PlatformNotFoundError("审查方案不存在")
            row = session.execute(
                select(RepositoryPolicyRecord.id, RepositoryPolicyRecord.policy)
                .where(
                    RepositoryPolicyRecord.repository_key == profile.repository_key,
                    RepositoryPolicyRecord.revision == expected_revision,
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                raise PlatformConflictError("仓库策略已变化，请刷新后重试")
            quality = profile_quality(session, identifier, scope, dataset_id, lock=True)
            if evidence_token is not None and quality.evidence_token != evidence_token:
                raise PlatformConflictError("评测或当前方案已变化，请重新查看质量提示")
            if dataset_id and evidence_token is None:
                raise ValueError("请先查看评测质量提示再启用")
            if quality.status != "reviewed" and not reason.strip():
                raise ValueError("方案尚未验证或出现退步，请填写人工启用理由")
            previous = RepositoryPolicy.model_validate(row.policy)
            policy = previous.model_copy(update={"review_profile_id": identifier})
            session.execute(
                update(RepositoryPolicyRecord)
                .where(RepositoryPolicyRecord.id == row.id)
                .values(
                    policy=policy.model_dump(mode="json"),
                    revision=expected_revision + 1,
                    updated_by=actor,
                    updated_at=now,
                )
            )
            platform_audit(
                session,
                "platform.profile.activated",
                identifier,
                profile.repository,
                actor,
                now,
                revision=expected_revision + 1,
                details={"previous_profile_id": previous.review_profile_id,
                         "quality_status": quality.status,
                         "evidence_token": quality.evidence_token,
                         "evaluation_dataset_id": dataset_id,
                         "quality_reasons": list(quality.reasons),
                         "reason": redact_text(reason.strip())[:1000]},
            )
        return expected_revision + 1
