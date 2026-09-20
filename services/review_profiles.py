"""捕获与装配不可变审查方案；解密、文本加工和客户端创建均在事务外。"""

import base64
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import uuid4

from domain.enums import (
    ModelApiProtocol,
    ModelProvider,
    ModelReasoningEffort,
    ReviewAgent,
)
from domain.platform import (
    PlatformConflictError,
    PlatformNotFoundError,
    ProfileCreate,
    ProfileView,
)
from domain.retrieval import RetrievalSettings, RetrievalSettingsView
from domain.security import redact_text
from persistence.review_profiles import ReviewProfileRepository
from services.agent_settings import AgentSettingsService
from services.agent_workflow import FixedAgentWorkflow
from services.ai_settings import ActiveAiRuntime, AiSecretCipher, AiSettingsService
from services.model_providers import create_model_reviewer
from services.model_review import (
    PROMPT_VERSION,
    ModelPricing,
    ModelServiceSettings,
    StructuredReviewPromptBuilder,
)
from services.rag import KnowledgeChunk, MarkdownKnowledgeBase
from services.rbac import ResourceScope
from services.retrieval import RetrievalSettingsService
from services.retrieval_providers import external_retrieval_paused
from services.review_planning import DeterministicReviewPlanner, ReviewPlanningSettings


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


class ReviewProfileService:
    def __init__(
        self,
        repository: ReviewProfileRepository,
        cipher: AiSecretCipher,
        ai: AiSettingsService,
        agents: AgentSettingsService,
        knowledge: MarkdownKnowledgeBase,
        retrieval: RetrievalSettingsService,
    ):
        self.repository, self.cipher = repository, cipher
        self.ai, self.agents, self.knowledge, self.retrieval = (
            ai,
            agents,
            knowledge,
            retrieval,
        )

    def create(
        self, draft: ProfileCreate, actor: str, scope: ResourceScope
    ) -> ProfileView:
        stamp = self.repository.stamp(draft.repository, scope)
        if draft.base_profile_id is not None:
            return self._create_candidate(draft, stamp, actor, scope)
        if stamp["ai"] != draft.expected_ai_revision:
            raise PlatformConflictError("AI 配置已变化，请刷新后保存方案")
        models = self.agents.model_settings()
        if set(models) != set(ReviewAgent):
            raise ValueError("请先完成四个 Agent 的配置，方案需要完整审查流程")
        settings = self.ai.get()
        retrieval, key = self.retrieval.runtime()
        chunks = self.knowledge.chunks()
        chunks = tuple(
            chunk
            for chunk in chunks
            if chunk.repository_scope is None
            or chunk.repository_scope == draft.repository.casefold()
        )
        allowed_sources = stamp["policy"].get("knowledge_sources")
        if allowed_sources is not None:
            allowed = frozenset(allowed_sources)
            chunks = tuple(chunk for chunk in chunks if chunk.source in allowed)
        credentials: dict[str, str | None] = {
            agent.value: model.api_key for agent, model in models.items()
        }
        credentials["retrieval"] = key
        agents: dict[str, dict[str, Any]] = {}
        for agent, model in models.items():
            values = asdict(model)
            values.pop("api_key")
            agents[agent.value] = values
        prompt: dict[str, Any] = {
            "system": StructuredReviewPromptBuilder.SYSTEM_PROMPT,
            "roles": {
                key.value: value
                for key, value in StructuredReviewPromptBuilder.ROLE_INSTRUCTIONS.items()
            },
            "version": PROMPT_VERSION,
        }
        prompt["content_sha256"] = StructuredReviewPromptBuilder(prompt).content_sha256
        snapshot = json.loads(
            _json(
                {
                    "schema_version": 1,
                    "agents": agents,
                    "prompt": prompt,
                    "planning": {
                        "max_units": settings.max_units,
                        "max_scope_depth": settings.max_scope_depth,
                        "max_unit_input_bytes": settings.max_unit_input_bytes,
                        "max_total_input_bytes": settings.max_total_input_bytes,
                    },
                    "knowledge": [asdict(chunk) for chunk in chunks],
                    "retrieval": {
                        "revision": retrieval.revision,
                        "settings": retrieval.settings.model_dump(mode="json"),
                        "tested": retrieval.tested,
                    },
                }
            )
        )
        return self._save_snapshot(draft, stamp, actor, scope, snapshot, credentials, stamp["ai"])

    def _create_candidate(self, draft: ProfileCreate, stamp, actor: str, scope: ResourceScope) -> ProfileView:
        assert draft.base_profile_id is not None
        source = self.repository.load(draft.base_profile_id, scope)
        if source["repository"].casefold() != draft.repository.casefold():
            raise PlatformNotFoundError("基础方案不属于目标仓库")
        snapshot = deepcopy(source["snapshot"])
        if sha256(_json(snapshot).encode()).hexdigest() != source["fingerprint"]:
            raise ValueError("基础方案快照校验失败")
        if snapshot.get("schema_version") != 1 or snapshot["prompt"]["version"] != PROMPT_VERSION:
            raise ValueError("基础方案协议与当前程序不兼容")
        prompt = snapshot["prompt"]
        prompt.pop("content_sha256", None)
        prompt["roles"].update({agent.value: redact_text(value) for agent, value in draft.role_instructions.items()})
        if draft.supplementary_instructions is not None:
            prompt["supplementary_instructions"] = redact_text(draft.supplementary_instructions)
        prompt["content_sha256"] = StructuredReviewPromptBuilder(prompt).content_sha256
        credentials = json.loads(base64.urlsafe_b64decode(self.cipher.decrypt(
            f"review_profile:{source['id']}", source["ciphertext"], source["nonce"], source["key_version"],
        )))
        return self._save_snapshot(draft, stamp, actor, scope, snapshot, credentials, source["ai_revision"])

    def _save_snapshot(self, draft, stamp, actor, scope, snapshot, credentials, ai_revision) -> ProfileView:
        identifier = str(uuid4())
        serialized = _json(snapshot).encode()
        if len(serialized) > 2 * 1024 * 1024:
            raise ValueError("审查方案超过 2 MiB，请缩小仓库知识范围")
        encrypted = self.cipher.encrypt(
            f"review_profile:{identifier}",
            base64.urlsafe_b64encode(_json(credentials).encode()).decode(),
        )
        return self.repository.create(
            {
                "id": identifier,
                "name": draft.name.strip(),
                "repository": stamp["repository"],
                "repository_key": stamp["repository"].casefold(),
                "note": draft.note,
                "fingerprint": sha256(serialized).hexdigest(),
                "ai_revision": ai_revision,
                "snapshot": snapshot,
                "summary": {
                    "prompt_version": snapshot["prompt"]["version"],
                    "prompt_content_sha256": snapshot["prompt"]["content_sha256"],
                    "base_profile_id": draft.base_profile_id,
                    "models": {
                        agent: model["model"] for agent, model in snapshot["agents"].items()
                    },
                    "knowledge_versions": {
                        chunk["source"]: chunk["version"] for chunk in snapshot["knowledge"]
                    },
                    "retrieval_settings": snapshot["retrieval"]["settings"],
                },
                "ciphertext": encrypted.ciphertext,
                "nonce": encrypted.nonce,
                "key_version": encrypted.key_version,
                "created_by": actor,
                "created_at": datetime.now(UTC),
            },
            stamp,
            actor,
            scope,
        )


