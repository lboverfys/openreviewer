import json
from decimal import Decimal
from hashlib import sha256

import pytest

import services.model_review as model_review_service
from domain.enums import (
    FindingCategory,
    LocationSide,
    ModelApiProtocol,
    ModelCallStatus,
    ModelProvider,
    ModelReasoningEffort,
    ModelReviewVerdict,
    ReviewAgent,
    Severity,
    VerificationStatus,
)
from domain.model_review import (
    ModelFindingCandidate,
    ModelFindingLocation,
    ModelReviewInput,
    ModelReviewOutput,
    ModelReviewResult,
    ModelTokenUsage,
    finding_identity_fingerprint,
    materialize_findings,
    model_review_output_schema,
)
from domain.retrieval import ContextEvidence, stable_key
from domain.review_planning import RepositoryRule, ReviewUnit
from services.model_review import (
    FragmentLineMap,
    ModelPricing,
    ModelServiceSettings,
    StructuredReviewPromptBuilder,
    combine_model_review_results,
    plan_model_review_batches,
    remap_model_review_result,
    validate_model_api_endpoint,
)
from services.pinned_http import PublicDnsPinnedNetworkBackend

HEAD_SHA = "a" * 40
BLOB_SHA = "b" * 40
RULE_SHA = "c" * 40


def make_model_input() -> ModelReviewInput:
    rule_content = "# Review\nCheck authorization boundaries.\n"
    rule_bytes = rule_content.encode("utf-8")
    patch = "@@ -1 +1,2 @@\n-old\n+new\n+authorize()\n"
    patch_bytes = patch.encode("utf-8")
    rule = RepositoryRule(
        path="AGENTS.md",
        scope=None,
        blob_sha=RULE_SHA,
        content=rule_content,
        content_sha256=sha256(rule_bytes).hexdigest(),
        byte_size=len(rule_bytes),
    )
    unit = ReviewUnit(
        unit_key="d" * 64,
        review_version_key=f"42:9:{HEAD_SHA}",
        head_sha=HEAD_SHA,
        file="src/auth.py",
        blob_sha=BLOB_SHA,
        language="python",
        patch=patch,
        patch_sha256=sha256(patch_bytes).hexdigest(),
        rule_paths=("AGENTS.md",),
        estimated_input_bytes=len(rule_bytes) + len(patch_bytes),
        planner_version="review-plan-v1",
    )
    return ModelReviewInput(
        review_plan_id="plan-1",
        review_run_id="run-1",
        plan_fingerprint="e" * 64,
        planner_version="review-plan-v1",
        review_version_key=f"42:9:{HEAD_SHA}",
        repository_id=42,
        repository="owner/repository",
        pull_request_number=9,
        head_sha=HEAD_SHA,
        rules=(rule,),
        units=(unit,),
        total_estimated_input_bytes=unit.estimated_input_bytes,
    )


def make_output(*, start_line: int = 2) -> ModelReviewOutput:
    return ModelReviewOutput(
        verdict=ModelReviewVerdict.ISSUES_FOUND,
        summary="发现授权路径可能绕过统一权限检查。",
        checked_areas=("授权边界", "角色校验"),
        findings=(
            ModelFindingCandidate(
                unit_key="d" * 64,
                severity=Severity.HIGH,
                category=FindingCategory.AUTHORIZATION,
                location=ModelFindingLocation(
                    file="src/auth.py",
                    start_line=start_line,
                    end_line=start_line,
                    side=LocationSide.RIGHT,
                    symbol="authorize",
                ),
                title="授权检查可被绕过",
                evidence="新增路径在校验角色前直接继续执行。",
                impact="普通用户可以进入管理员流程。",
                suggestion="在进入流程前执行统一授权检查。",
                required_test="增加普通用户访问被拒绝的回归测试。",
                confidence=0.93,
                rule_reference="AGENTS.md",
            ),
        )
    )


def make_large_v2_input() -> ModelReviewInput:
    source = make_model_input()
    units = []
    for index, file in enumerate(("src/a.py", "src/b.py", "src/c.py"), start=1):
        patch = f"@@ -1 +1 @@\n-old-{index}\n+" + ("x" * 120_000) + "\n"
        encoded = patch.encode("utf-8")
        units.append(
            ReviewUnit(
                unit_key=f"{index}" * 64,
                review_version_key=source.review_version_key,
                head_sha=source.head_sha,
                file=file,
                blob_sha=f"{index + 3}" * 40,
                language="python",
                patch=patch,
                patch_sha256=sha256(encoded).hexdigest(),
                rule_paths=("AGENTS.md",),
                estimated_input_bytes=len(encoded),
                planner_version="review-planner-v2",
            )
        )
    return ModelReviewInput(
        review_plan_id=source.review_plan_id,
        review_run_id=source.review_run_id,
        plan_fingerprint=source.plan_fingerprint,
        planner_version="review-planner-v2",
        review_version_key=source.review_version_key,
        repository_id=source.repository_id,
        repository=source.repository,
        pull_request_number=source.pull_request_number,
        head_sha=source.head_sha,
        rules=source.rules,
        units=tuple(units),
        total_estimated_input_bytes=(
            sum(unit.estimated_input_bytes for unit in units)
            + sum(rule.byte_size for rule in source.rules)
        ),
    )


