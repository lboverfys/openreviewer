"""让录制输出夹具经过生产审查边界的确定性离线执行器。"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

import httpx

from domain.enums import (
    FindingCategory,
    LocationSide,
    ModelApiProtocol,
    ModelProvider,
    Severity,
    VerificationStatus,
)
from domain.model_review import ModelReviewInput, materialize_findings
from domain.review_planning import RepositoryRule, ReviewUnit
from services.model_providers import create_model_reviewer
from services.model_review import (
    ModelServiceSettings,
    combine_model_review_results,
    plan_model_review_batches,
)


@dataclass(frozen=True, slots=True)
class GoldenFlowCase:
    case_id: str
    file: str
    patch: str
    predicted_finding_keys: frozenset[str]


def run_golden_review_flow(
    cases: tuple[GoldenFlowCase, ...],
) -> dict[str, frozenset[str]]:
    """回放录制输出，验证 Prompt、协议解析、聚合和定位契约未回归。

    该函数使用 ``MockTransport``，不会调用真实模型，因此结果不能解释为模型
    准确率；它只衡量固定输出夹具经过当前生产流水线后的兼容性。
    """

    predictions: dict[str, frozenset[str]] = {}
    for index, case in enumerate(cases, start=1):
        review_input = _review_input(case, index)
        settings = ModelServiceSettings(
            provider=ModelProvider.OPENAI,
            model="openreviewer-golden-flow-v1",
            api_key="offline-golden-flow-key",
            api_protocol=ModelApiProtocol.RESPONSES,
            context_window_tokens=128_000,
            max_output_tokens=4_096,
            max_batch_input_tokens=32_000,
            max_request_bytes=1024 * 1024,
            api_base_url="https://golden-flow.invalid",
        )
        batches = plan_model_review_batches(review_input, settings)
        if not batches:
            raise ValueError(f"golden flow produced no model batch: {case.case_id}")
        handler = _GoldenModelHandler(case)
        with httpx.Client(
            base_url="https://golden-flow.invalid",
            transport=httpx.MockTransport(handler),
        ) as client:
            reviewer = create_model_reviewer(settings, client=client)
            results = tuple(reviewer.review(batch.review_input) for batch in batches)
        combined = combine_model_review_results(
            review_input,
            results,
            batches=batches,
        )
        materialized = materialize_findings(review_input, combined.output)
        predictions[case.case_id] = frozenset(
            _finding_key(item.finding)
            for item in materialized
            if item.finding.verification_status is VerificationStatus.VERIFIED
        )
    return predictions


class _GoldenModelHandler:
    def __init__(self, case: GoldenFlowCase) -> None:
        self._case = case

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/responses":
            raise ValueError("golden flow used an unexpected model endpoint")
        body = json.loads(request.content)
        prompt_payload = self._prompt_payload(body)
        review_units = prompt_payload.get("review_units")
        if not isinstance(review_units, list) or len(review_units) != 1:
            raise ValueError("golden flow prompt lost its review unit")
        unit = review_units[0]
        if (
            not isinstance(unit, dict)
            or unit.get("file") != self._case.file
            or unit.get("patch") != self._case.patch
        ):
            raise ValueError("golden flow prompt changed the benchmark patch")
        output_contract = prompt_payload.get("output_contract")
        if not isinstance(output_contract, dict) or output_contract.get("type") != "object":
            raise ValueError("golden flow prompt lost its output contract")
        unit_key = unit.get("unit_key")
        if not isinstance(unit_key, str):
            raise ValueError("golden flow prompt lost its unit identity")
        findings = [
            _candidate_payload(key, unit_key, self._case.file)
            for key in sorted(self._case.predicted_finding_keys)
        ]
        output = {
            "verdict": "issues_found" if findings else "no_actionable_issue",
            "summary": "离线黄金集模型完成了当前补丁审查。",
            "checked_areas": ["黄金集回归"],
            "findings": findings,
        }
        return httpx.Response(
            200,
            headers={"x-request-id": f"golden-{self._case.case_id}"},
            json={
                "id": f"golden-{self._case.case_id}",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(output, ensure_ascii=False),
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 40},
            },
        )

    @staticmethod
    def _prompt_payload(body: object) -> Mapping[str, object]:
        if not isinstance(body, dict):
            raise ValueError("golden flow request body is not an object")
        inputs = body.get("input")
        if not isinstance(inputs, list) or len(inputs) != 2:
            raise ValueError("golden flow request lost its input messages")
        user_message = inputs[1]
        if not isinstance(user_message, dict):
            raise ValueError("golden flow user message is invalid")
        content = user_message.get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise ValueError("golden flow user content is invalid")
        block = content[0]
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            raise ValueError("golden flow user text is invalid")
        payload = json.loads(block["text"])
        if not isinstance(payload, dict):
            raise ValueError("golden flow prompt payload is invalid")
        return payload


def _review_input(case: GoldenFlowCase, index: int) -> ModelReviewInput:
    head_sha = sha256(f"head:{case.case_id}".encode()).hexdigest()[:40]
    blob_sha = sha256(f"blob:{case.case_id}".encode()).hexdigest()[:40]
    unit_key = sha256(f"unit:{case.case_id}".encode()).hexdigest()
    review_version_key = f"1:{index}:{head_sha}"
    rule_content = "# Golden review\nReport only findings directly supported by the diff.\n"
    rule_bytes = rule_content.encode()
    rule = RepositoryRule(
        path="AGENTS.md",
        scope=None,
        blob_sha=sha256(b"golden-rule").hexdigest()[:40],
        content=rule_content,
        content_sha256=sha256(rule_bytes).hexdigest(),
        byte_size=len(rule_bytes),
    )
    patch_bytes = case.patch.encode()
    unit = ReviewUnit(
        unit_key=unit_key,
        review_version_key=review_version_key,
        head_sha=head_sha,
        file=case.file,
        blob_sha=blob_sha,
        language=_language(case.file),
        patch=case.patch,
        patch_sha256=sha256(patch_bytes).hexdigest(),
        rule_paths=(rule.path,),
        estimated_input_bytes=len(patch_bytes),
        planner_version="review-planner-v2",
    )
    return ModelReviewInput(
        review_plan_id=f"golden-plan-{index}",
        review_run_id=f"golden-run-{index}",
        plan_fingerprint=sha256(f"plan:{case.case_id}".encode()).hexdigest(),
        planner_version="review-planner-v2",
        review_version_key=review_version_key,
        repository_id=1,
        repository="openreviewer/golden",
        pull_request_number=index,
        head_sha=head_sha,
        rules=(rule,),
        units=(unit,),
        total_estimated_input_bytes=len(patch_bytes) + len(rule_bytes),
    )


def _candidate_payload(key: str, unit_key: str, expected_file: str) -> dict[str, object]:
    parts = key.split(":", 3)
    if len(parts) != 4:
        raise ValueError(f"golden finding key is malformed: {key}")
    category, file, raw_line, slug = parts
    try:
        FindingCategory(category)
        line = int(raw_line)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"golden finding key is malformed: {key}") from exc
    if file != expected_file or line <= 0 or not slug:
        raise ValueError(f"golden finding key does not match its case: {key}")
    return {
        "unit_key": unit_key,
        "severity": Severity.HIGH.value,
        "category": category,
        "location": {
            "file": file,
            "start_line": line,
            "end_line": line,
            "side": LocationSide.RIGHT.value,
            "symbol": slug,
        },
        "title": slug,
        "evidence": f"黄金集快照记录了 {slug}。",
        "impact": "该问题会影响当前变更的正确性或安全性。",
        "suggestion": "按黄金集约束修复并保留对应回归测试。",
        "required_test": "覆盖该黄金集场景。",
        "confidence": 0.95,
        "rule_reference": "AGENTS.md",
        "identity_hint": slug,
    }


def _finding_key(finding: object) -> str:
    category = getattr(getattr(finding, "category", None), "value", None)
    location = getattr(finding, "location", None)
    title = getattr(finding, "title", None)
    if (
        not isinstance(category, str)
        or location is None
        or not isinstance(title, str)
    ):
        raise ValueError("golden flow produced an incomplete finding")
    return f"{category}:{location.file}:{location.start_line}:{title}"


def _language(file: str) -> str:
    if file.endswith(".py"):
        return "python"
    if file.endswith((".yml", ".yaml")):
        return "yaml"
    return "text"
