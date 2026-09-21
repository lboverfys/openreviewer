"""固定历史输入与方案的独立真实调用试验；默认 prepare 只读，不发布 GitHub 评论。"""

import argparse
import json
import os
import time
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from apps.maintenance.export_workflow_evidence import controlled_path
from domain.enums import ReviewAgent
from domain.model_review import ModelReviewInput
from domain.security import SafeError
from persistence.database import Database
from persistence.experiment_inputs import frozen_experiment_inputs
from persistence.review_profiles import ReviewProfileRepository
from services.agent_workflow import FixedAgentWorkflow
from services.ai_settings import AiSecretCipher
from services.experiment_accounting import ExperimentBudget
from services.model_budget import model_budget_scope
from services.model_review import (
    combine_model_review_results,
    plan_model_review_batches,
)
from services.rag import MarkdownKnowledgeBase, merge_review_citations
from services.rbac import ResourceScope
from services.review_profiles import ReviewProfileRuntimeLoader

VARIANTS = {
    "full": frozenset(),
    **{
        "without_" + a.value: frozenset({a})
        for a in (ReviewAgent.SECURITY, ReviewAgent.CONVENTION, ReviewAgent.LOGIC)
    },
}


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def dump_input(value: ModelReviewInput):
    return {
        **value.model_dump(mode="json"),
        "context_evidence": [
            item.model_dump(mode="json") for item in value.context_evidence
        ],
        "repository_policy": value.repository_policy.model_dump(mode="json")
        if value.repository_policy
        else None,
    }


def prepare(
    sessions,
    profile_id: str,
    repository: str,
    run_ids: tuple[str, ...],
    output: Path,
    variants: list[str],
):
    output = controlled_path(output)
    if output.exists():
        raise ValueError("不覆盖既有实验计划")
    profile = ReviewProfileRepository(sessions).load(
        profile_id, ResourceScope(repositories=frozenset({repository}))
    )
    if profile["repository"].casefold() != repository.casefold():
        raise ValueError("方案与样本仓库不一致")
    samples = frozen_experiment_inputs(sessions, repository, run_ids)
    if len(
        {(sample.pull_request_number, sample.head_sha) for sample in samples}
    ) != len(samples):
        raise ValueError("一个实验不得重复收录相同 PR/SHA")
    for model in profile["snapshot"]["agents"].values():
        if model.get("pricing") is None:
            raise ValueError("方案缺少价格，不能保证实验估算费用上限")
    body = {
        "schema_version": 1,
        "profile_id": profile_id,
        "profile_fingerprint": profile["fingerprint"],
        "repository": repository,
        "variants": variants,
        "samples": [dump_input(value) for value in samples],
        "context_mode": "frozen_saved_retrieval",
        "human_review_status": "pending",
        "cost_scope": "independent_experiment_excludes_frozen_retrieval_preparation",
        "split": "pilot_not_validation",
    }
    payload = {"sha256": sha256(encoded(body)).hexdigest(), "plan": body}
    if len(encoded(payload)) > 32 * 1024 * 1024:
        raise ValueError("固定输入超过 32 MiB，请减少当前批次的样本")
    with output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return {
        "samples": len(samples),
        "variants": variants,
        "sha256": payload["sha256"],
        "input_bytes": sum(item.total_estimated_input_bytes for item in samples),
        "context_count": sum(len(item.context_evidence) for item in samples),
    }


class ExperimentReviewer:
    def __init__(self, reviewer, settings, accountant):
        self.reviewer, self.settings, self.accountant = reviewer, settings, accountant

    def review(self, review_input):
        with model_budget_scope(self.accountant):
            if review_input.review_agent == ReviewAgent.SUMMARY:
                return self.reviewer.review(review_input)
            batches = plan_model_review_batches(review_input, self.settings)
            results = []
            for batch in batches:
                result = self.reviewer.review(
                    batch.review_input.model_copy(
                        update={"evaluation_batch_number": batch.number}
                    )
                )
                results.append(result)
            return combine_model_review_results(
                review_input, tuple(results), batches=batches
            )

    def close(self) -> None:
        """供应商客户端由共享的基础 runtime 统一关闭。"""