def make_many_line_input(line_count: int = 10_000) -> ModelReviewInput:
    """生成可稳定切成多片段的单文件统一 diff。"""

    source = make_model_input()
    patch = (
        f"@@ -1,{line_count} +1,{line_count} @@\n"
        + "".join(f"+line-{index}\n" for index in range(1, line_count + 1))
    )
    unit = source.units[0].model_copy(update={"patch": patch})
    unit = unit.model_copy(
        update={
            "patch_sha256": sha256(patch.encode("utf-8")).hexdigest(),
            "estimated_input_bytes": len(patch.encode("utf-8")),
        }
    )
    return source.model_copy(
        update={
            "units": (unit,),
            "total_estimated_input_bytes": unit.estimated_input_bytes
            + sum(rule.byte_size for rule in source.rules),
        }
    )


def make_related_v3_input() -> ModelReviewInput:
    source = make_model_input()
    definitions = (
        ("1" * 64, "a" * 64, "src/audit.py", 35_000),
        ("2" * 64, "b" * 64, "src/user_controller.py", 14_000),
        ("3" * 64, "b" * 64, "src/user_service.py", 14_000),
    )
    units = []
    for index, (unit_key, group_key, file, size) in enumerate(definitions):
        patch = f"@@ -1 +1 @@\n-old-{index}\n+" + ("x" * size) + "\n"
        encoded = patch.encode("utf-8")
        units.append(
            ReviewUnit(
                unit_key=unit_key,
                group_key=group_key,
                review_version_key=source.review_version_key,
                head_sha=source.head_sha,
                file=file,
                blob_sha=f"{index + 4}" * 40,
                language="python",
                patch=patch,
                patch_sha256=sha256(encoded).hexdigest(),
                rule_paths=("AGENTS.md",),
                estimated_input_bytes=len(encoded),
                planner_version="review-planner-v3",
            )
        )
    return source.model_copy(
        update={
            "planner_version": "review-planner-v3",
            "units": tuple(units),
            "total_estimated_input_bytes": (
                sum(unit.estimated_input_bytes for unit in units)
                + sum(rule.byte_size for rule in source.rules)
            ),
        }
    )


def make_result(output: ModelReviewOutput, fingerprint: str) -> ModelReviewResult:
    return ModelReviewResult(
        provider=ModelProvider.OPENAI,
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        model="test-model",
        status=ModelCallStatus.SUCCEEDED,
        prompt_version="test",
        request_fingerprint=fingerprint,
        response_status=200,
        duration_ms=10,
        usage=ModelTokenUsage(input_tokens=10, output_tokens=4),
        output=output,
    )


def candidate_at(unit_key: str, line: int, *, title: str = "跨片段问题") -> ModelFindingCandidate:
    return ModelFindingCandidate(
        unit_key=unit_key,
        severity=Severity.HIGH,
        category=FindingCategory.SECURITY,
        location=ModelFindingLocation(
            file="src/auth.py",
            start_line=line,
            end_line=line,
            side=LocationSide.RIGHT,
            symbol="check",
        ),
        title=title,
        evidence="证据",
        impact="影响",
        suggestion="建议",
        required_test=None,
        confidence=0.8,
        rule_reference="AGENTS.md",
    )


def test_model_schema_excludes_platform_owned_finding_fields() -> None:
    schema = model_review_output_schema()
    item = schema["properties"]["findings"]["items"]

    assert item["additionalProperties"] is False
    assert "fingerprint" not in item["properties"]
    assert "head_sha" not in item["properties"]
    assert "verification_status" not in item["properties"]
    assert set(item["required"]) == set(item["properties"])
    assert set(schema["required"]) == set(schema["properties"])
    assert {"verdict", "summary", "checked_areas", "findings"} == set(
        schema["properties"]
    )


def test_model_output_rejects_a_no_issue_verdict_with_findings() -> None:
    with pytest.raises(ValueError, match="cannot contain findings"):
        ModelReviewOutput(
            verdict=ModelReviewVerdict.NO_ACTIONABLE_ISSUE,
            summary="未发现可报告问题。",
            checked_areas=("授权边界",),
            findings=make_output().findings,
        )


