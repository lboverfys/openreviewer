import type { AuthUser } from "./types";
import { hasPermission } from "./rbac";

export default function WorkflowGuide({user}: {user: AuthUser}) {
  return <details className="workflow-guide" open><summary>平台怎么用：配置 → 提交 PR → 看结果 → 处理问题</summary>
    <div className="workflow-guide-grid">
      {hasPermission(user,"settings:manage") && <a href="#settings"><b>01 · 配好模型</b><span>审查用哪个模型、多少钱，是否启用向量和精排。</span></a>}
      {hasPermission(user,"knowledge:manage") && <a href="#knowledge"><b>02 · 准备审查依据</b><span>补齐团队规则与业务约定；关联代码由检索自动提供。</span></a>}
      <div><b>03 · 提交 GitHub PR</b><span>系统接收变更，等待 CI，然后固定提交、准备上下文并执行审查。</span></div>
      <a href="#platform"><b>04 · 核对与处理</b><span>在任务里核对每条问题，再批准、发布或建立修复待办。</span></a>
      <a href="#evaluations"><b>05 · 按需评测效果</b><span>把已完成审查收录为样本，记录误报和漏报；换模型后再比较。</span></a>
    </div>
  </details>;
}