def references_for(sample, runtime):
    knowledge = MarkdownKnowledgeBase()
    chunks = tuple(
        item
        for item in runtime.knowledge_chunks or ()
        if item.repository_scope in (None, sample.repository.casefold())
        and (
            sample.repository_policy.knowledge_sources is None
            or item.source in sample.repository_policy.knowledge_sources
        )
    )
    topics = knowledge.search(
        " ".join(dict.fromkeys(Path(unit.file).stem for unit in sample.units[:32])),
        limit=4,
        chunks=chunks,
    )
    responsibilities = {
        ReviewAgent.SECURITY: "security authorization authentication secrets 安全 鉴权 权限 密钥",
        ReviewAgent.CONVENTION: "coding convention maintainability style 规范 编码 可维护性",
        ReviewAgent.LOGIC: "logic reliability business database correctness 逻辑 可靠性 数据库",
        ReviewAgent.SUMMARY: "evidence findings deduplication 证据 缺陷 误报 合并",
    }
    citations = {
        agent: merge_review_citations(
            topics, knowledge.search(query, limit=8, chunks=chunks)
        )
        for agent, query in responsibilities.items()
    }
    return (
        {
            agent: tuple(
                f"{item.source}#{item.heading}@{item.version}: {item.excerpt}"[:2000]
                for item in items
            )
            for agent, items in citations.items()
        },
        {
            agent: {item.source: item.version for item in items}
            for agent, items in citations.items()
        },
    )