def test_materialization_adds_trusted_identity_and_line_independent_fingerprint() -> None:
    review_input = make_model_input()
    first = materialize_findings(review_input, make_output(start_line=2))[0].finding
    moved = materialize_findings(review_input, make_output(start_line=200))[0].finding
    retitled_output = make_output(start_line=2)
    retitled_output = retitled_output.model_copy(
        update={
            "findings": (
                retitled_output.findings[0].model_copy(update={"title": "不同措辞"}),
            )
        }
    )
    retitled = materialize_findings(review_input, retitled_output)[0].finding

    assert first.fingerprint == moved.fingerprint
    assert first.fingerprint == retitled.fingerprint
    assert first.head_sha == HEAD_SHA
    assert first.verification_status is VerificationStatus.VERIFIED
    assert first.location is not None
    assert first.location.blob_sha == BLOB_SHA
    assert first.location.in_diff is True
    assert moved.verification_status is VerificationStatus.REJECTED
    assert moved.location is not None
    assert moved.location.in_diff is False


def test_identity_hint_keeps_finding_identity_across_file_renames() -> None:
    candidate = candidate_at("a" * 64, 2).model_copy(
        update={"identity_hint": "authorization-missing-scope"}
    )
    renamed = candidate.model_copy(
        update={
            "location": candidate.location.model_copy(
                update={"file": "src/renamed_auth.py"}
            )
        }
    )

    assert finding_identity_fingerprint(candidate, "src/auth.py") == (
        finding_identity_fingerprint(renamed, "src/renamed_auth.py")
    )


def test_identity_fingerprint_does_not_depend_on_evidence_wording() -> None:
    first = candidate_at("a" * 64, 2).model_copy(
        update={
            "identity_hint": "authorization-missing-scope",
            "evidence": "普通用户可以绕过管理员范围校验。",
        }
    )
    second = first.model_copy(
        update={"evidence": "缺少对象级权限检查导致越权访问。"}
    )

    assert finding_identity_fingerprint(first, "src/auth.py") == (
        finding_identity_fingerprint(second, "src/auth.py")
    )


