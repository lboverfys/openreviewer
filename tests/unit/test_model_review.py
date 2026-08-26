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
