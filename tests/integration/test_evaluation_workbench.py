"""真实数据库验证样本隔离、批量快照、双人复核和公平配对口径。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import httpx
import pytest
from sqlalchemy import delete, event, select, update

from apps.api.main import create_app
from domain.enums import FindingEvaluationVerdict
from domain.evaluation_workbench import (
    EvaluationArchive,
    EvaluationConflictError,
    EvaluationDatasetCreate,
    EvaluationDecision,
    EvaluationNotFoundError,
    FindingReviewWrite,
    ObservationImport,
    ObservationReplace,
    ReferenceDefect,
    ReferenceReviewWrite,
    ReferenceUpdate,
)
from persistence.evaluation_sources import capture_review_sources
from persistence.models import (
    EvaluationDatasetRecord,
    ReviewFindingRecord,
    ReviewRunRecord,
)
from services.auth import AuthService, AuthSettings, UserCredential
from services.evaluation_workbench import EvaluationWorkbench
from services.rbac import AccessRole, ResourceScope
from tests.evaluation_support import seed_evaluation_runs
from tests.integration.test_management_api import database as database
from tests.integration.test_postgres_contract import (
    postgres_database as postgres_database,
)
from tests.support import TEST_HASHER, TEST_PASSWORD, TEST_PASSWORD_HASH, TEST_USERNAME

ALL = ResourceScope.unrestricted_scope()
OWN = ResourceScope(repositories=frozenset({"lboverfys/NiuMa"}))
DENIED = ResourceScope(repositories=frozenset({"other/private"}))


def create_pair(database, *, split="validation", baseline_findings=None, candidate_findings=None, candidate_cost=2000):
    runs = seed_evaluation_runs(database, [
        {"label":"base","pr":301,"findings":baseline_findings if baseline_findings is not None else ["权限问题","无效建议"],"cost":1000},
        {"label":"candidate","pr":301,"findings":candidate_findings if candidate_findings is not None else ["已找到权限问题"],"cost":candidate_cost,"model":"candidate-model"},
    ])
    service = EvaluationWorkbench(database.sessions)
    dataset = service.create_dataset(EvaluationDatasetCreate(
        name="授权评测", review_run_ids=(runs["base"],), split=split,
    ), "create-pair", TEST_USERNAME, ALL)
    service.import_observations(dataset.id, ObservationImport(
        review_run_ids=(runs["candidate"],), variant="candidate", split=split,
    ), TEST_USERNAME, ALL)
    sample = service.cases(dataset.id, ALL).items[0]
    return service, dataset, sample.id, runs


def set_reference(service, case_id):
    sample = service.case(case_id, ALL)
    sample = service.update_reference(case_id, ReferenceUpdate(
        expected_revision=sample.revision,
        reference_defects=(ReferenceDefect(key="auth",title="缺少资源归属校验",category="authorization",file="src/service.py",start_line=1),),
    ), TEST_USERNAME, ALL)
    for actor in ("alice","bob"):
        sample = service.review_reference(case_id, ReferenceReviewWrite(
            expected_revision=sample.revision, agrees=True,
        ), actor, ALL)


def submit_ballot(service, case_id, variant, actor, decisions):
    observation = service.observation(case_id, variant, ALL)
    findings = service.findings(case_id, variant, ALL, limit=100).items
    for finding, (verdict, reference) in zip(findings, decisions, strict=True):
        observation = service.review_finding(case_id, variant, finding.finding.id, FindingReviewWrite(
            expected_revision=observation.observation.revision,
            decision=EvaluationDecision(verdict=verdict, reference_key=reference, location_correct=True),
        ), actor, ALL)
    return service.submit_review(case_id, variant, observation.observation.revision, actor, ALL)


def test_batch_capture_uses_fixed_queries_and_freezes_redacted_code(database):
    secret = "sk-" + "x" * 40
    runs = seed_evaluation_runs(database, [
        {"label":f"run-{number}","pr":400+number,"evidence":"凭据 "+secret,"patch":"@@ -1 +1 @@\n-old\n+"+secret+"\n"}
        for number in range(3)
    ])
    statements = []
    def capture(_conn,_cursor,statement,_params,_context,_many):
        statements.append(statement)
    event.listen(database.engine,"before_cursor_execute",capture)
    try:
        with database.sessions() as session:
            sources = capture_review_sources(session,tuple(runs.values()),OWN,datetime.now(UTC))
        assert len(statements) == 6
        assert all("SELECT *" not in statement for statement in statements)
        assert len(sources) == 3
        for source in sources.values():
            assert secret not in source.model_dump_json()
            assert source.changes[0].file == "src/service.py"
            assert source.models[0].application_revision == "d" * 40
            assert source.models[0].knowledge_versions == {"rules.md":"e"*16}
    finally:
        event.remove(database.engine,"before_cursor_execute",capture)


def test_import_rejects_cross_scope_changed_sha_and_split_leakage(database):
    service,dataset,case_id,runs = create_pair(database,split="tuning")
    with pytest.raises(EvaluationNotFoundError):
        service.dataset(dataset.id,DENIED)
    assert not service.datasets(DENIED).items
    with pytest.raises(EvaluationNotFoundError):
        service.case(case_id,DENIED)
    with pytest.raises(EvaluationNotFoundError):
        service.observation(case_id,"baseline",DENIED)
    with pytest.raises(EvaluationNotFoundError):
        service.report(dataset.id,DENIED)
    duplicate = service.import_observations(dataset.id,ObservationImport(
        review_run_ids=(runs["base"],),split="tuning",
    ),TEST_USERNAME,ALL)
    assert duplicate.imported == 0
    with pytest.raises(EvaluationConflictError,match="调参集"):
        service.import_observations(dataset.id,ObservationImport(
            review_run_ids=(runs["base"],),split="validation",
        ),TEST_USERNAME,ALL)
    different = seed_evaluation_runs(database,[{"label":"new-sha","pr":301,"head_sha":"b"*40}])
    with pytest.raises(EvaluationConflictError,match="SHA"):
        service.import_observations(dataset.id,ObservationImport(
            review_run_ids=(different["new-sha"],),split="tuning",variant="candidate",
        ),TEST_USERNAME,ALL)
    with pytest.raises(EvaluationNotFoundError):
        with database.sessions() as session:
            capture_review_sources(session,(runs["base"],),DENIED,datetime.now(UTC))
    assert service.cases(dataset.id,ALL).items[0].head_sha == "a"*40


def test_pair_report_accepts_one_reviewer_for_each_result_and_reference(database):
    service,dataset,case_id,_runs = create_pair(database)
    set_reference(service,case_id)
    initial = service.report(dataset.id,ALL)
    assert initial.performance_pairs == 1 and initial.quality_pairs == 0
    assert initial.baseline.precision is None and initial.baseline.recall is None
    assert initial.baseline.mean_estimated_cost_usd == 0.001
    assert initial.candidate.mean_estimated_cost_usd == 0.002
    base = [(FindingEvaluationVerdict.VALID,"auth"),(FindingEvaluationVerdict.FALSE_POSITIVE,None)]
    candidate = [(FindingEvaluationVerdict.VALID,"auth")]
    for variant,decisions in (("baseline",base),("candidate",candidate)):
        submit_ballot(service,case_id,variant,"alice",decisions)
    report = service.report(dataset.id,ALL)
    assert report.quality_pairs == report.reference_pairs == 1
    assert report.baseline.precision == 0.5 and report.candidate.precision == 1
    assert report.baseline.recall == report.candidate.recall == 1
    assert report.baseline.reference_expected_count == 1
    assert report.deltas["precision"] == 0.5
    assert report.baseline.precision_ci95["lower"] < 0.5 < report.baseline.precision_ci95["upper"]


def test_disagreements_unknown_price_and_missing_pairs_are_explicit(database):
    service,dataset,case_id,_runs = create_pair(database,baseline_findings=["问题"],candidate_cost=None)
    submit_ballot(service,case_id,"baseline","alice",[("valid",None)])
    submit_ballot(service,case_id,"baseline","bob",[("false_positive",None)])
    for actor in ("alice","bob"):
        submit_ballot(service,case_id,"candidate",actor,[("valid",None)])
    report = service.report(dataset.id,ALL)
    assert report.disputed_pairs == 0 and report.quality_pairs == 1
    assert report.baseline.false_positive_count == 1
    assert report.priced_pairs == 0 and report.baseline.mean_estimated_cost_usd is None
    assert report.candidate.mean_estimated_cost_usd is None
    assert report.baseline.recall is None
    extra = seed_evaluation_runs(database,[{"label":"unpaired","pr":302}])
    service.import_observations(dataset.id,ObservationImport(review_run_ids=(extra["unpaired"],),split="validation"),TEST_USERNAME,ALL)
    report = service.report(dataset.id,ALL)
    assert report.case_count == 2 and report.performance_pairs == 1
    assert report.missing_candidate == 1
    assert service.report(dataset.id,ALL,"tuning").case_count == 0


def test_empty_predictions_need_two_submissions_and_can_miss_known_defects(database):
    service,dataset,case_id,_runs = create_pair(database,baseline_findings=[],candidate_findings=[])
    set_reference(service,case_id)
    for variant in ("baseline","candidate"):
        for actor in ("alice","bob"):
            submit_ballot(service,case_id,variant,actor,[])
    report = service.report(dataset.id,ALL)
    assert report.quality_pairs == 1
    assert report.baseline.precision is None
    assert report.baseline.recall == 0
    assert report.baseline.reference_false_negative_count == 1


def test_source_replacement_and_reference_edits_reset_reviews_explicitly(database):
    service,dataset,case_id,runs = create_pair(database,baseline_findings=["问题"])
    before = service.observation(case_id,"baseline",ALL)
    service.submit_review(case_id,"baseline",before.observation.revision,"alice",ALL)
    completed = submit_ballot(service,case_id,"baseline","alice",[("valid",None)])
    with pytest.raises(EvaluationConflictError):
        service.submit_review(case_id,"baseline",before.observation.revision,"alice",ALL)
    sample = service.case(case_id,ALL)
    with pytest.raises(EvaluationConflictError,match="清空"):
        service.update_reference(case_id,ReferenceUpdate(expected_revision=sample.revision,reference_defects=()),"alice",ALL)
    updated = service.update_reference(case_id,ReferenceUpdate(
        expected_revision=sample.revision,reference_defects=(),reset_reviews=True,
    ),"alice",ALL)
    assert updated.baseline.assessment_status == "pending"
    assert not service.observation(case_id,"baseline",ALL).ballots
    new = seed_evaluation_runs(database,[{"label":"replacement","pr":301}])
    observation = service.observation(case_id,"baseline",ALL)
    replaced = service.replace_observation(case_id,"baseline",ObservationReplace(
        expected_revision=observation.observation.revision,review_run_id=new["replacement"],
    ),"alice",ALL)
    assert replaced.observation.source_run_id == new["replacement"]
    with pytest.raises(EvaluationConflictError,match="同时"):
        service.replace_observation(case_id,"baseline",ObservationReplace(
            expected_revision=replaced.observation.revision,review_run_id=runs["candidate"],
        ),"alice",ALL)
    archived = service.archive(dataset.id,EvaluationArchive(
        expected_revision=service.dataset(dataset.id,ALL).revision,archived=True,
    ),"alice",ALL)
    assert archived.archived_at is not None
    with pytest.raises(EvaluationConflictError,match="归档"):
        service.submit_review(case_id,"baseline",replaced.observation.revision,"alice",ALL)
    assert service.report(dataset.id,ALL).performance_pairs == 1
    restored = service.archive(dataset.id,EvaluationArchive(expected_revision=archived.revision,archived=False),"alice",ALL)
    assert restored.archived_at is None
    assert completed.ballots[0].submitted_at is not None


def test_case_and_finding_pagination_and_single_query_report(database):
    runs = seed_evaluation_runs(database,[{"label":str(number),"pr":500+number,"findings":[f"问题 {index}" for index in range(13)]} for number in range(13)])
    service = EvaluationWorkbench(database.sessions)
    dataset = service.create_dataset(EvaluationDatasetCreate(name="分页",review_run_ids=tuple(runs.values())),"pagination","alice",ALL)
    first = service.cases(dataset.id,ALL)
    second = service.cases(dataset.id,ALL,cursor=first.next_cursor)
    assert len(first.items)==10 and len(second.items)==3
    assert not {item.id for item in first.items}.intersection(item.id for item in second.items)
    case_id = first.items[0].id
    page = service.findings(case_id,"baseline",ALL)
    next_page = service.findings(case_id,"baseline",ALL,cursor=page.next_cursor)
    assert len(page.items)==10 and len(next_page.items)==3
    assert service.changes(case_id,"baseline",ALL).items[0].file == "src/service.py"
    statements = []
    def capture(_conn,_cursor,statement,_params,_context,_many): statements.append(statement)
    event.listen(database.engine,"before_cursor_execute",capture)
    try:
        service.report(dataset.id,ALL,"tuning")
        assert len(statements)==1
        assert "source_snapshot" not in statements[0] and "ballots" not in statements[0]
    finally:
        event.remove(database.engine,"before_cursor_execute",capture)


def test_postgres_snapshot_survives_source_cleanup_and_reviews_use_cas(postgres_database):
    service,dataset,case_id,runs = create_pair(postgres_database,baseline_findings=["问题"],candidate_findings=[])
    with postgres_database.sessions() as session:
        session.execute(update(ReviewFindingRecord).where(
            ReviewFindingRecord.review_run_id == runs["base"],
        ).values(title="原运行已变化"))
        session.commit()
    assert service.findings(case_id,"baseline",ALL).items[0].finding.title == "问题"
    with postgres_database.sessions() as session:
        session.execute(delete(ReviewRunRecord).where(ReviewRunRecord.id.in_(tuple(runs.values()))))
        session.commit()
    assert service.findings(case_id,"baseline",ALL).items[0].finding.title == "问题"
    assert service.changes(case_id,"baseline",ALL).items
    sample = service.case(case_id,ALL)
    barrier = Barrier(2)
    def replace_reference(actor):
        barrier.wait(timeout=10)
        try:
            service.update_reference(case_id,ReferenceUpdate(expected_revision=sample.revision,reference_defects=()),actor,ALL)
            return True
        except EvaluationConflictError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(replace_reference,("alice","bob")))
    assert sum(results)==1
    assert service.report(dataset.id,ALL).performance_pairs==1


def test_evaluation_api_uses_login_identity_and_enforces_scope_and_csrf(database):
    runs=seed_evaluation_runs(database,[{"label":"source","pr":601}])
    auth=AuthService(AuthSettings(
        username=TEST_USERNAME,password_hash=TEST_PASSWORD_HASH,
        session_secret=b"test-evaluation-session-secret-long-enough",cookie_secure=False,
        additional_users=(
            UserCredential(username="alice",password_hash=TEST_PASSWORD_HASH,role=AccessRole.ADJUDICATOR,resource_scope=OWN),
            UserCredential(username="viewer",password_hash=TEST_PASSWORD_HASH,role=AccessRole.VIEWER,resource_scope=OWN),
            UserCredential(username="outsider",password_hash=TEST_PASSWORD_HASH,role=AccessRole.ADJUDICATOR,resource_scope=DENIED),
        ),
    ),password_hasher=TEST_HASHER)
    application=create_app(auth_service=auth,evaluation_service=EvaluationWorkbench(database.sessions))
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url="http://testserver") as client:
            assert (await client.get("/api/v1/evaluations/datasets")).status_code==401
            await client.post("/api/v1/auth/login",json={"username":"alice","password":TEST_PASSWORD})
            body={"name":"API评测","review_run_ids":[runs["source"]]}
            response=await client.post("/api/v1/evaluations/datasets",json=body,headers={"Idempotency-Key":"api-create"})
            assert response.status_code==201,response.text
            identifier=response.json()["id"]
            repeated=await client.post("/api/v1/evaluations/datasets",json=body,headers={"Idempotency-Key":"api-create"})
            assert repeated.json()["id"]==identifier
            with database.sessions() as session:
                assert len(session.scalars(select(EvaluationDatasetRecord.id).limit(2)).all()) == 1
            assert (await client.get("/api/v1/evaluations/sources")).status_code==200
            sample=(await client.get("/api/v1/evaluations/datasets/"+identifier+"/cases")).json()["items"][0]
            invalid=await client.put("/api/v1/evaluations/cases/"+sample["id"]+"/reference",json={
                "expected_revision":sample["revision"],"reference_defects":[],"reviewer":"bob",
            })
            assert invalid.status_code==422
            denied=await client.put("/api/v1/evaluations/cases/"+sample["id"]+"/reference",json={
                "expected_revision":sample["revision"],"reference_defects":[],
            },headers={"Origin":"https://untrusted.example"})
            assert denied.status_code==403
            await client.post("/api/v1/auth/login",json={"username":"viewer","password":TEST_PASSWORD})
            assert (await client.post("/api/v1/evaluations/datasets",json=body,headers={"Idempotency-Key":"viewer-create"})).status_code==403
            assert (await client.get("/api/v1/evaluations/datasets/"+identifier+"/report")).status_code==200
            await client.post("/api/v1/auth/login",json={"username":"outsider","password":TEST_PASSWORD})
            assert (await client.get("/api/v1/evaluations/datasets")).json()["items"]==[]
            assert (await client.get("/api/v1/evaluations/datasets/"+identifier+"/report")).status_code==404
            assert (await client.get("/api/v1/evaluations/cases/"+sample["id"])).status_code==404
    asyncio.run(exercise())


def test_same_reviewer_is_not_duplicated_and_another_account_can_continue(database):
    service,dataset,case_id,_runs = create_pair(database,baseline_findings=["问题"])
    submit_ballot(service,case_id,"baseline","alice",[("valid",None)])
    repeated = submit_ballot(service,case_id,"baseline","ALICE",[("valid",None)])
    assert len(repeated.ballots) == 1
    assert repeated.observation.assessment_status == "complete"
    submit_ballot(service,case_id,"baseline","bob",[("valid",None)])
    current = service.observation(case_id,"baseline",ALL)
    service.submit_review(case_id,"baseline",current.observation.revision,"charlie",ALL)
    assert service.report(dataset.id,ALL).quality_pairs == 0