def test_materialization_rejects_unit_and_rule_references_outside_the_plan() -> None:
    review_input = make_model_input()
    unknown_unit = make_output().model_copy(
        update={
            "findings": (
                make_output().findings[0].model_copy(update={"unit_key": "0" * 64}),
            )
        }
    )
    with pytest.raises(ValueError, match="unknown review unit"):
        materialize_findings(review_input, unknown_unit)

    unknown_rule = make_output().model_copy(
        update={
            "findings": (
                make_output().findings[0].model_copy(
                    update={"rule_reference": "docs/unknown.md"}
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="unknown repository rule"):
        materialize_findings(review_input, unknown_rule)


def test_prompt_is_one_bounded_plan_payload_and_marks_repository_text_untrusted() -> None:
    prompt = StructuredReviewPromptBuilder().build(
        make_model_input(),
        ModelProvider.OPENAI,
        "test-model",
        ModelApiProtocol.RESPONSES,
    )
    chat_prompt = StructuredReviewPromptBuilder().build(
        make_model_input(),
        ModelProvider.OPENAI,
        "test-model",
        ModelApiProtocol.CHAT_COMPLETIONS,
    )

    payload = json.loads(prompt.user)
    assert len(payload["review_units"]) == 1
    assert payload["review_units"][0]["unit_key"] == "d" * 64
    assert len(payload["repository_rules"]) == 1
    assert set(payload["output_contract"]["required"]) == {
        "verdict",
        "summary",
        "checked_areas",
        "findings",
    }
    assert "不可信数据" in prompt.system
    assert len(prompt.request_fingerprint) == 64
    assert prompt.request_fingerprint != chat_prompt.request_fingerprint


def test_prompt_carries_the_agent_specific_review_role() -> None:
    review_input = make_model_input().model_copy(
        update={"review_agent": ReviewAgent.SECURITY}
    )
    prompt = StructuredReviewPromptBuilder().build(
        review_input,
        ModelProvider.OPENAI,
        "security-model",
        ModelApiProtocol.RESPONSES,
    )

    assert '"agent":"security"' in prompt.user
    assert "鉴权" in prompt.user
    assert "不要输出思维链" in prompt.system


def test_batch_conclusions_are_merged_without_losing_checked_areas() -> None:
    first = ModelReviewOutput(
        verdict=ModelReviewVerdict.NO_ACTIONABLE_ISSUE,
        summary="检查了鉴权边界。",
        checked_areas=("鉴权",),
        findings=(),
    )
    second = ModelReviewOutput(
        verdict=ModelReviewVerdict.NO_ACTIONABLE_ISSUE,
        summary="检查了输入校验。",
        checked_areas=("输入校验",),
        findings=(),
    )

    combined = combine_model_review_results(
        make_model_input(),
        (
            make_result(first, "8" * 64),
            make_result(second, "9" * 64),
        ),
    )

    assert combined.output.verdict is ModelReviewVerdict.NO_ACTIONABLE_ISSUE
    assert "第1批：检查了鉴权边界。" in (combined.output.summary or "")
    assert combined.output.checked_areas == ("鉴权", "输入校验")


def test_model_batches_use_context_window_without_omitting_files() -> None:
    review_input = make_large_v2_input()
    one_million = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="deepseek-v4-flash",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        context_window_tokens=1_000_000,
        max_output_tokens=16_384,
        max_request_bytes=8 * 1024 * 1024,
    )
    small_context = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="small-context-model",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        context_window_tokens=32_768,
        max_output_tokens=4_096,
        max_request_bytes=1024 * 1024,
    )

    large_batches = plan_model_review_batches(review_input, one_million)
    small_batches = plan_model_review_batches(review_input, small_context)

    assert len(large_batches) >= 3
    assert len(small_batches) > 1
    for batches in (large_batches, small_batches):
        assert {
            file
            for batch in batches
            for file in batch.files
        } == {unit.file for unit in review_input.units}
        fragments_by_unit: dict[str, list[str]] = {}
        for batch in batches:
            for unit in batch.review_input.units:
                fragments_by_unit.setdefault(unit.unit_key, []).append(unit.patch)
        assert {
            unit_key: "".join(fragments)
            for unit_key, fragments in fragments_by_unit.items()
        } == {unit.unit_key: unit.patch for unit in review_input.units}
    assert all(
        batch.estimated_input_tokens <= one_million.batch_input_budget_tokens
        for batch in large_batches
    )
    assert all(
        batch.estimated_input_tokens <= small_context.batch_input_budget_tokens
        for batch in small_batches
    )


def test_model_batches_reserve_associated_context_without_losing_code() -> None:
    source = make_large_v2_input()
    content = "关联代码\n" * 1000
    evidence = ContextEvidence(
        reference_id=stable_key("index", "chunk"), chunk_id="chunk", index_id="index",
        head_sha=HEAD_SHA, file="src/context.py", blob_sha=BLOB_SHA,
        symbol="authorize", start_line=1, end_line=1000, content=content,
        content_hash=sha256(content.encode()).hexdigest(), routes=("bm25",),
        rank=1, fused_rank=1, fusion_score=1, selected=True,
        unit_keys=(source.units[0].unit_key,),
    )
    review_input = source.model_copy(update={"context_evidence": (evidence,)})
    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI, model="context-model", api_key="test-key",
        api_protocol=ModelApiProtocol.RESPONSES, context_window_tokens=128000,
        max_output_tokens=4096, max_batch_input_tokens=64000,
    )
    batches = plan_model_review_batches(review_input, settings)
    patches: dict[str, list[str]] = {}
    for batch in batches:
        assert batch.estimated_input_tokens <= settings.batch_input_budget_tokens
        expected = any(unit.unit_key in evidence.unit_keys for unit in batch.review_input.units)
        assert bool(batch.review_input.context_evidence) is expected
        for unit in batch.review_input.units:
            patches.setdefault(unit.unit_key, []).append(unit.patch)
    assert {key: "".join(parts) for key, parts in patches.items()} == {
        unit.unit_key: unit.patch for unit in source.units
    }


def test_model_batch_planning_caches_rule_and_piece_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_input = make_large_v2_input()
    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="cache-check-model",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        context_window_tokens=32_768,
        max_output_tokens=4_096,
        max_request_bytes=1024 * 1024,
    )
    original_rule_size = model_review_service._rule_prompt_bytes
    original_unit_size = model_review_service._unit_prompt_bytes
    rule_calls: list[str] = []
    unit_calls: list[tuple[str, int]] = []

    def counted_rule_size(rule: RepositoryRule) -> int:
        rule_calls.append(rule.path)
        return original_rule_size(rule)

    def counted_unit_size(unit: ReviewUnit, patch: str) -> int:
        unit_calls.append((unit.unit_key, len(patch)))
        return original_unit_size(unit, patch)

    monkeypatch.setattr(model_review_service, "_rule_prompt_bytes", counted_rule_size)
    monkeypatch.setattr(model_review_service, "_unit_prompt_bytes", counted_unit_size)

    batches = plan_model_review_batches(review_input, settings)

    assert rule_calls == [rule.path for rule in review_input.rules]
    assert len(unit_calls) == len(review_input.units) + sum(
        len(batch.review_input.units) for batch in batches
    )


