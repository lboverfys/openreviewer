"""人工补跑的当前配置与影响范围，只读取非敏感参数。"""

from sqlalchemy import func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from domain.enums import ModelApiProtocol, ModelProvider, ModelReasoningEffort
from domain.review_progress import AgentRetryImpact
from persistence.models import (
    AiAgentConfigRecord,
    AiProviderConfigRecord,
    AiSettingsRecord,
    ModelReviewBatchRecord,
    OutboxEventRecord,
    ReviewPlanRecord,
)
from services.model_review import (
    ModelServiceSettings,
    model_configuration,
    model_configuration_matches,
)


def current_model_configurations(session: Session) -> dict[str, dict[str, str]]:
    fields = (
        "provider",
        "model",
        "api_protocol",
        "api_base_url",
        "reasoning_effort",
        "context_window_tokens",
        "max_output_tokens",
        "max_batch_input_tokens",
    )
    rows = (
        session.execute(
            select(
                AiAgentConfigRecord.agent,
                AiAgentConfigRecord.use_shared_connection,
                AiAgentConfigRecord.model_override,
                *(getattr(AiAgentConfigRecord, name) for name in fields),
                *(
                    getattr(AiProviderConfigRecord, name).label(f"shared_{name}")
                    for name in fields
                ),
                AiProviderConfigRecord.test_status.label("shared_test_status"),
            )
            .select_from(AiAgentConfigRecord)
            .outerjoin(AiSettingsRecord, AiSettingsRecord.id == 1)
            .outerjoin(
                AiProviderConfigRecord,
                AiProviderConfigRecord.provider == AiSettingsRecord.active_provider,
            )
            .where(
                AiAgentConfigRecord.enabled.is_(True),
                AiAgentConfigRecord.test_status == "succeeded",
            )
            .limit(4)
        )
        .mappings()
        .all()
    )
    configurations = {}
    for row in rows:
        if row["use_shared_connection"]:
            if row["shared_test_status"] != "succeeded":
                continue
            values = {name: row[f"shared_{name}"] for name in fields}
        else:
            values = {name: row[name] for name in fields}
        if row["use_shared_connection"] and row["model_override"]:
            values["model"] = row["model_override"]
        settings = ModelServiceSettings(
            **{
                **values,
                "provider": ModelProvider(values["provider"]),
                "api_protocol": ModelApiProtocol(values["api_protocol"]),
                "reasoning_effort": ModelReasoningEffort(values["reasoning_effort"]),
                # 仅构造非敏感配置指纹，不创建客户端、不解密或发送凭据。
                "api_key": "configuration-comparison-only",
            }
        )
        configurations[row["agent"]] = model_configuration(settings)
    return configurations


def retry_impacts(
    session: Session, plan_id: str, agents: tuple[str, ...]
) -> dict[str, AgentRetryImpact]:
    if not agents:
        return {}
    configurations = current_model_configurations(session)
    batch = ModelReviewBatchRecord
    keys = (
        "provider",
        "api_protocol",
        "model",
        "prompt_version",
        "configuration_fingerprint",
        "provenance",
    )
    rows = (
        session.execute(
            select(
                batch.agent,
                batch.batch_number,
                batch.status,
                *(batch.result[key].label(key) for key in keys),
            )
            .where(batch.review_plan_id == plan_id, batch.agent.in_(agents))
            .order_by(batch.agent, batch.batch_number)
            .limit(3001)
        )
        .mappings()
        .all()
    )
    if len(rows) > 3000:
        raise ValueError("模型批次数量超过安全上限")
    ranked = (
        select(
            OutboxEventRecord.payload,
            func.row_number()
            .over(
                partition_by=OutboxEventRecord.payload["agent"].as_string(),
                order_by=(
                    OutboxEventRecord.occurred_at.desc(),
                    OutboxEventRecord.id.desc(),
                ),
            )
            .label("position"),
        )
        .join(
            ReviewPlanRecord,
            ReviewPlanRecord.review_run_id == OutboxEventRecord.aggregate_id,
        )
        .where(
            ReviewPlanRecord.id == plan_id,
            OutboxEventRecord.event_type == "review.model.batches_planned",
            OutboxEventRecord.payload["agent"].as_string().in_(agents),
        )
        .subquery()
    )
    planned = {
        payload["agent"]: payload
        for payload in session.scalars(
            select(ranked.c.payload).where(ranked.c.position == 1).limit(4)
        )
    }
    grouped: dict[str, list[RowMapping]] = {agent: [] for agent in agents}
    for row in rows:
        grouped[row["agent"]].append(row)
    return {
        agent: AgentRetryImpact(
            agent=agent,
            model=configurations[agent]["model"],
            previous_models=tuple(
                sorted({row["model"] for row in batches if row["model"]})
            ),
            batch_count=len(batches),
            restart=(
                bool(batches) and all(row["status"] == "succeeded" for row in batches)
            )
            or any(
                key in planned.get(agent, {})
                and str(planned[agent][key]) != configurations[agent][key]
                for key in (
                    "model",
                    "provider",
                    "api_protocol",
                    "configuration_fingerprint",
                    "reasoning_effort",
                    "context_window_tokens",
                    "max_batch_input_tokens",
                )
            )
            or any(
                row["status"] == "succeeded"
                and not model_configuration_matches(dict(row), configurations[agent])
                for row in batches
            ),
        )
        for agent, batches in grouped.items()
        if agent in configurations
    }
