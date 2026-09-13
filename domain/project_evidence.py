"""只导出聚合证据，明确人工复核与工程验证各自的状态。"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from domain.evaluation_workbench import EvaluationComparisonReport


class ProjectEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    generated_at: datetime
    evaluation_status: Literal["awaiting_human_review", "partial_review", "reviewed_samples"]
    evaluation: EvaluationComparisonReport | None
    limitations: tuple[str, ...]
    next_steps: tuple[str, ...]


def project_evidence(report: EvaluationComparisonReport | None) -> ProjectEvidence:
    status: Literal["awaiting_human_review", "partial_review", "reviewed_samples"] = "awaiting_human_review"
    if report and report.quality_pairs:
        status = "reviewed_samples" if (report.reference_pairs == report.case_count
            and not report.provenance_missing_pairs and report.split == "validation") else "partial_review"
    return ProjectEvidence(generated_at=datetime.now(UTC), evaluation_status=status,
        evaluation=report, limitations=(
            "数据只覆盖报告所列样本，比例需结合分母和 95% 区间解释",
            "录制输出、模拟模型与故障恢复测试不证明真实模型准确率",
            "配置价格下的估算费用不等同于供应商账单",
            "未完成双人复核时不得填写准确率、漏报改善或人工时间节省百分比",
        ), next_steps=(
            "固定同一 PR/SHA，分别用基线和候选方案完成独立审查，评测时关闭增量复用",
            "覆盖正常、已知缺陷和跨文件样本，并固定调参与验收划分",
            "两位成员独立确认参考缺陷和每条问题，处理分歧后导出聚合报告",
            "面试时同时展示对应提交的 CI 故障恢复产物及其隔离环境说明",
        ))
