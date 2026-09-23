from hashlib import sha256

import pytest

from domain.enums import (
    ChangedFileStatus,
    PatchState,
    RepositoryRuleIssueKind,
    ReviewAgent,
    ReviewFileDecision,
)
from domain.github import PullRequestFile
from domain.review_planning import (
    RepositoryRule,
    RepositoryRuleIssue,
    RepositoryRulesSnapshot,
)
from services.review_planning import (
    DeterministicReviewPlanner,
    ReviewPlanningSettings,
)
from services.task_queue import ReviewTarget

HEAD_SHA = "c" * 40


def _target(head_sha: str = HEAD_SHA) -> ReviewTarget:
    return ReviewTarget(
        installation_id=156153422,
        repository_id=42,
        repository="lboverfys/NiuMa",
        pull_request_number=48,
        head_sha=head_sha,
        review_version_key=f"42:48:{head_sha}",
        context_fetched_at=None,
    )


def _file(
    path: str,
    *,
    patch_state: PatchState = PatchState.AVAILABLE,
    patch: str = "@@ -1 +1 @@\n-old\n+new\n",
) -> PullRequestFile:
    return PullRequestFile(
        path=path,
        status=ChangedFileStatus.MODIFIED,
        blob_sha="d" * 40,
        additions=1,
        deletions=1,
        changes=2,
        patch_state=patch_state,
        patch=patch if patch_state is PatchState.AVAILABLE else None,
    )


def _rule(path: str, content: str, blob_sha: str) -> RepositoryRule:
    encoded = content.encode("utf-8")
    return RepositoryRule(
        path=path,
        scope=path.rsplit("/", 1)[0] if "/" in path else None,
        blob_sha=blob_sha,
        content=content,
        content_sha256=sha256(encoded).hexdigest(),
        byte_size=len(encoded),
    )


def _snapshot(
    *,
    head_sha: str = HEAD_SHA,
    rules: tuple[RepositoryRule, ...] = (),
    incomplete_files: tuple[str, ...] = (),
    issues: tuple[RepositoryRuleIssue, ...] = (),
) -> RepositoryRulesSnapshot:
    return RepositoryRulesSnapshot(
        repository_id=42,
        repository="lboverfys/NiuMa",
        head_sha=head_sha,
        rules=rules,
        incomplete_files=incomplete_files,
        issues=issues,
        candidate_count=max(len(rules), len(issues)),
        requested_candidate_count=max(len(rules), len(issues)),
    )


def test_planner_assigns_every_file_once_and_orders_applicable_rules() -> None:
    root_rule = _rule("AGENTS.md", "根规则\n", "a" * 40)
    source_rule = _rule("src/AGENTS.md", "源码规则\n", "b" * 40)
    rules = _snapshot(
        rules=(root_rule, source_rule),
        incomplete_files=("locked/secret.py",),
        issues=(
            RepositoryRuleIssue(
                kind=RepositoryRuleIssueKind.CONTENT_UNAVAILABLE,
                rule_path="locked/AGENTS.md",
                affected_file_count=1,
            ),
        ),
    )
    files = (
        _file("src/missing.ts", patch_state=PatchState.MISSING),
        _file("dist/bundle.js"),
        _file("assets/logo.png", patch_state=PatchState.BINARY),
        _file("src/app.py"),
        _file("assets/manual.pdf", patch_state=PatchState.BINARY),
        _file("locked/secret.py"),
        _file("src/huge.ts", patch_state=PatchState.TOO_LARGE),
    )

    planner = DeterministicReviewPlanner()
    plan = planner.plan(_target(), files, rules)
    repeated = planner.plan(_target(), tuple(reversed(files)), rules)

    assert plan == repeated
    assert plan.plan_fingerprint == repeated.plan_fingerprint
    assert {item.file: item.decision for item in plan.files} == {
        "assets/logo.png": ReviewFileDecision.BINARY,
        "assets/manual.pdf": ReviewFileDecision.BINARY,
        "dist/bundle.js": ReviewFileDecision.GENERATED,
        "locked/secret.py": ReviewFileDecision.RULES_INCOMPLETE,
        "src/app.py": ReviewFileDecision.PLANNED,
        "src/huge.ts": ReviewFileDecision.PATCH_TOO_LARGE,
        "src/missing.ts": ReviewFileDecision.PATCH_MISSING,
    }
    assert len(plan.files) == len(files)
    assert len(plan.units) == 1
    assert plan.units[0].file == "src/app.py"
    assert plan.units[0].language == "python"
    assert plan.units[0].rule_paths == ("AGENTS.md", "src/AGENTS.md")
    assert plan.total_estimated_input_bytes == (
        len(plan.units[0].patch.encode("utf-8"))
        + root_rule.byte_size
        + source_rule.byte_size
    )