class ReviewProfileRuntimeLoader:
    def __init__(
        self,
        repository: ReviewProfileRepository,
        cipher: AiSecretCipher,
        *,
        max_agent_concurrency: int = 1,
    ):
        self.repository, self.cipher = repository, cipher
        self.max_agent_concurrency = max_agent_concurrency

    def __call__(self, identifier: str) -> ActiveAiRuntime:
        row = self.repository.load(identifier)
        snapshot = row["snapshot"]
        if (
            snapshot.get("schema_version") != 1
            or snapshot["prompt"]["version"] != PROMPT_VERSION
        ):
            raise ValueError("方案使用的审查协议与当前程序不兼容，请保存新方案")
        if sha256(_json(snapshot).encode()).hexdigest() != row["fingerprint"]:
            raise ValueError("审查方案快照校验失败")
        credentials = json.loads(
            base64.urlsafe_b64decode(
                self.cipher.decrypt(
                    f"review_profile:{identifier}",
                    row["ciphertext"],
                    row["nonce"],
                    row["key_version"],
                )
            )
        )
        models: dict[ReviewAgent, ModelServiceSettings] = {}
        for agent, values in snapshot["agents"].items():
            values = dict(values)
            prices = values.pop("pricing", None)
            pricing: ModelPricing | None = None
            if prices:
                pricing = ModelPricing(
                    input_usd_per_million=Decimal(prices["input_usd_per_million"]),
                    output_usd_per_million=Decimal(prices["output_usd_per_million"]),
                    cache_read_usd_per_million=Decimal(
                        prices["cache_read_usd_per_million"]
                    )
                    if prices.get("cache_read_usd_per_million") is not None
                    else None,
                    cache_write_usd_per_million=Decimal(
                        prices["cache_write_usd_per_million"]
                    )
                    if prices.get("cache_write_usd_per_million") is not None
                    else None,
                )
            models[ReviewAgent(agent)] = ModelServiceSettings(
                **{
                    **values,
                    "provider": ModelProvider(values["provider"]),
                    "api_protocol": ModelApiProtocol(values["api_protocol"])
                    if values.get("api_protocol")
                    else None,
                    "reasoning_effort": ModelReasoningEffort(
                        values["reasoning_effort"]
                    ),
                    "api_key": credentials[agent],
                    "pricing": pricing,
                    "prompt_snapshot": snapshot["prompt"],
                }
            )
        reviewers = {}
        try:
            for agent, model in models.items():
                reviewers[agent] = create_model_reviewer(model)
            retrieval = snapshot["retrieval"]
            return ActiveAiRuntime(
                revision=row["ai_revision"],
                reviewer=None,
                planner=DeterministicReviewPlanner(
                    ReviewPlanningSettings(**snapshot["planning"])
                ),
                agent_workflow=FixedAgentWorkflow(
                    reviewers,
                    summary_reviewer=reviewers[ReviewAgent.SUMMARY],
                    agent_settings=models,
                    max_concurrency=self.max_agent_concurrency,
                ),
                profile_id=identifier,
                knowledge_chunks=tuple(
                    KnowledgeChunk(**chunk) for chunk in snapshot["knowledge"]
                ),
                retrieval_settings=(
                    RetrievalSettingsView(
                        revision=retrieval["revision"],
                        settings=RetrievalSettings.model_validate(
                            retrieval["settings"]
                        ),
                        tested=retrieval["tested"],
                        key_configured=bool(credentials.get("retrieval")),
                        external_calls_paused=external_retrieval_paused(),
                    ),
                    credentials.get("retrieval"),
                ),
            )
        except Exception:
            for reviewer in reviewers.values():
                reviewer.close()
            raise
