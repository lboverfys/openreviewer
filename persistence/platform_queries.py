"""运行诊断和审计投影；聚合发生在数据库内。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, literal, or_, select, true
from sqlalchemy.orm import Session, sessionmaker

from domain.pagination import CursorPage, encode_cursor
from domain.platform import (
    DiagnosticReport,
    FailureDiagnostic,
    PlatformAudit,
    ProviderChannelView,
    RepositoryDiagnostic,
)
from persistence.models import (
    ModelCallRecord,
    ModelUsageRequestRecord,
    OutboxEventRecord,
    ProviderCircuitRecord,
    RepositoryPolicyRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.pagination import apply_cursor
from persistence.resource_scope import resource_predicate
from persistence.review_insights import collect_review_insights
from services.rbac import ResourceScope


def repository_visible(scope: ResourceScope, repository_key):
    if scope.unrestricted:
        return true()
    if scope.installation_ids:
        return (
            select(ReviewRunRecord.id)
            .where(
                ReviewRunRecord.repository_key == repository_key,
                resource_predicate(
                    scope,
                    installation_column=ReviewRunRecord.installation_id,
                    repository_column=ReviewRunRecord.repository,
                    repository_key_column=ReviewRunRecord.repository_key,
                ),
            )
            .correlate_except(ReviewRunRecord)
            .exists()
        )
    return resource_predicate(
        scope,
        installation_column=literal(0),
        repository_column=repository_key,
        repository_key_column=repository_key,
    )


class PlatformQueries:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def diagnostics(self, scope: ResourceScope, *, days: int = 7) -> DiagnosticReport:
        if not 1 <= days <= 30:
            raise ValueError("诊断时间范围为 1 到 30 天")
        now = datetime.now(UTC)
        since = now - timedelta(days=days)
        run, task = ReviewRunRecord, ReviewTaskRecord
        access = resource_predicate(
            scope,
            installation_column=run.installation_id,
            repository_column=run.repository,
            repository_key_column=run.repository_key,
        )
        calls = (
            select(
                ReviewPlanRecord.review_run_id.label("run_id"),
                func.sum(ModelCallRecord.duration_ms).label("duration_ms"),
            )
            .join(
                ReviewPlanRecord, ReviewPlanRecord.id == ModelCallRecord.review_plan_id
            )
            .where(
                ModelCallRecord.created_at >= since,
            )
            .group_by(ReviewPlanRecord.review_run_id)
            .subquery()
        )
        with self.sessions() as session:
            postgres = session.get_bind().dialect.name == "postgresql"
            queue_ms = (
                (
                    func.extract("epoch", task.first_claimed_at)
                    - func.extract("epoch", task.created_at)
                )
                * 1000
                if postgres
                else (
                    func.julianday(task.first_claimed_at)
                    - func.julianday(task.created_at)
                )
                * 86_400_000
            )
            percentile = (
                func.percentile_cont(0.95)
                .within_group(queue_ms)
                .filter(task.first_claimed_at >= since)
                if postgres
                else literal(None)
            )
            rows = (
                session.execute(
                    select(
                        run.repository_key.label("repository"),
                        func.count()
                        .filter(
                            task.execution_status.in_(
                                ("queued", "ready_for_review", "waiting_for_ci")
                            ),
                            task.workflow_status != "paused",
                        )
                        .label("queued"),
                        func.count()
                        .filter(task.execution_status == "running")
                        .label("running"),
                        func.count()
                        .filter(task.workflow_status == "paused")
                        .label("paused"),
                        func.count()
                        .filter(
                            task.execution_status == "failed", task.updated_at >= since
                        )
                        .label("failed"),
                        func.count()
                        .filter(
                            task.execution_status == "completed",
                            task.updated_at >= since,
                        )
                        .label("completed"),
                        func.min(
                            case(
                                (
                                    task.execution_status.in_(
                                        ("queued", "ready_for_review", "waiting_for_ci")
                                    ),
                                    task.created_at,
                                )
                            )
                        ).label("oldest_queued_at"),
                        func.avg(queue_ms)
                        .filter(task.first_claimed_at >= since)
                        .label("mean_queue_ms"),
                        percentile.label("p95_queue_ms"),
                        func.avg(calls.c.duration_ms).label("mean_model_ms"),
                        func.max(
                            RepositoryPolicyRecord.policy[
                                "max_concurrent_reviews"
                            ].as_integer()
                        ).label("max_concurrent_reviews"),
                    )
                    .join(task, task.review_run_id == run.id)
                    .outerjoin(calls, calls.c.run_id == run.id)
                    .outerjoin(
                        RepositoryPolicyRecord,
                        RepositoryPolicyRecord.repository_key == run.repository_key,
                    )
                    .where(
                        access,
                        or_(
                            task.updated_at >= since,
                            task.execution_status.in_(
                                (
                                    "queued",
                                    "ready_for_review",
                                    "waiting_for_ci",
                                    "running",
                                )
                            ),
                            task.workflow_status == "paused",
                        ),
                    )
                    .group_by(run.repository_key)
                    .order_by(run.repository_key)
                    .limit(101)
                )
                .mappings()
                .all()
            )
            failures = (
                session.execute(
                    select(
                        task.last_error_code.label("code"),
                        func.count().label("count"),
                    )
                    .join(run, run.id == task.review_run_id)
                    .where(
                        access,
                        task.updated_at >= since,
                        task.last_error_code.is_not(None),
                    )
                    .group_by(task.last_error_code)
                    .order_by(func.count().desc(), task.last_error_code)
                    .limit(50)
                )
                .mappings()
                .all()
            )
            channels: tuple[ProviderChannelView, ...] = ()
            if scope.unrestricted:
                active = (
                    select(
                        ModelUsageRequestRecord.connection_key,
                        func.count().label("active"),
                    )
                    .where(
                        ModelUsageRequestRecord.status == "reserved",
                        ModelUsageRequestRecord.permit_expires_at > now,
                    )
                    .group_by(ModelUsageRequestRecord.connection_key)
                    .subquery()
                )
                channel_rows = (
                    session.execute(
                        select(
                            ProviderCircuitRecord.connection_key.label("key"),
                            ProviderCircuitRecord.provider,
                            ProviderCircuitRecord.failure_count,
                            ProviderCircuitRecord.open_until,
                            func.coalesce(active.c.active, 0).label("in_flight"),
                        )
                        .outerjoin(
                            active,
                            active.c.connection_key
                            == ProviderCircuitRecord.connection_key,
                        )
                        .where(ProviderCircuitRecord.updated_at >= since)
                        .order_by(ProviderCircuitRecord.updated_at.desc())
                        .limit(100)
                    )
                    .mappings()
                    .all()
                )
                channels = tuple(
                    ProviderChannelView.model_validate(row) for row in channel_rows
                )
            insights = collect_review_insights(session, scope, since, now)
        return DiagnosticReport(
            since=since,
            until=now,
            repositories=tuple(
                RepositoryDiagnostic.model_validate(row) for row in rows[:100]
            ),
            failures=tuple(FailureDiagnostic.model_validate(row) for row in failures),
            truncated=len(rows) > 100,
            provider_channels=channels,
            insights=insights,
        )

    def audits(
        self,
        scope: ResourceScope,
        *,
        limit: int = 10,
        cursor: str | None = None,
        object_id: str | None = None,
    ) -> CursorPage[PlatformAudit]:
        event = OutboxEventRecord
        statement = select(
            event.id,
            event.event_type,
            event.payload,
            event.aggregate_id,
            event.occurred_at,
        ).where(
            event.aggregate_type == "platform",
            repository_visible(scope, event.payload["repository_key"].as_string()),
        )
        if scope.installation_ids:
            statement = statement.where(
                or_(
                    event.payload["installation_id"].as_integer().is_(None),
                    event.payload["installation_id"]
                    .as_integer()
                    .in_(scope.installation_ids),
                )
            )
        if object_id:
            statement = statement.where(event.aggregate_id == object_id)
        statement = apply_cursor(statement, event.occurred_at, event.id, cursor).limit(
            limit + 1
        )
        with self.sessions() as session:
            rows = session.execute(statement).all()
        items = tuple(
            PlatformAudit(
                id=row.id,
                event_type=row.event_type,
                actor=row.payload["actor"],
                repository=row.payload["repository"],
                object_id=row.aggregate_id,
                revision=row.payload.get("revision"),
                created_at=row.occurred_at,
            )
            for row in rows[:limit]
        )
        return CursorPage(
            items=items,
            next_cursor=encode_cursor(items[-1].created_at, items[-1].id)
            if len(rows) > limit and items
            else None,
        )
