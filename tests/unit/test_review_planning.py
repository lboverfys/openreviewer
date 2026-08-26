from hashlib import sha256

import pytest

from domain.enums import (
    ChangedFileStatus,
    PatchState,
    RepositoryRuleIssueKind,
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
        _file("assets/manual.pdf"),
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
        "assets/manual.pdf": ReviewFileDecision.UNSUPPORTED,
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
