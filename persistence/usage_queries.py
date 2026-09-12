"""费用页面只读取月度汇总与有界请求列表。"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from domain.pagination import CursorPage, encode_cursor
from domain.platform import (
    PlatformNotFoundError,
    UsageBreakdown,
    UsageMonth,
    UsageRequest,
)
from domain.repository_policy import RepositoryPolicy
from persistence.models import ModelUsageRequestRecord as Request
from persistence.models import RepositoryPolicyRecord
from persistence.models import RepositoryUsageMonthRecord as Month
from persistence.pagination import apply_cursor
from persistence.resource_scope import resource_predicate
from services.rbac import ResourceScope


def _scope(model, scope: ResourceScope):
    return resource_predicate(
        scope,
        installation_column=model.installation_id,
        repository_column=model.repository,
        repository_key_column=model.repository_key,
    )


class UsageQueries:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def months(
        self,
        scope: ResourceScope,
        month: str,
        *,
        limit: int = 10,
        cursor: str | None = None,
    ) -> CursorPage[UsageMonth]:
        try:
            period = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
        except ValueError as exc:
            raise ValueError("月份格式应为 YYYY-MM") from exc
        columns = [
            getattr(Month, name)
            for name in UsageMonth.model_fields
            if name not in {"month", "budget_microusd", "warning_percent", "warning"}
        ]
        statement = (
            select(*columns, Month.month, RepositoryPolicyRecord.policy)
            .outerjoin(
                RepositoryPolicyRecord,
                RepositoryPolicyRecord.repository_key == Month.repository_key,
            )
            .where(Month.month == period, _scope(Month, scope))
        )
        statement = apply_cursor(statement, Month.created_at, Month.id, cursor).limit(
            limit + 1
        )
        with self.sessions() as session:
            rows = session.execute(statement).mappings().all()
        items = []
        for row in rows[:limit]:
            values = dict(row)
            policy = RepositoryPolicy.model_validate(values.pop("policy") or {})
            values["month"] = row["month"].strftime("%Y-%m")
            values["budget_microusd"] = policy.monthly_budget_microusd
            values["warning_percent"] = policy.budget_warning_percent
            values["warning"] = policy.monthly_budget_microusd is not None and (
                (row["estimated_cost_microusd"] + row["reserved_cost_microusd"]) * 100
                >= policy.monthly_budget_microusd * policy.budget_warning_percent
            )
            items.append(UsageMonth.model_validate(values))
        return CursorPage(
            items=tuple(items),
            next_cursor=encode_cursor(items[-1].created_at, items[-1].id)
            if len(rows) > limit and items
            else None,
        )

    def requests(
        self,
        scope: ResourceScope,
        month_id: str,
        *,
        limit: int = 10,
        cursor: str | None = None,
    ) -> CursorPage[UsageRequest]:
        statement = select(
            *(getattr(Request, name) for name in UsageRequest.model_fields)
        ).where(Request.month_id == month_id, _scope(Request, scope))
        statement = apply_cursor(
            statement, Request.created_at, Request.id, cursor
        ).limit(limit + 1)
        with self.sessions() as session:
            rows = session.execute(statement).mappings().all()
        items = tuple(UsageRequest.model_validate(row) for row in rows[:limit])
        return CursorPage(
            items=items,
            next_cursor=encode_cursor(items[-1].created_at, items[-1].id)
            if len(rows) > limit and items
            else None,
        )

    def breakdown(
        self, scope: ResourceScope, month_id: str
    ) -> tuple[UsageBreakdown, ...]:
        with self.sessions() as session:
            if (
                session.scalar(
                    select(Month.id).where(Month.id == month_id, _scope(Month, scope))
                )
                is None
            ):
                raise PlatformNotFoundError("月份不存在")
            rows = (
                session.execute(
                    select(
                        Request.model,
                        Request.purpose,
                        func.count().label("request_count"),
                        func.coalesce(
                            func.sum(Request.estimated_cost_microusd), 0
                        ).label("estimated_cost_microusd"),
                        func.count()
                        .filter(Request.estimated_cost_microusd.is_(None))
                        .label("unknown_count"),
                    )
                    .where(Request.month_id == month_id, _scope(Request, scope))
                    .group_by(
                        Request.model,
                        Request.purpose,
                    )
                    .order_by(func.count().desc(), Request.model, Request.purpose)
                    .limit(100)
                )
                .mappings()
                .all()
            )
        return tuple(UsageBreakdown.model_validate(row) for row in rows)