def execute(
    sessions,
    manifest: Path,
    output: Path,
    limit_microusd: int,
    max_requests: int,
    cipher=None,
):
    if limit_microusd <= 0 or not 1 <= max_requests <= 200:
        raise ValueError("实验必须指定正数费用上限和 1 至 200 次请求上限")
    manifest = controlled_path(manifest)
    if manifest.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("实验计划超过 32 MiB")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    body = payload["plan"]
    if sha256(encoded(body)).hexdigest() != payload["sha256"]:
        raise ValueError("实验计划哈希不一致")
    variants = body["variants"]
    if (
        not 1 <= len(variants) <= 4
        or len(set(variants)) != len(variants)
        or any(item not in VARIANTS for item in variants)
    ):
        raise ValueError("实验变体无效")
    samples = tuple(ModelReviewInput.model_validate(value) for value in body["samples"])
    if not 1 <= len(samples) <= 10:
        raise ValueError("实验样本数量无效")
    repository = body["repository"]
    profile = ReviewProfileRepository(sessions).load(
        body["profile_id"], ResourceScope(repositories=frozenset({repository}))
    )
    if (
        profile["fingerprint"] != body["profile_fingerprint"]
        or profile["repository"].casefold() != repository.casefold()
    ):
        raise ValueError("实验方案来源不一致")
    # 执行前重新读取现行外发/暂停策略；修改过的策略需要重新固定计划。
    fresh = frozen_experiment_inputs(
        sessions, repository, tuple(sample.review_run_id for sample in samples)
    )
    if any(dump_input(a) != dump_input(b) for a, b in zip(samples, fresh, strict=True)):
        raise ValueError("源输入或当前策略发生变化，请重新准备实验")
    output = controlled_path(output)
    output.mkdir(mode=0o700)
    (output / "plan.json").write_bytes(encoded(payload))
    budget = ExperimentBudget(output, limit_microusd, max_requests)
    runtime = ReviewProfileRuntimeLoader(
        ReviewProfileRepository(sessions), cipher or AiSecretCipher.from_environment()
    )(body["profile_id"])
    base = runtime.agent_workflow
    if base is None:
        raise ValueError("方案缺少固定 Agent 工作流")
    results = []
    review_items = []
    try:
        for index, sample in enumerate(samples):
            if sample.repository_policy is None:
                raise ValueError("实验样本缺少仓库策略")
            references, versions = references_for(sample, runtime)
            # 轮换执行顺序，减少全部基线先运行产生的时间偏差；不重复使用模型结果。
            ordered = (
                variants[index % len(variants) :] + variants[: index % len(variants)]
            )
            for variant in ordered:
                identity = str(uuid4())
                active = sample.model_copy(
                    update={
                        "review_run_id": identity,
                        "review_plan_id": identity,
                        "reuse_dependencies": {},
                        "knowledge_versions": {
                            key: value
                            for group in versions.values()
                            for key, value in group.items()
                        },
                    }
                )
                wrapped = {
                    agent: ExperimentReviewer(
                        reviewer,
                        base.agent_settings[agent],
                        budget.accountant(
                            identity,
                            agent.value,
                            sample.repository_policy.max_model_requests,
                        ),
                    )
                    for agent, reviewer in base.reviewers.items()
                }
                workflow = FixedAgentWorkflow(
                    wrapped,
                    summary_reviewer=wrapped.get(ReviewAgent.SUMMARY),
                    max_concurrency=1,
                )
                record = {
                    "run_id": identity,
                    "source_run_id": sample.review_run_id,
                    "pr": sample.pull_request_number,
                    "head_sha": sample.head_sha,
                    "variant": variant,
                    "input_sha256": sha256(encoded(dump_input(sample))).hexdigest(),
                }
                started = time.monotonic()
                try:
                    value = workflow.run(
                        active,
                        references=references,
                        reference_versions=versions,
                        allow_partial_aggregation=True,
                        excluded_agents=VARIANTS[variant],
                    )
                    record.update(
                        json.loads(
                            json.dumps(
                                asdict(value),
                                default=lambda value: (
                                    value.model_dump(mode="json")
                                    if isinstance(value, BaseModel)
                                    else value.isoformat()
                                    if isinstance(value, datetime)
                                    else str(value)
                                ),
                            )
                        )
                    )
                except Exception as exc:
                    record.update(
                        status="failed",
                        error_code=SafeError.from_exception(exc).code.value,
                    )
                requests = [
                    row for row in budget.requests.values() if row["run_id"] == identity
                ]
                record.update(
                    elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
                    candidate_count=len(record.get("findings", [])),
                    request_count=len(requests),
                    recorded_input_tokens=sum(
                        row.get("input_tokens") or 0 for row in requests
                    ),
                    recorded_output_tokens=sum(
                        row.get("output_tokens") or 0 for row in requests
                    ),
                    unknown_usage_requests=sum(
                        row.get("input_tokens") is None
                        or row.get("output_tokens") is None
                        for row in requests
                    ),
                    known_cost_microusd=sum(
                        row["estimated_cost_microusd"] or 0 for row in requests
                    ),
                    unknown_cost_requests=sum(
                        row["estimated_cost_microusd"] is None for row in requests
                    ),
                    human_review_status="pending",
                )
                (output / (identity + ".result.json")).write_bytes(encoded(record))
                review_items.append(
                    {
                        "run_id": identity,
                        "source_run_id": sample.review_run_id,
                        "variant": variant,
                        "repository": sample.repository,
                        "pr": sample.pull_request_number,
                        "head_sha": sample.head_sha,
                        "reference_defects": None,
                        "reference_reviewers": [],
                        "reviewer_submissions": [],
                        "findings": [
                            {
                                "key": sha256(encoded(finding)).hexdigest(),
                                "finding": finding,
                                "human_decisions": [],
                            }
                            for finding in record.get("findings", [])
                        ],
                    }
                )
                results.append(
                    {
                        key: record.get(key)
                        for key in (
                            "run_id",
                            "source_run_id",
                            "pr",
                            "head_sha",
                            "variant",
                            "status",
                            "request_count",
                            "known_cost_microusd",
                            "unknown_cost_requests",
                            "elapsed_ms",
                            "candidate_count",
                            "recorded_input_tokens",
                            "recorded_output_tokens",
                            "unknown_usage_requests",
                        )
                    }
                )
    finally:
        base.close()
        report = {
            "plan_sha256": payload["sha256"],
            "application_image": os.environ.get("OPENREVIEWER_DEPLOYMENT_IMAGE"),
            "profile_fingerprint": body["profile_fingerprint"],
            "scope": body["cost_scope"],
            "split": body["split"],
            "results": results,
            "planned_runs": len(samples) * len(variants),
            "finished_runs": len(results),
            "requests": len(budget.requests),
            "reserved_upper_bound_microusd": budget.reserved,
            "budget_limit_microusd": budget.limit,
            "human_review_status": "pending",
            "quality_metrics": None,
        }
        (output / "report.json").write_bytes(encoded(report))
        (output / "human-review-packet.json").write_bytes(
            encoded(
                {
                    "status": "awaiting_real_reviewers",
                    "notes": "候选数量不等于有效问题；不得用模型意见填写 human_decisions。当前为试点，不满足正式验收。",
                    "items": review_items,
                }
            )
        )
        files = {
            path.name: {
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()).hexdigest(),
            }
            for path in output.iterdir()
            if path.is_file()
        }
        (output / "manifest.json").write_bytes(encoded({"files": files}))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    draft = sub.add_parser("prepare")
    draft.add_argument("--repository", required=True)
    draft.add_argument("--profile-id", required=True)
    draft.add_argument("--run-id", action="append", required=True)
    draft.add_argument(
        "--variants", nargs="+", choices=tuple(VARIANTS), default=list(VARIANTS)
    )
    draft.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--budget-usd", type=Decimal, required=True)
    run.add_argument("--max-requests", type=int, default=100)
    args = parser.parse_args()
    database = Database.from_environment()
    try:
        result = (
            prepare(
                database.sessions,
                args.profile_id,
                args.repository,
                tuple(args.run_id),
                args.output,
                args.variants,
            )
            if args.command == "prepare"
            else execute(
                database.sessions,
                args.manifest,
                args.output,
                int(args.budget_usd * 1_000_000),
                args.max_requests,
            )
        )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
