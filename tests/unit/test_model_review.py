from decimal import Decimal
from hashlib import sha256

import pytest

from domain.enums import (
    FindingCategory,
    LocationSide,
    ModelApiProtocol,
    ModelProvider,
    Severity,
    VerificationStatus,
)
from domain.model_review import (
    ModelFindingCandidate,
    ModelFindingLocation,
    ModelReviewInput,
    ModelReviewOutput,
    ModelTokenUsage,
    materialize_findings,
    model_review_output_schema,
)
from domain.review_planning import RepositoryRule, ReviewUnit
from services.model_review import (
    ModelPricing,
    ModelServiceSettings,
    StructuredReviewPromptBuilder,
    plan_model_review_batches,
)


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


def test_model_schema_excludes_platform_owned_finding_fields() -> None:
    schema = model_review_output_schema()
    item = schema["properties"]["findings"]["items"]

    assert item["additionalProperties"] is False
    assert "fingerprint" not in item["properties"]
    assert "head_sha" not in item["properties"]
    assert "verification_status" not in item["properties"]
    assert set(item["required"]) == set(item["properties"])


def test_materialization_adds_trusted_identity_and_line_independent_fingerprint() -> None:
    review_input = make_model_input()
    first = materialize_findings(review_input, make_output(start_line=2))[0].finding
    moved = materialize_findings(review_input, make_output(start_line=200))[0].finding

    assert first.fingerprint == moved.fingerprint
    assert first.head_sha == HEAD_SHA
    assert first.verification_status is VerificationStatus.UNVERIFIED
    assert first.location is not None
    assert first.location.blob_sha == BLOB_SHA
    assert first.location.in_diff is False


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

    assert '"review_units"' in prompt.user
    assert prompt.user.count('"unit_key"') == 1
    assert '"repository_rules"' in prompt.user
    assert "不可信数据" in prompt.system
    assert len(prompt.request_fingerprint) == 64
    assert prompt.request_fingerprint != chat_prompt.request_fingerprint


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

    assert len(large_batches) == 1
    assert len(small_batches) > 1
    assert {
        file
        for batch in small_batches
        for file in batch.files
    } == {unit.file for unit in review_input.units}
    assert all(
        batch.estimated_input_tokens <= small_context.input_budget_tokens
        for batch in small_batches
    )


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

    nested_settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="relay-model",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        api_base_url="https://relay.example/v1/account/gateway/openai",
    )
    assert nested_settings.api_request_path("/v1/chat/completions") == "v1/chat/completions"

    root_settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI,
        model="relay-model",
        api_key="relay-key",
        api_protocol=ModelApiProtocol.RESPONSES,
        api_base_url="https://relay.example/gateway",
    )
    assert root_settings.api_request_path("/v1/responses") == "v1/responses"

    for unsafe_url in (
        "http://relay.example/v1",
        "https://user:password@relay.example/v1",
        "https://relay.example/v1?key=secret",
        "https://relay.example/v1#fragment",
        "https://relay.example:invalid/v1",
    ):
        with pytest.raises(ValueError, match="API base URL"):
            ModelServiceSettings(
                provider=ModelProvider.OPENAI,
                model="relay-model",
                api_key="relay-key",
                api_base_url=unsafe_url,
            )
