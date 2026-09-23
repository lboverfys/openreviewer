"""区分未完成的审查与需要人工接管的非文本审查范围。"""

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ReviewCoverage:
    status: str
    model_completed: bool
    rules_complete: bool | None = None
    file_decisions: Mapping[str, int] = field(default_factory=dict)
    exclusions_acknowledged: bool = False

    @property
    def excluded_count(self) -> int:
        return sum(count for decision, count in self.file_decisions.items() if decision != "planned")

    @property
    def can_acknowledge(self) -> bool:
        return (
            self.status == "partial" and self.model_completed and self.rules_complete is True
            and self.excluded_count > 0
            and all(decision in {"planned", "binary", "generated"}
                    for decision, count in self.file_decisions.items() if count)
        )

    @property
    def block_reason(self) -> str | None:
        if self.status == "stale":
            return "当前审查对应的提交已过期，请检查最新提交后再批准或发布。"
        if self.status != "partial":
            return None
        if not self.model_completed:
            return "仍有 Agent 或批次未完成，请完成失败节点后再批准或发布。"
        if self.can_acknowledge:
            if self.exclusions_acknowledged:
                return None
            return (
                f"AI 已完成可审查文本，另有 {self.excluded_count} 个二进制或生成文件未做文本审查。"
                "核对候选问题后，可在批准时确认由人工接管这些文件；无需重试成功批次。"
            )
        if self.excluded_count:
            return (
                f"AI 已完成当前范围，但有 {self.excluded_count} 个变更文件未进入审查，暂不能批准或发布。"
                "请查看排除原因，补齐支持规则或文件内容后创建新审查；重试已成功批次不会补入这些文件。"
            )
        if self.rules_complete is False:
            return "AI 已完成当前范围，但审查规则快照不完整，暂不能批准或发布。请补齐规则后创建新审查。"
        return "AI 已完成当前范围，但覆盖记录不完整，暂不能批准或发布。请创建新审查重新生成计划。"