def test_related_files_stay_in_one_batch_when_the_group_fits() -> None:
    review_input = make_related_v3_input()
    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="related-file-model",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        context_window_tokens=32_768,
        max_output_tokens=4_096,
        max_batch_input_tokens=24_000,
        max_request_bytes=1024 * 1024,
    )

    batches = plan_model_review_batches(review_input, settings)
    related_keys = {"2" * 64, "3" * 64}
    containing_batches = [
        batch
        for batch in batches
        if related_keys
        & {unit.unit_key for unit in batch.review_input.units}
    ]

    assert len(batches) == 2
    assert len(containing_batches) == 1
    assert related_keys <= {
        unit.unit_key for unit in containing_batches[0].review_input.units
    }


def test_model_settings_default_to_gateway_safe_batching_and_optional_reasoning() -> None:
    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="one-million-context-model",
        api_key="relay-key",
        context_window_tokens=1_000_000,
        max_output_tokens=16_384,
    )

    assert settings.reasoning_effort is ModelReasoningEffort.NONE
    assert settings.max_batch_input_tokens == 64_000
    assert settings.batch_input_budget_tokens == 64_000


def test_model_batches_split_one_large_file_without_losing_unit_identity() -> None:
    source = make_large_v2_input()
    large_unit = source.units[0].model_copy(
        update={
            "patch": "@@ -1 +1 @@\n-old\n+" + ("变" * 180_000) + "\n",
        }
    )
    large_unit = large_unit.model_copy(
        update={
            "patch_sha256": sha256(large_unit.patch.encode("utf-8")).hexdigest(),
            "estimated_input_bytes": len(large_unit.patch.encode("utf-8")),
        }
    )
    review_input = source.model_copy(
        update={
            "units": (large_unit,),
            "total_estimated_input_bytes": (
                large_unit.estimated_input_bytes
                + sum(rule.byte_size for rule in source.rules)
            ),
        }
    )
    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="small-context-model",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        context_window_tokens=32_768,
        max_output_tokens=4_096,
        max_request_bytes=1024 * 1024,
    )

    batches = plan_model_review_batches(review_input, settings)

    assert len(batches) > 1
    fragments = [batch.review_input.units[0] for batch in batches]
    assert all(batch.fragmented for batch in batches)
    assert all(fragment.unit_key == large_unit.unit_key for fragment in fragments)
    assert "".join(fragment.patch for fragment in fragments) == large_unit.patch


def test_model_batches_reject_count_above_persistence_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_input = make_large_v2_input()
    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="small-context-model",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        context_window_tokens=32_768,
        max_output_tokens=4_096,
        max_request_bytes=1024 * 1024,
    )
    monkeypatch.setattr(model_review_service, "MAX_MODEL_REVIEW_BATCHES", 1)

    with pytest.raises(ValueError, match="batch count exceeds"):
        plan_model_review_batches(review_input, settings)


def test_fragment_line_mapping_handles_adjacent_boundaries_and_both_sides() -> None:
    review_input = make_many_line_input()
    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="small-context-model",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        context_window_tokens=32_768,
        max_output_tokens=4_096,
        max_request_bytes=1024 * 1024,
    )
    batches = plan_model_review_batches(review_input, settings)

    assert len(batches) > 1
    first = batches[0]
    second = batches[1]
    first_map = first.line_maps[0]
    second_map = second.line_maps[0]
    assert first_map.line_mode == "global"
    assert second_map.line_mode == "local"
    assert first_map.local_to_right[-1] is not None
    # 片段可能恰好把一条超长行从中间切开；此时边界两端映射到同一
    # 原始行，下一条完整行仍必须保持连续。
    assert second_map.local_to_right[1] == first_map.local_to_right[-1] + 1

    candidate = candidate_at(
        review_input.units[0].unit_key,
        1,
        title="第二片段首行",
    )
    remapped = remap_model_review_result(
        make_result(ModelReviewOutput(findings=(candidate,)), "1" * 64),
        second,
    )
    location = remapped.output.findings[0].location
    assert location is not None
    assert location.start_line == second_map.local_to_right[0]
    assert location.end_line == second_map.local_to_right[0]

    # 同一片段内的相邻范围必须分别还原，不可只映射首行。
    range_candidate = candidate_at(
        review_input.units[0].unit_key,
        1,
        title="片段相邻范围",
    ).model_copy(
        update={
            "location": ModelFindingLocation(
                file="src/auth.py",
                start_line=1,
                end_line=2,
                side=LocationSide.RIGHT,
                symbol="check",
            )
        }
    )
    range_result = remap_model_review_result(
        make_result(ModelReviewOutput(findings=(range_candidate,)), "2" * 64),
        second,
    )
    range_location = range_result.output.findings[0].location
    assert range_location is not None
    assert range_location.start_line == second_map.local_to_right[0]
    assert range_location.end_line == second_map.local_to_right[1]

    # 删除行只允许使用 left，新增行只允许使用 right。
    source_map = plan_model_review_batches(make_model_input(), settings)[0].line_maps[0]
    assert source_map.map_line(1, LocationSide.LEFT) == 1
    assert source_map.map_line(1, LocationSide.RIGHT) == 1
    with pytest.raises(ValueError):
        source_map.map_line(3, LocationSide.LEFT)


