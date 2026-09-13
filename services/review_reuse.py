"""跨提交的精确输入复用：只移除提交身份，保留代码、位置、证据和方案。"""

import json
import os
from dataclasses import asdict, dataclass
from hashlib import sha256

from domain.enums import ModelCallStatus, ReviewAgent
from domain.model_review import ModelReviewInput, ModelReviewResult, ModelTokenUsage
from services.model_review import StructuredReviewPromptBuilder


def _digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _translate(value, mapping):
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, dict):
        return {key: _translate(item, mapping) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_translate(item, mapping) for item in value]
    return value


@dataclass(frozen=True)
class ReuseIdentity:
    key: str
    normalize: dict[str, str]


def reuse_identity(review_input: ModelReviewInput, settings) -> ReuseIdentity | None:
    policy = review_input.repository_policy
    if (not policy or not policy.incremental_review or not policy.review_profile_id
            or not review_input.units or review_input.connection_test
            or review_input.review_agent is ReviewAgent.SUMMARY):
        return None
    mapping = {unit.unit_key: _digest(("unit", unit.file)) for unit in review_input.units}
    groups: dict[str, list[str]] = {}
    for unit in review_input.units:
        groups.setdefault(unit.group_key or unit.unit_key, []).append(unit.file)
    mapping.update({key: _digest(("group", sorted(files))) for key, files in groups.items()
                    if key not in mapping})
    for item in review_input.context_evidence:
        mapping[item.reference_id] = _digest(("evidence", item.file, item.blob_sha,
            item.symbol, item.start_line, item.end_line, item.content_hash))
    prompt = StructuredReviewPromptBuilder(settings.prompt_snapshot).build(
        review_input, settings.provider, settings.model, settings.resolved_api_protocol,
    )
    body = json.loads(prompt.user)
    body["target"].pop("head_sha", None)
    body["target"].pop("plan_fingerprint", None)
    for evidence in body.get("context_evidence", []):
        evidence.pop("head_sha", None)
        evidence.pop("index_id", None)
    configuration = asdict(settings)
    api_key = configuration.pop("api_key", None)
    configuration["connection_hash"] = _digest(api_key)
    identity = {
        "version": 1, "repository_id": review_input.repository_id,
        "profile_id": policy.review_profile_id, "agent": review_input.review_agent,
        "program": os.environ.get("OPENREVIEWER_DEPLOYMENT_IMAGE", "development"),
        "settings": configuration, "system": prompt.system,
        "input": _translate(body, mapping), "planner": review_input.planner_version,
        "blobs": [(unit.file, unit.blob_sha) for unit in review_input.units],
        "knowledge_versions": review_input.knowledge_versions,
        "dependencies": review_input.reuse_dependencies,
        "egress": policy.egress.model_dump(),
    }
    return ReuseIdentity(_digest(identity), mapping)


def reusable_payload(result: ModelReviewResult, identity: ReuseIdentity, head_sha: str):
    if result.status is not ModelCallStatus.SUCCEEDED or result.reused_from_run_id:
        return None
    payload = result.model_dump(mode="json")
    payload["output"] = _translate(payload["output"], identity.normalize)
    text = json.dumps(payload, ensure_ascii=False)
    # 模型在自然语言中写入旧 SHA 时不能仅替换身份后复用。
    if head_sha in text or len(text.encode()) > 256 * 1024:
        return None
    return payload


def restore_reused(payload, identity: ReuseIdentity, source_run: str, current_head: str):
    restored = dict(payload)
    restored["output"] = _translate(restored["output"], {value: key for key, value in identity.normalize.items()})
    original = ModelReviewResult.model_validate(restored)
    return original.model_copy(update={
        "usage": ModelTokenUsage(input_tokens=0, output_tokens=0),
        "duration_ms": 0, "estimated_cost_microusd": 0,
        "provider_request_id": None, "provider_response_id": None,
        "request_fingerprint": _digest(("reuse", identity.key, current_head)),
        "reused_from_run_id": source_run, "reused_input_tokens": original.usage.total_input_tokens,
    })
