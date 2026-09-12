"""团队权限、策略版本和审查入口的完整数据库回归。"""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import event, select

from apps.api.main import create_app
from domain.enums import CiState, ExecutionStatus
from domain.models import ReviewRequest
from domain.repository_policy import RepositoryPolicy, RepositoryPolicyDeniedError
from persistence.auth import SqlAlchemySessionStore
from persistence.dashboard import SqlAlchemyDashboardRepository
from persistence.models import OutboxEventRecord, ReviewRunRecord, TeamMemberRecord
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.review_management import SqlAlchemyReviewManagementRepository
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from persistence.team import SqlAlchemyMemberStore
from services.auth import AuthService, InvalidSessionError, UserCredential
from services.dashboard import DashboardService
from services.rbac import AccessRole, ResourceScope
from services.review_management import (
    ReviewAction,
    ReviewActionConflictError,
    ReviewManagementService,
)
from services.reviews import ReviewService
from services.team import MemberScope, MemberWrite, RepositoryWrite, TeamService
from tests.integration.test_finding_pagination import _seed_review
from tests.integration.test_github_context_persistence import _context, _submit
from tests.integration.test_management_api import database as database
from tests.integration.test_webhook_api import (
    application_for as webhook_application,
)
from tests.integration.test_webhook_api import encode, post_webhook, webhook_payload
from tests.support import (
    TEST_GITHUB_ACCESS_POLICY,
    TEST_HASHER,
    TEST_PASSWORD,
    TEST_USERNAME,
    make_auth_service,
)


def _services(database):
    member_store = SqlAlchemyMemberStore(database.sessions)
    authentication = AuthService(
        make_auth_service().settings, password_hasher=TEST_HASHER,
        member_store=member_store,
        session_store=SqlAlchemySessionStore(database.sessions),
    )
    team = TeamService(database.sessions, TEST_USERNAME, password_hasher=TEST_HASHER)
    application = create_app(
        auth_service=authentication, team_service=team,
        dashboard_service=DashboardService(SqlAlchemyDashboardRepository(database.sessions)),
        review_management_service=ReviewManagementService(
            SqlAlchemyReviewManagementRepository(database.sessions),
        ),
        review_service=ReviewService(SqlAlchemyReviewRepository(database.sessions)),
        github_access_policy=TEST_GITHUB_ACCESS_POLICY,
    )
    return authentication, team, application


def test_member_access_changes_revoke_existing_sessions_and_preserve_administrator(database):
    _, _, application = _services(database)
    run_id = _seed_review(database, finding_count=0)

    async def exercise():
        transport = httpx.ASGITransport(app=application)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin,
            httpx.AsyncClient(transport=transport, base_url="http://testserver") as member,
        ):
            assert (await admin.get("/api/v1/team/members")).status_code == 401
            assert (await admin.post("/api/v1/auth/login", json={
                "username": TEST_USERNAME, "password": TEST_PASSWORD,
            })).status_code == 200
            body = {
                "expected_revision": 0, "role": "viewer", "enabled": True,
                "password": TEST_PASSWORD,
                "scope": {"repositories": ["lboverfys/NiuMa"]},
            }
            created = await admin.put("/api/v1/team/members/reviewer", json=body)
            assert created.status_code == 200, created.text
            assert created.json()["revision"] == 1
            assert "password_hash" not in created.text and TEST_PASSWORD not in created.text
            assert (await admin.put("/api/v1/team/members/reviewer", json=body)).status_code == 409
            assert (await admin.put("/api/v1/team/members/" + TEST_USERNAME, json=body)).status_code == 422
            assert (await admin.put("/api/v1/team/members/outsider", json=body,
                headers={"Origin": "https://untrusted.example"})).status_code == 403
            assert (await member.post("/api/v1/auth/login", json={
                "username": "reviewer", "password": TEST_PASSWORD,
            })).status_code == 200
            assert (await member.get("/api/v1/team/members")).status_code == 403
            assert (await member.get("/api/v1/team/repositories")).status_code == 403
            assert (await member.get("/api/v1/reviews/" + run_id)).status_code == 200
            assert (await member.get("/api/v1/reviews")).json()["total"] == 1

            body.pop("password")
            body.update(expected_revision=1, scope={"repositories": ["other/repo"]})
            updated = await admin.put("/api/v1/team/members/reviewer", json=body)
            assert updated.status_code == 200
            assert (await member.get("/api/v1/auth/me")).status_code == 401
            assert (await member.post("/api/v1/auth/login", json={
                "username": "reviewer", "password": TEST_PASSWORD,
            })).status_code == 200
            assert (await member.get("/api/v1/reviews/" + run_id)).status_code == 404
            assert (await member.get("/api/v1/reviews")).json()["total"] == 0

            body.update(expected_revision=2, enabled=False)
            assert (await admin.put("/api/v1/team/members/reviewer", json=body)).status_code == 200
            assert (await member.get("/api/v1/auth/me")).status_code == 401
            assert (await member.post("/api/v1/auth/login", json={
                "username": "reviewer", "password": TEST_PASSWORD,
            })).status_code == 401
            assert (await admin.get("/api/v1/auth/me")).status_code == 200
            audit = await admin.get("/api/v1/team/audits")
            assert audit.status_code == 200 and len(audit.json()["items"]) == 3
            assert TEST_PASSWORD not in audit.text and "$argon2" not in audit.text

    asyncio.run(exercise())