def test_fragment_mapping_rejects_zero_and_unprovable_lines() -> None:
    mapping = FragmentLineMap(
        unit_key="d" * 64,
        file="src/auth.py",
        fragment_index=0,
        fragment_count=2,
        line_mode="local",
        local_to_left=(None,),
        local_to_right=(7,),
    )
    with pytest.raises(ValueError):
        mapping.map_line(0, LocationSide.RIGHT)
    with pytest.raises(ValueError):
        mapping.map_line(1, LocationSide.LEFT)
    assert mapping.map_line(1, LocationSide.RIGHT) == 7


def test_batch_mapping_rejects_findings_for_units_not_sent_in_that_batch() -> None:
    batch = plan_model_review_batches(
        make_model_input(),
        ModelServiceSettings(
            provider=ModelProvider.OPENAI,
            model="test-model",
            api_key="relay-key",
        ),
    )[0]
    candidate = candidate_at("f" * 64, 1).model_copy(update={"location": None})

    with pytest.raises(ValueError, match="outside its batch"):
        remap_model_review_result(
            make_result(ModelReviewOutput(findings=(candidate,)), "6" * 64),
            batch,
        )


def test_fragment_mapping_prefers_declared_line_mode_and_rejects_unknown_lines() -> None:
    mapping = FragmentLineMap(
        unit_key="d" * 64,
        file="src/auth.py",
        fragment_index=0,
        fragment_count=2,
        line_mode="global",
        local_to_left=(100, 101),
        local_to_right=(100, 101),
    )
    # 行号 1 在局部范围内但不在全局映射中，可安全按局部兼容。
    assert mapping.map_line(1, LocationSide.RIGHT) == 100
    with pytest.raises(ValueError):
        mapping.map_line(102, LocationSide.RIGHT)


def test_line_mapping_keeps_multiple_findings_on_original_files_for_zero_count_hunks() -> None:
    source = make_model_input()
    new_patch = "@@ -0,0 +1,2 @@\n+first\n+second\n"
    removed_patch = "@@ -1,2 +0,0 @@\n-first\n-second\n"
    new_unit = source.units[0].model_copy(
        update={
            "unit_key": "e" * 64,
            "file": "src/new.py",
            "patch": new_patch,
            "patch_sha256": sha256(new_patch.encode("utf-8")).hexdigest(),
            "estimated_input_bytes": len(new_patch.encode("utf-8")),
        }
    )
    removed_unit = source.units[0].model_copy(
        update={
            "unit_key": "f" * 64,
            "file": "src/removed.py",
            "patch": removed_patch,
            "patch_sha256": sha256(removed_patch.encode("utf-8")).hexdigest(),
            "estimated_input_bytes": len(removed_patch.encode("utf-8")),
        }
    )
    review_input = source.model_copy(
        update={
            "units": (new_unit, removed_unit),
            "total_estimated_input_bytes": (
                new_unit.estimated_input_bytes + removed_unit.estimated_input_bytes
            ),
        }
    )
    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="test-model",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
    )
    batch = plan_model_review_batches(review_input, settings)[0]

    def candidate(
        unit: ReviewUnit,
        *,
        side: LocationSide,
        start_line: int,
        end_line: int,
        title: str,
    ) -> ModelFindingCandidate:
        return ModelFindingCandidate(
            unit_key=unit.unit_key,
            severity=Severity.HIGH,
            category=FindingCategory.SECURITY,
            location=ModelFindingLocation(
                file=unit.file,
                start_line=start_line,
                end_line=end_line,
                side=side,
                symbol=None,
            ),
            title=title,
            evidence="证据",
            impact="影响",
            suggestion="建议",
            required_test=None,
            confidence=0.8,
            rule_reference="AGENTS.md",
        )

    result = remap_model_review_result(
        make_result(
            ModelReviewOutput(
                findings=(
                    candidate(
                        new_unit,
                        side=LocationSide.RIGHT,
                        start_line=1,
                        end_line=2,
                        title="新增文件范围",
                    ),
                    candidate(
                        new_unit,
                        side=LocationSide.RIGHT,
                        start_line=2,
                        end_line=2,
                        title="新增文件第二处",
                    ),
                    candidate(
                        removed_unit,
                        side=LocationSide.LEFT,
                        start_line=1,
                        end_line=2,
                        title="删除文件范围",
                    ),
                )
            ),
            "5" * 64,
        ),
        batch,
    )

    locations = [item.location for item in result.output.findings]
    assert [(item.file, item.start_line, item.end_line, item.side) for item in locations if item] == [
        ("src/new.py", 1, 2, LocationSide.RIGHT),
        ("src/new.py", 2, 2, LocationSide.RIGHT),
        ("src/removed.py", 1, 2, LocationSide.LEFT),
    ]


