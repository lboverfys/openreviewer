"""实验必须固定来源、独立调用并限费；MockTransport 验证真实协议组装，不收费。"""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256

import httpx
import pytest
from sqlalchemy import event, select, update

from apps.maintenance.run_review_experiment import VARIANTS, execute, prepare
from domain.enums import ReviewAgent
from domain.evaluation_outputs import CapturedModelOutput
from domain.platform import PlatformNotFoundError
from domain.security import SafeApplicationError
from persistence.experiment_inputs import frozen_experiment_inputs
from persistence.models import RepositoryPolicyRecord, ReviewRunRecord, ReviewUnitRecord
from services.agent_workflow import PARALLEL_AGENTS, FixedAgentWorkflow
from services.experiment_accounting import ExperimentBudget
from services.model_budget import ModelBudgetRequest
from services.model_providers import create_model_reviewer
from tests.evaluation_support import seed_evaluation_runs
from tests.integration.test_management_api import database as database
from tests.integration.test_review_profiles import ALL, draft, profile_services
from tests.support import TEST_USERNAME
from tests.unit.test_model_review import make_model_input
from tests.unit.test_workflow import StaticReviewer, model_result


def test_ablation_cannot_exclude_summary_or_disguise_an_unconfigured_role():
    workflow=FixedAgentWorkflow({})
    for excluded in (frozenset({ReviewAgent.SUMMARY}),frozenset(PARALLEL_AGENTS)):
        with pytest.raises(ValueError,match="一个"):
            workflow.run(make_model_input(),excluded_agents=excluded)
    calls=[]
    workflow=FixedAgentWorkflow({ReviewAgent.LOGIC:StaticReviewer(model_result("1"),name="logic",calls=calls)})
    result=workflow.run(make_model_input(),excluded_agents=frozenset({ReviewAgent.SECURITY}))
    assert result.status=="failed"
    assert {row.agent:row.status for row in result.agents}=={ReviewAgent.SECURITY:"excluded",ReviewAgent.CONVENTION:"disabled",ReviewAgent.LOGIC:"completed"}


def source(database, tmp_path):
    _, _, _, service, _, _ = profile_services(database, tmp_path)
    profile = service.create(draft(), TEST_USERNAME, ALL)
    ids = seed_evaluation_runs(
        database, [{"label": "real-fixture", "pr": 1, "findings": []}]
    )
    with database.sessions() as session:
        row = session.scalar(select(ReviewUnitRecord))
        row.patch_sha256 = sha256(row.patch.encode()).hexdigest()
        row.review_domains = ["security", "convention", "logic"]
        session.commit()
    return profile, ids["real-fixture"], service.cipher


def test_frozen_inputs_use_fixed_queries_and_reject_wrong_repository(
    database, tmp_path
):
    profile, run_id, _ = source(database, tmp_path)
    statements = []

    def capture(_conn, _cursor, sql, *_args):
        if sql.lstrip().upper().startswith("SELECT"):
            statements.append(sql)

    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        result = frozen_experiment_inputs(
            database.sessions, profile.repository, (run_id,)
        )
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)
    assert len(statements) == 5 and result[0].review_run_id == run_id
    with pytest.raises(ValueError, match="仓库"):
        frozen_experiment_inputs(database.sessions, "wrong/repo", (run_id,))
    with database.sessions() as session, session.begin():
        session.execute(
            update(RepositoryPolicyRecord).values(
                policy={"monthly_budget_microusd": 100}
            )
        )
    with pytest.raises(ValueError, match="月度"):
        frozen_experiment_inputs(database.sessions, profile.repository, (run_id,))


