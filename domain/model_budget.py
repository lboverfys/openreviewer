"""每个 Review Plan 共用的模型调用硬预算契约。"""

from pydantic import BaseModel, ConfigDict, Field


class ModelBudgetPolicy(BaseModel):
    """在计划创建时固化、后续重试不可绕过的资源上限。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_http_calls: int = Field(default=64, ge=1, le=10_000)
    max_input_tokens: int = Field(default=2_000_000, ge=1_000, le=1_000_000_000)
    max_output_tokens: int = Field(default=250_000, ge=256, le=100_000_000)
    max_estimated_cost_microusd: int | None = Field(
        default=None,
        ge=1,
        le=1_000_000_000_000,
    )
    max_duration_seconds: int = Field(default=900, ge=30, le=86_400)