def test_large_utf8_single_line_is_preserved_and_maps_every_fragment() -> None:
    source = make_model_input()
    patch = "@@ -1 +1 @@\n-old\n+" + ("审" * 180_000) + "\n"
    unit = source.units[0].model_copy(update={"patch": patch})
    unit = unit.model_copy(
        update={
            "patch_sha256": sha256(patch.encode("utf-8")).hexdigest(),
            "estimated_input_bytes": len(patch.encode("utf-8")),
        }
    )
    review_input = source.model_copy(
        update={
            "units": (unit,),
            "total_estimated_input_bytes": unit.estimated_input_bytes
            + sum(rule.byte_size for rule in source.rules),
        }
    )
    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="small-context-model",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        context_window_tokens=32_768,
        max_output_tokens=4_096,
        max_request_bytes=1024 * 1024,
    )
    batches = plan_model_review_batches(review_input, settings)
    assert len(batches) > 1
    assert "".join(batch.review_input.units[0].patch for batch in batches) == patch
    for batch in batches:
        result = remap_model_review_result(
            make_result(
                ModelReviewOutput(
                    findings=(candidate_at(review_input.units[0].unit_key, 1, title=f"片段-{batch.number}"),)
                ),
                f"{batch.number:064x}",
            ),
            batch,
        )
        location = result.output.findings[0].location
        assert location is not None
        assert location.start_line == 1


def test_cross_fragment_results_are_deduplicated_and_sorted_after_remapping() -> None:
    review_input = make_many_line_input()
    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="small-context-model",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        context_window_tokens=32_768,
        max_output_tokens=4_096,
        max_request_bytes=1024 * 1024,
    )
    batches = plan_model_review_batches(review_input, settings)
    assert len(batches) >= 2
    unit_key = review_input.units[0].unit_key
    duplicate_first = candidate_at(unit_key, 2, title="同一个问题")
    duplicate_second = candidate_at(unit_key, 1, title="同一个问题").model_copy(
        update={"confidence": 0.95}
    )
    unique = candidate_at(unit_key, 3, title="另一个问题").model_copy(
        update={
            "severity": Severity.CRITICAL,
            "confidence": 0.7,
            "evidence": "另一段独立证据",
        }
    )
    results = (
        make_result(ModelReviewOutput(findings=(duplicate_first, unique)), "3" * 64),
        make_result(ModelReviewOutput(findings=(duplicate_second,)), "4" * 64),
    )
    combined = combine_model_review_results(
        review_input,
        results,
        batches=batches[:2],
    )
    assert len(combined.output.findings) == 2
    assert combined.output.findings[0].title == "另一个问题"
    assert combined.output.findings[1].title == "同一个问题"
    assert combined.output.findings[1].confidence == 0.95


def test_pricing_uses_decimal_microusd_and_requires_cache_rates_when_used() -> None:
    usage = ModelTokenUsage(
        input_tokens=100,
        output_tokens=40,
        cache_read_input_tokens=20,
        reasoning_output_tokens=10,
    )
    pricing = ModelPricing(
        input_usd_per_million=Decimal("2"),
        output_usd_per_million=Decimal("10"),
        cache_read_usd_per_million=Decimal("0.5"),
    )
    incomplete = ModelPricing(
        input_usd_per_million=Decimal("2"),
        output_usd_per_million=Decimal("10"),
    )

    assert pricing.estimate_microusd(usage) == 610
    assert incomplete.estimate_microusd(usage) is None


def test_model_settings_support_secret_file_and_reject_dual_secret_sources(
    tmp_path,
) -> None:
    key_file = tmp_path / "model-api-key"
    key_file.write_text("test-only-model-key\n", encoding="utf-8")
    settings = ModelServiceSettings.from_environment(
        {
            "OPENREVIEWER_MODEL_PROVIDER": "anthropic",
            "OPENREVIEWER_MODEL_NAME": "test-model",
            "OPENREVIEWER_MODEL_API_KEY_FILE": str(key_file),
            "OPENREVIEWER_MODEL_INPUT_USD_PER_MILLION": "3",
            "OPENREVIEWER_MODEL_OUTPUT_USD_PER_MILLION": "15",
        }
    )

    assert settings.provider is ModelProvider.ANTHROPIC
    assert settings.resolved_api_protocol is ModelApiProtocol.MESSAGES
    assert settings.api_key == "test-only-model-key"
    assert settings.max_response_bytes == 16 * 1024 * 1024
    assert "test-only-model-key" not in repr(settings)
    with pytest.raises(ValueError, match="only one"):
        ModelServiceSettings.from_environment(
            {
                "OPENREVIEWER_MODEL_PROVIDER": "openai",
                "OPENREVIEWER_MODEL_NAME": "test-model",
                "OPENREVIEWER_MODEL_API_KEY": "direct-key",
                "OPENREVIEWER_MODEL_API_KEY_FILE": str(key_file),
            }
        )