def test_legacy_members_import_once_and_login_race_cannot_issue_current_token(database):
    from dataclasses import replace

    settings = replace(make_auth_service().settings, additional_users=(
        UserCredential(
            username="legacy", password_hash=TEST_HASHER.hash(TEST_PASSWORD),
            role=AccessRole.VIEWER,
            resource_scope=ResourceScope(repositories=frozenset({"lboverfys/NiuMa"})),
        ),
        UserCredential(
            username="limited-admin", password_hash=TEST_HASHER.hash(TEST_PASSWORD),
            role=AccessRole.ADMINISTRATOR,
            resource_scope=ResourceScope(repositories=frozenset({"lboverfys/NiuMa"})),
        ),
    ))
    store = SqlAlchemyMemberStore(database.sessions)
    store.bootstrap(settings)
    limited_admin = store.find_user("limited-admin")
    assert limited_admin is not None
    assert limited_admin.resource_scope.allows(10, "lboverfys/NiuMa")
    assert not limited_admin.resource_scope.allows(10, "other/private")
    auth = AuthService(settings, member_store=store, password_hasher=TEST_HASHER)
    authenticated = auth.authenticate("legacy", TEST_PASSWORD)
    assert authenticated is not None
    team = TeamService(database.sessions, TEST_USERNAME, password_hasher=TEST_HASHER)
    team.save_member("legacy", MemberWrite(
        expected_revision=1, role=AccessRole.VIEWER, enabled=False,
        scope=MemberScope(repositories=("lboverfys/NiuMa",)),
    ), TEST_USERNAME)
    store.bootstrap(settings)
    assert store.find_user("legacy") is None
    with pytest.raises(InvalidSessionError):
        auth.create_session(authenticated)
    with database.sessions() as session:
        row = session.get(TeamMemberRecord, "legacy")
        assert row.revision == 2 and row.enabled is False


def test_repository_policy_snapshot_is_frozen_and_rejected_branch_does_not_review(database):
    _, team, _ = _services(database)
    first = team.save_repository(RepositoryWrite(
        repository="lboverfys/NiuMa", expected_revision=0,
        policy=RepositoryPolicy(target_branches=("release/*",), max_model_requests=3),
    ), TEST_USERNAME)
    task_id, run_id = _submit(database, "policy-snapshot", "a" * 40)
    team.save_repository(RepositoryWrite(
        repository=first.repository, expected_revision=1,
        policy=RepositoryPolicy(enabled=False, target_branches=("main",)),
    ), TEST_USERNAME, first.id)
    with pytest.raises(RepositoryPolicyDeniedError):
        _submit(database, "paused-repository", "b" * 40)
    now = datetime.now(UTC)
    queue = SqlAlchemyReviewTaskQueue(database.sessions)
    lease = queue.claim_next("policy-worker", timedelta(minutes=1))
    assert lease is not None and lease.task_id == task_id
    result = queue.store_github_context(
        lease, _context("a" * 40, CiState.SUCCESS, now, include_files=True),
        ci_poll_interval=timedelta(seconds=30), ci_wait_timeout=timedelta(minutes=5),
    )
    assert result is ExecutionStatus.CANCELLED
    details = SqlAlchemyReviewManagementRepository(database.sessions).get(run_id)
    assert details.repository_policy.revision == 1
    assert details.repository_policy.max_model_requests == 3
    assert details.repository_policy.enabled is True
    assert details.review_plan_id is None
    assert any(item.event_type == "review.policy_skipped" for item in details.events)