@pytest.mark.parametrize("suffix", ["csv", "tsv"])
def test_contract_tables_are_included_in_review(suffix):
    source = _file(f"tests/contracts/endpoints.{suffix}", patch="@@ -1 +1 @@\n-GET /old\n+GET /new\n")
    plan = DeterministicReviewPlanner().plan(_target(), (source,), _snapshot())
    assert plan.files[0].decision is ReviewFileDecision.PLANNED
    assert plan.units[0].language == suffix
    assert plan.units[0].patch == source.patch


@pytest.mark.parametrize("path,language", [
    ("deploy/nginx/container.conf.template", "configuration"),
    ("config/application.yaml.template", "yaml"),
    ("src/example.unregistered", "text"),
    (".env.example", "text"),
    ("LICENSE", "text"),
    ("notes.TXT", "text"),
])
def test_available_text_never_requires_a_language_allowlist(path, language):
    source = _file(path)
    plan = DeterministicReviewPlanner().plan(_target(), (source,), _snapshot())
    assert plan.files[0].decision is ReviewFileDecision.PLANNED
    assert plan.units[0].language == language
    assert plan.units[0].patch == source.patch


@pytest.mark.parametrize("state,decision", [
    (PatchState.MISSING, ReviewFileDecision.PATCH_MISSING),
    (PatchState.TOO_LARGE, ReviewFileDecision.PATCH_TOO_LARGE),
    (PatchState.BINARY, ReviewFileDecision.BINARY),
])
def test_unknown_types_keep_unavailable_content_distinct(state, decision):
    plan = DeterministicReviewPlanner().plan(_target(), (_file("source.unknown", patch_state=state),), _snapshot())
    assert not plan.units
    assert plan.files[0].decision is decision


@pytest.mark.parametrize("path", [".gitignore", "backend/.gitignore"])
def test_gitignore_is_reviewed_as_configuration_by_all_agents(path: str) -> None:
    source = _file(path, patch="@@ -1 +1 @@\n-*.log\n+.env\n")
    planner = DeterministicReviewPlanner()
    plan = planner.plan(_target(), (source,), _snapshot())
    assert plan == planner.plan(_target(), (source,), _snapshot())
    assert plan.files[0].decision is ReviewFileDecision.PLANNED
    assert plan.units[0].language == "configuration"
    assert plan.units[0].patch == source.patch
    assert set(plan.units[0].review_domains) == {
        ReviewAgent.SECURITY, ReviewAgent.CONVENTION, ReviewAgent.LOGIC,
    }


@pytest.mark.parametrize("state, decision", [
    (PatchState.BINARY, ReviewFileDecision.BINARY),
    (PatchState.MISSING, ReviewFileDecision.PATCH_MISSING),
    (PatchState.TOO_LARGE, ReviewFileDecision.PATCH_TOO_LARGE),
])
def test_gitignore_does_not_bypass_patch_validation(state, decision) -> None:
    plan = DeterministicReviewPlanner().plan(
        _target(), (_file(".gitignore", patch_state=state),), _snapshot(),
    )
    assert plan.files[0].decision is decision
    assert plan.units == ()