def test_model_settings_reject_provider_protocol_mismatch() -> None:
    with pytest.raises(ValueError, match="OpenAI API protocol"):
        ModelServiceSettings(
            provider=ModelProvider.OPENAI,
            model="test-model",
            api_key="test-key",
            api_protocol=ModelApiProtocol.MESSAGES,
        )
    with pytest.raises(ValueError, match="Anthropic API protocol"):
        ModelServiceSettings(
            provider=ModelProvider.ANTHROPIC,
            model="test-model",
            api_key="test-key",
            api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        )


def test_model_settings_accept_relay_prefix_and_reject_unsafe_base_urls() -> None:
    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="relay-model",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        api_base_url=" HTTPS://relay.example/api/v1/ ",
    )

    assert settings.resolved_api_base_url == "https://relay.example/api/v1"
    assert settings.api_request_path("/v1/chat/completions") == "chat/completions"
    assert settings.api_request_url("/v1/chat/completions") == (
        "https://relay.example/api/v1/chat/completions"
    )

    nested_settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="relay-model",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        api_base_url="https://relay.example/v1/account/gateway/openai",
    )
    assert nested_settings.api_request_path("/v1/chat/completions") == "v1/chat/completions"
    assert nested_settings.api_request_url("/v1/chat/completions") == (
        "https://relay.example/v1/account/gateway/openai/v1/chat/completions"
    )

    root_settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="relay-model",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.RESPONSES,
        api_base_url="https://relay.example/gateway",
    )
    assert root_settings.api_request_path("/v1/responses") == "v1/responses"
    assert root_settings.api_request_url("/v1/responses") == (
        "https://relay.example/gateway/v1/responses"
    )

    for unsafe_url in (
        "http://relay.example/v1",
        "https://user:password@relay.example/v1",
        "https://relay.example/v1?key=secret",
        "https://relay.example/v1#fragment",
        "https://relay.example:invalid/v1",
        "https://127.0.0.1/v1",
        "https://[::1]/v1",
        "https://169.254.169.254/latest/meta-data",
        "https://service.internal/v1",
        "https://localhost/v1",
    ):
        with pytest.raises(ValueError, match="API base URL"):
            ModelServiceSettings(
                provider=ModelProvider.OPENAI,
                model="relay-model",
                api_key="relay-key",
                api_base_url=unsafe_url,
            )


def test_model_endpoint_dns_validation_rejects_mixed_or_private_answers() -> None:
    def public_resolver(*_args, **_kwargs):
        return [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (10, 1, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0)),
        ]

    assert validate_model_api_endpoint(
        "https://relay.example/v1",
        resolver=public_resolver,
    ) == ("2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34")

    def mixed_resolver(*_args, **_kwargs):
        return [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("10.0.0.8", 443)),
        ]

    with pytest.raises(ValueError, match="non-public"):
        validate_model_api_endpoint(
            "https://relay.example/v1",
            resolver=mixed_resolver,
        )


def test_model_tcp_backend_connects_only_to_the_validated_ip_literal() -> None:
    connected_hosts: list[str] = []

    class Delegate:
        def connect_tcp(self, host, _port, **_kwargs):
            connected_hosts.append(host)
            return "stream"

    backend = PublicDnsPinnedNetworkBackend(
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ]
    )
    backend._delegate = Delegate()  # type: ignore[assignment]

    assert backend.connect_tcp("relay.example", 443) == "stream"
    assert connected_hosts == ["93.184.216.34"]


def test_model_tcp_backend_fails_closed_on_a_mixed_dns_answer() -> None:
    connected_hosts: list[str] = []

    class Delegate:
        def connect_tcp(self, host, _port, **_kwargs):
            connected_hosts.append(host)
            return "stream"

    backend = PublicDnsPinnedNetworkBackend(
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("10.0.0.8", 443)),
        ]
    )
    backend._delegate = Delegate()  # type: ignore[assignment]

    with pytest.raises(Exception, match="non-public"):
        backend.connect_tcp("relay.example", 443)
    assert connected_hosts == []