def test_designated_approver_is_enforced_but_administrator_can_intervene(database):
    run_id = _seed_review(database, finding_count=0)
    with database.sessions() as session:
        run = session.get(ReviewRunRecord, run_id)
        run.repository_policy = {
            "repository": run.repository, "revision": 1, "approver": "assigned-reviewer",
        }
        session.commit()
    repository = SqlAlchemyReviewManagementRepository(database.sessions)
    scope = ResourceScope(repositories=frozenset({"lboverfys/NiuMa"}))
    with pytest.raises(ReviewActionConflictError, match="审批负责人"):
        repository.apply_action(run_id, ReviewAction.APPROVE,
            actor="another-reviewer", request_id="wrong-approver", scope=scope)
    result = repository.apply_action(
        run_id, ReviewAction.APPROVE, actor=TEST_USERNAME, request_id="administrator",
    )
    assert result[0] == run_id
    details = repository.get(run_id)
    assert details.workflow_status is ExecutionStatus.AWAITING_PUBLISH


def test_team_lists_use_one_bounded_query_and_cursor_pages_do_not_repeat(database):
    _, team, _ = _services(database)
    now = datetime.now(UTC)
    with database.sessions() as session, session.begin():
        session.add_all([
            TeamMemberRecord(
                username=f"member-{number:03}", username_key=f"member-{number:03}",
                password_hash="unused-test-hash", role="viewer", enabled=True,
                resource_scope={"installation_ids": [], "organizations": [], "repositories": []},
                revision=1, created_at=now, updated_at=now, updated_by=TEST_USERNAME,
            ) for number in range(23)
        ])
    statements = []
    def capture(_connection, _cursor, statement, _params, _context, _many):
        statements.append(statement)
    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        first = team.members()
        assert len(statements) == 1 and "LIMIT" in statements[0]
        assert "password_hash" not in statements[0]
        statements.clear()
        second = team.members(cursor=first.next_cursor)
        assert len(statements) == 1
        assert len(first.items) == len(second.items) == 10
        assert not {item.username for item in first.items}.intersection(
            item.username for item in second.items
        )
        with pytest.raises(ValueError):
            team.members(cursor="invalid")
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)


def test_repository_management_api_and_policy_validation(database):
    _, _, application = _services(database)
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://testserver",
        ) as client:
            await client.post("/api/v1/auth/login", json={
                "username": TEST_USERNAME, "password": TEST_PASSWORD,
            })
            body = {"repository": "lboverfys/NiuMa", "expected_revision": 0,
                    "policy": {"target_branches": ["main"], "max_model_requests": 5}}
            created = await client.post("/api/v1/team/repositories", json=body)
            assert created.status_code == 201, created.text
            identifier = created.json()["id"]
            assert (await client.post("/api/v1/team/repositories", json=body)).status_code == 409
            listed = await client.get("/api/v1/team/repositories")
            assert listed.status_code == 200 and len(listed.json()["items"]) == 1
            body["expected_revision"] = 1
            body["policy"]["approver"] = "unknown-member"
            assert (await client.put("/api/v1/team/repositories/" + identifier, json=body)).status_code == 422
            body["policy"]["approver"] = TEST_USERNAME
            body["policy"]["knowledge_sources"] = ["missing.md"]
            assert (await client.put("/api/v1/team/repositories/" + identifier, json=body)).status_code == 422
            body["policy"]["knowledge_sources"] = []
            updated = await client.put("/api/v1/team/repositories/" + identifier, json=body)
            assert updated.status_code == 200 and updated.json()["revision"] == 2
            assert (await client.put("/api/v1/team/repositories/" + identifier, json=body)).status_code == 409
            assert (await client.get("/api/v1/team/repositories?limit=101")).status_code == 422
            assert (await client.get("/api/v1/team/audits?cursor=invalid")).status_code == 422
            body["expected_revision"] = 2
            body["policy"]["enabled"] = False
            assert (await client.put("/api/v1/team/repositories/" + identifier, json=body)).status_code == 200
            denied = await client.post("/api/v1/reviews", headers={"Idempotency-Key": "disabled-policy"},
                json=ReviewRequest(installation_id=10, repository_id=42,
                    repository="lboverfys/NiuMa", pull_request_number=128,
                    head_sha="a" * 40).model_dump(mode="json"))
            assert denied.status_code == 409 and "暂停" in denied.json()["detail"]
        webhook = await post_webhook(webhook_application(database), encode(webhook_payload()))
        assert webhook.status_code == 202
        assert webhook.json()["accepted"] is False
        assert webhook.json()["reason"] == "repository_paused"
        with database.sessions() as session:
            assert session.scalar(select(ReviewRunRecord.id).limit(1)) is None
            assert session.scalar(select(OutboxEventRecord.id).where(
                OutboxEventRecord.event_type == "review.requested",
            ).limit(1)) is None
    asyncio.run(exercise())
