"""有界读取已保存计划，固定实验输入；不创建任务、不重新检索、不修改生产记录。"""

from collections import defaultdict

from sqlalchemy import select

from domain.model_review import ModelReviewInput
from domain.repository_policy import RepositoryPolicySnapshot
from domain.retrieval import ContextEvidence, RetrievalTrace
from domain.review_planning import DEFAULT_REVIEW_DOMAINS, RepositoryRule, ReviewUnit
from persistence.models import (
    RepositoryPolicyRecord,
    RetrievalTraceRecord,
    ReviewPlanRecord,
    ReviewPlanRuleRecord,
    ReviewRunRecord,
    ReviewUnitRecord,
)


def frozen_experiment_inputs(sessions, repository: str, run_ids: tuple[str, ...]):
    if not 1 <= len(run_ids) <= 10 or len(set(run_ids)) != len(run_ids):
        raise ValueError("实验每次需要 1 至 10 条不同的已保存运行")
    run, plan, rule, unit = (
        ReviewRunRecord,
        ReviewPlanRecord,
        ReviewPlanRuleRecord,
        ReviewUnitRecord,
    )
    with sessions() as session, session.begin():
        headers = (
            session.execute(
                select(
                    run.id,
                    run.repository_id,
                    run.repository,
                    run.pull_request_number,
                    run.head_sha,
                    run.review_version_key,
                    plan.id.label("plan_id"),
                    plan.plan_fingerprint,
                    plan.planner_version,
                    plan.unit_count,
                    plan.rule_count,
                    plan.total_estimated_input_bytes,
                )
                .join(plan, plan.review_run_id == run.id)
                .where(run.id.in_(run_ids), run.repository_key == repository.casefold())
                .with_for_update(read=True, of=run)
                .limit(11)
            )
            .mappings()
            .all()
        )
        if len(headers) != len(run_ids):
            raise ValueError("实验源运行不存在、缺少计划或仓库不一致")
        if any(
            row["unit_count"] > 100
            or row["rule_count"] > 256
            or row["total_estimated_input_bytes"] > 2 * 1024 * 1024
            for row in headers
        ):
            raise ValueError("请选择不超过 100 文件、2 MiB 的小规模真实 PR 进行试验")
        policy = session.execute(
            select(
                RepositoryPolicyRecord.policy, RepositoryPolicyRecord.revision
            ).where(RepositoryPolicyRecord.repository_key == repository.casefold())
        ).one()
        snapshot = RepositoryPolicySnapshot.model_validate(
            {**policy.policy, "repository": repository, "revision": policy.revision}
        )
        if not snapshot.enabled:
            raise ValueError("仓库已经暂停，不能启动实验")
        if snapshot.monthly_budget_microusd is not None:
            raise ValueError(
                "独立实验不能绕过仓库月度限额，请使用现有受账本约束的任务流程"
            )
        plans = [row["plan_id"] for row in headers]
        rules = defaultdict(list)
        for row in session.execute(
            select(
                rule.review_plan_id,
                rule.path,
                rule.scope,
                rule.blob_sha,
                rule.content,
                rule.content_sha256,
                rule.byte_size,
            )
            .where(rule.review_plan_id.in_(plans))
            .order_by(rule.review_plan_id, rule.ordinal)
            .limit(2561)
        ).mappings():
            values = dict(row)
            rules[values.pop("review_plan_id")].append(values)
        units = defaultdict(list)
        for row in session.execute(
            select(
                unit.review_plan_id,
                unit.unit_key,
                unit.group_key,
                unit.file,
                unit.blob_sha,
                unit.language,
                unit.patch,
                unit.patch_sha256,
                unit.rule_paths,
                unit.review_domains,
                unit.estimated_input_bytes,
                unit.planner_version,
            )
            .where(unit.review_plan_id.in_(plans))
            .order_by(unit.review_plan_id, unit.ordinal)
            .limit(1001)
        ).mappings():
            values = dict(row)
            units[values.pop("review_plan_id")].append(values)
        contexts: dict[str, list[ContextEvidence]] = defaultdict(list)
        trace_rows = session.execute(
            select(
                RetrievalTraceRecord.review_run_id,
                RetrievalTraceRecord.plan_fingerprint,
                RetrievalTraceRecord.payload,
            )
            .where(
                RetrievalTraceRecord.review_run_id.in_(run_ids),
                RetrievalTraceRecord.agent.is_not(None),
            )
            .limit(41)
        ).all()
        identities = {row["id"]: row for row in headers}
        for row in trace_rows:
            if (
                row.plan_fingerprint
                != identities[row.review_run_id]["plan_fingerprint"]
            ):
                continue
            trace = RetrievalTrace.model_validate(row.payload)
            contexts[row.review_run_id].extend(
                item.model_copy(update={"agent": trace.agent})
                for item in trace.selected
            )
    result = {}
    for row in headers:
        plan_rules, plan_units = rules[row["plan_id"]], units[row["plan_id"]]
        if len(plan_rules) != row["rule_count"] or len(plan_units) != row["unit_count"]:
            raise ValueError("持久计划内容缺失，不能静默缩小试验输入")
        result[row["id"]] = ModelReviewInput(
            review_plan_id=row["plan_id"],
            review_run_id=row["id"],
            plan_fingerprint=row["plan_fingerprint"],
            planner_version=row["planner_version"],
            review_version_key=row["review_version_key"],
            repository_id=row["repository_id"],
            repository=row["repository"],
            pull_request_number=row["pull_request_number"],
            head_sha=row["head_sha"],
            rules=tuple(RepositoryRule.model_validate(item) for item in plan_rules),
            units=tuple(
                ReviewUnit.model_validate(
                    {
                        **item,
                        "review_domains": item["review_domains"]
                        or DEFAULT_REVIEW_DOMAINS,
                        "review_version_key": row["review_version_key"],
                        "head_sha": row["head_sha"],
                    }
                )
                for item in plan_units
            ),
            total_estimated_input_bytes=row["total_estimated_input_bytes"],
            repository_policy=snapshot,
            context_evidence=tuple(contexts[row["id"]]),
        )
    return tuple(result[identity] for identity in run_ids)