def test_ablation_makes_fresh_calls_keeps_production_unchanged_and_has_no_human_scores(
    database, tmp_path, monkeypatch
):
    profile, run_id, cipher = source(database, tmp_path)
    plan = tmp_path / "plan.json"
    prepare(
        database.sessions,
        profile.id,
        profile.repository,
        (run_id,),
        plan,
        list(VARIANTS),
    )
    requests = []

    # 真实方案可能跨角色产生超过单次上限的知识；测试必须走实际分批与协议验证。
    reference_groups = {agent: tuple(f"{agent.value}-{number}.md#rule@v1: 已核对的测试规则"
        for number in range(8)) for agent in (*PARALLEL_AGENTS, ReviewAgent.SUMMARY)}
    version_groups = {agent: {f"{agent.value}-{number}.md": "v1" for number in range(8)}
        for agent in reference_groups}
    monkeypatch.setattr("apps.maintenance.run_review_experiment.references_for",
        lambda *_args: (reference_groups, version_groups))

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "verdict": "no_actionable_issue",
                                        "summary": "无确认问题",
                                        "checked_areas": ["变更"],
                                        "findings": [],
                                    }
                                ),
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            "services.review_profiles.create_model_reviewer",
            lambda settings: create_model_reviewer(settings, client=client),
        )
        report = execute(
            database.sessions, plan, tmp_path / "results", 1_000_000, 100, cipher
        )
    assert len(requests) == report["requests"] == 13
    assert len({row["run_id"] for row in report["results"]}) == 6
    assert {row["variant"]: row["request_count"] for row in report["results"]} == {
        "full": 3,
        "single_generalist": 1,
        "without_context": 3,
        "without_security": 2,
        "without_convention": 2,
        "without_logic": 2,
    }
    assert all(
        row["status"] == "completed" and row["unknown_cost_requests"] == 0
        for row in report["results"]
    )
    assert (
        report["quality_metrics"] is None and report["human_review_status"] == "pending"
    )
    with database.sessions() as session:
        assert len(session.scalars(select(ReviewRunRecord.id)).all()) == 1
        assert (
            session.scalar(select(RepositoryPolicyRecord.policy)).get(
                "review_profile_id"
            )
            is None
        )
    metadata = json.loads((tmp_path / "results" / "manifest.json").read_text())
    assert (
        len([name for name in metadata["files"] if name.endswith(".output.json")]) == 13
    )
    for path in (tmp_path / "results").iterdir():
        assert "fixture-private-key" not in path.read_text(encoding="utf-8")
    values = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "results").glob("*.result.json")
    ]
    value = next(item for item in values if item["variant"] == "full")
    assert isinstance(value["agents"][0]["result"]["output"], dict)
    single = next(item for item in values if item["variant"] == "single_generalist")
    assert single["agents"] == [] and single["generalist_model_source"] == "logic"
    assert len(report["variant_summaries"]) == 6
    assert all(row["valid_findings"] is None for row in report["variant_summaries"])


def test_tampered_plan_and_cross_repo_profile_stop_before_calls(database, tmp_path):
    profile, run_id, cipher = source(database, tmp_path)
    plan = tmp_path / "plan.json"
    with pytest.raises(PlatformNotFoundError):
        prepare(database.sessions, profile.id, "wrong/repo", (run_id,), plan, ["full"])
    prepare(
        database.sessions, profile.id, profile.repository, (run_id,), plan, ["full"]
    )
    payload = json.loads(plan.read_text())
    payload["plan"]["samples"][0]["head_sha"] = "b" * 40
    plan.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="哈希"):
        execute(database.sessions, plan, tmp_path / "results", 100, 10, cipher)
    assert not (tmp_path / "results").exists()


def test_experiment_budget_is_atomic_and_unknown_cost_does_not_release_reservations(
    tmp_path,
):
    budget = ExperimentBudget(tmp_path, 30, 20)
    request = ModelBudgetRequest("openai", "responses", "fixture", 10, 10, 10, 10)
    account = budget.accountant("run", "logic")

    def reserve(_):
        try:
            return account.reserve(request)
        except SafeApplicationError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        values = [item for item in pool.map(reserve, range(8)) if item is not None]
    assert len(values) == 3 and budget.reserved == 30
    account.settle(
        values[0],
        input_tokens=None,
        output_tokens=None,
        estimated_cost_microusd=None,
        response_status=None,
        duration_ms=1,
        uncertain=True,
    )
    account.settle(
        values[0],
        input_tokens=1,
        output_tokens=1,
        estimated_cost_microusd=1,
        response_status=200,
        duration_ms=1,
    )
    with pytest.raises(SafeApplicationError):
        account.reserve(request)
    with pytest.raises(SafeApplicationError):
        account.reserve(replace(request, cost_upper_bound_microusd=None))
    assert budget.requests[values[0].id]["status"] == "uncertain"
    account.record_output(
        values[0], CapturedModelOutput(status="captured", text="可见答案", byte_size=12)
    )
    assert (
        json.loads(
            (tmp_path / (values[0].id + ".output.json")).read_text(encoding="utf-8")
        )["text"]
        == "可见答案"
    )