def test_planner_keeps_all_reviewable_files_and_defers_batching_to_model_stage() -> None:
    files = (
        _file("b.py", patch="b" * 3000),
        _file("c.py", patch="c" * 5000),
        _file("a.py", patch="a" * 3000),
    )
    planner = DeterministicReviewPlanner(
        ReviewPlanningSettings(
            max_units=10,
            max_unit_input_bytes=4096,
            max_total_input_bytes=4096,
        )
    )

    plan = planner.plan(_target(), files, _snapshot())

    assert [unit.file for unit in plan.units] == ["a.py", "b.py", "c.py"]
    assert {item.file: item.decision for item in plan.files} == {
        "a.py": ReviewFileDecision.PLANNED,
        "b.py": ReviewFileDecision.PLANNED,
        "c.py": ReviewFileDecision.PLANNED,
    }
    assert plan.total_estimated_input_bytes == 11_000


def test_planner_groups_related_layers_and_tests_deterministically() -> None:
    files = (
        _file("src/controllers/UserController.py"),
        _file("src/dto/user_dto.py"),
        _file("src/services/user.service.py"),
        _file("tests/test_user_service.py"),
        _file("src/services/billing_service.py"),
    )

    planner = DeterministicReviewPlanner()
    plan = planner.plan(_target(), files, _snapshot())
    repeated = planner.plan(_target(), tuple(reversed(files)), _snapshot())

    assert plan == repeated
    groups = {unit.file: unit.group_key for unit in plan.units}
    user_group = groups["src/controllers/UserController.py"]
    assert user_group is not None
    assert groups["src/dto/user_dto.py"] == user_group
    assert groups["src/services/user.service.py"] == user_group
    assert groups["tests/test_user_service.py"] == user_group
    assert groups["src/services/billing_service.py"] != user_group
    user_positions = [
        index
        for index, unit in enumerate(plan.units)
        if unit.group_key == user_group
    ]
    assert user_positions == list(range(min(user_positions), max(user_positions) + 1))


def test_planner_changes_identity_for_a_new_head_sha() -> None:
    file = _file("src/app.py")
    first = DeterministicReviewPlanner().plan(_target(), (file,), _snapshot())
    next_sha = "e" * 40
    second = DeterministicReviewPlanner().plan(
        _target(next_sha),
        (file,),
        _snapshot(head_sha=next_sha),
    )

    assert first.plan_fingerprint != second.plan_fingerprint
    assert first.units[0].unit_key != second.units[0].unit_key


def test_planner_rejects_rules_from_another_head_sha() -> None:
    with pytest.raises(ValueError):
        DeterministicReviewPlanner().plan(
            _target(),
            (_file("src/app.py"),),
            _snapshot(head_sha="e" * 40),
        )


def test_planner_independently_rejects_unbounded_scope_depth() -> None:
    planner = DeterministicReviewPlanner(
        ReviewPlanningSettings(max_scope_depth=1)
    )

    plan = planner.plan(
        _target(),
        (_file("a/b/c/app.py"),),
        _snapshot(),
    )

    assert plan.units == ()
    assert plan.files[0].decision is ReviewFileDecision.RULES_INCOMPLETE


def test_planner_marks_review_domains_deterministically() -> None:
    files = (
        _file("docs/README.md", patch="# usage\n"),
        _file("src/orders.py", patch="@@ -1 +1 @@\n-old\n+new\n"),
        _file(
            "src/auth.py",
            patch="@@ -1 +1 @@\n-password = input()\n+token = password\n",
        ),
    )

    plan = DeterministicReviewPlanner().plan(_target(), files, _snapshot())
    domains = {unit.file: unit.review_domains for unit in plan.units}

    assert domains["docs/README.md"] == (ReviewAgent.CONVENTION,)
    assert domains["src/orders.py"] == (
        ReviewAgent.SECURITY,
        ReviewAgent.CONVENTION,
        ReviewAgent.LOGIC,
    )
    assert domains["src/auth.py"] == (
        ReviewAgent.SECURITY,
        ReviewAgent.CONVENTION,
        ReviewAgent.LOGIC,
    )
