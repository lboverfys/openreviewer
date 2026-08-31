"""每个 Review Plan 共用的模型调用资源阈值契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ModelBudgetEnforcement = Literal["observe", "enforce"]


class ModelBudgetPolicy(BaseModel):
    """在计划创建时固化模型资源上限及超限处理方式。

    ``observe`` 模式仍会记录请求、Token、费用和运行时长，但不会因为累计
    指标超过配置值而暂停任务；``enforce`` 是旧版硬预算行为，供已有计划
    兼容和需要严格限制的调用方显式选择。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enforcement: ModelBudgetEnforcement = Field(default="observe")

    max_http_calls: int = Field(default=64, ge=1, le=10_000)
    max_input_tokens: int = Field(default=2_000_000, ge=1_000, le=1_000_000_000)
    max_output_tokens: int = Field(default=250_000, ge=256, le=100_000_000)
    max_estimated_cost_microusd: int | None = Field(
        default=None,
        ge=1,
        le=1_000_000_000_000,
    )
    # 总时长是异常任务的运行保护，不是费用或 Token 配额；默认给串行
    # 三路 Agent 加汇总阶段留出一小时，仍受 24 小时配置上限约束。
    max_duration_seconds: int = Field(default=3_600, ge=30, le=86_400)
