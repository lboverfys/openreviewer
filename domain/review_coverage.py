"""批准、发布与详情展示共用的覆盖阻塞原因。"""


def coverage_block_reason(
    coverage_status: str,
    *,
    model_completed: bool,
    excluded_file_count: int = 0,
    rules_complete: bool | None = None,
) -> str | None:
    if coverage_status == "stale":
        return "当前审查对应的提交已过期，请检查最新提交后再批准或发布。"
    if coverage_status != "partial":
        return None
    if not model_completed:
        return "仍有 Agent 或批次未完成，请完成失败节点后再批准或发布。"
    if excluded_file_count:
        return (
            f"AI 已完成当前范围，但有 {excluded_file_count} 个变更文件未进入审查，暂不能批准或发布。"
            "请查看排除原因，补齐支持规则或文件内容后创建新审查；重试已成功批次不会补入这些文件。"
        )
    if rules_complete is False:
        return "AI 已完成当前范围，但审查规则快照不完整，暂不能批准或发布。请补齐规则后创建新审查。"
    return "AI 已完成当前范围，但覆盖记录不完整，暂不能批准或发布。请创建新审查重新生成计划。"
