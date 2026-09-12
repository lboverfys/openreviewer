import { useState } from "react";
import Pagination, { PAGE_SIZE } from "./Pagination";
import {
  formatBytes,
  formatDuration,
  payloadString,
  reasoningEffortLabels,
} from "./review-details";
import type { ReviewDetails, ReviewEvent } from "./types";
import { formatDate, shortSha } from "./utils";

export const ciStateLabels: Record<string, string> = {
  not_configured: "未配置",
  unknown: "未知",
  pending: "进行中",
  success: "通过",
  failure: "失败",
};

export const fileDecisionLabels: Record<string, string> = {
  planned: "已送 AI",
  binary: "二进制",
  generated: "生成文件",
  unsupported: "不支持的类型",
  patch_missing: "Diff 缺失",
  patch_too_large: "Diff 过大",
  rules_incomplete: "规则不完整",
  omitted_by_budget: "历史范围记录",
};

interface ReviewSidebarProps {
  details: ReviewDetails;
  retryPending: boolean;
  retryStatus: string | null;
  modelDisplayState: string;
  modelStateClass: string;
  modelDisplayName: string | null;
  latestBatchPlan: ReviewEvent | undefined;
  failureStatus: number | null;
  failureDuration: number | null;
  failureCode: string | null;
  failureRequestId: string | null;
  hasBranchRoute: boolean;
  headBranchLabel: string;
  baseBranchLabel: string;
}

export function ReviewSidebar({
  details,
  retryPending,
  retryStatus,
  modelDisplayState,
  modelStateClass,
  modelDisplayName,
  latestBatchPlan,
  failureStatus,
  failureDuration,
  failureCode,
  failureRequestId,
  hasBranchRoute,
  headBranchLabel,
  baseBranchLabel,
}: ReviewSidebarProps) {
  const [ciPage, setCiPage] = useState(1);
  return (
    <aside className="review-detail-sidebar">
      <section className="review-panel review-summary-panel">
        <div className="review-panel-heading"><div><span className="review-eyebrow">RUN SUMMARY</span><h2>运行信息</h2></div></div>
        <dl className="review-summary-list">
          <div><dt>运行 ID</dt><dd><code title={details.review_run_id}>{details.review_run_id.slice(0, 12)}…</code></dd></div>
          <div><dt>任务 ID</dt><dd><code title={details.review_task_id}>{details.review_task_id.slice(0, 12)}…</code></dd></div>
          <div><dt>版本 SHA</dt><dd><code>{shortSha(details.head_sha)}</code></dd></div>
          <div><dt>覆盖状态</dt><dd>{details.coverage_status === "complete" ? "完整" : details.coverage_status === "partial" ? "部分" : details.coverage_status}</dd></div>
          <div><dt>创建时间</dt><dd>{formatDate(details.created_at)}</dd></div>
          <div><dt>{retryPending ? "自动重试" : "可用时间"}</dt><dd>{retryPending ? retryStatus ?? formatDate(details.available_at) : formatDate(details.available_at)}</dd></div>
        </dl>
      </section>

      <section className="review-panel review-model-panel">
        {details.repository_policy && <div className="review-panel-heading">
          <div>
            <h2>仓库策略 · v{details.repository_policy.revision}</h2>
            <p>目标分支：{details.repository_policy.target_branches?.join("、") || "全部"}</p>
            <p>审批负责人：{details.repository_policy.approver || "有审批权限的成员"}</p>
            <p>知识规则：{details.repository_policy.knowledge_sources == null ? "继承知识库" : details.repository_policy.knowledge_sources.join("、") || "不使用知识库"}</p>
            {details.repository_policy.max_model_requests != null && <p>请求额度已用：{details.model_request_count ?? 0} / {details.repository_policy.max_model_requests} 次</p>}
          </div>
        </div>}
        <div className="review-panel-heading"><div><span className="review-eyebrow">MODEL CALL</span><h2>AI 调用</h2></div><span className={`review-model-state ${modelStateClass}`}>{modelDisplayState}</span></div>
        <dl className="review-metric-grid">
          <div><dt>模型</dt><dd>{modelDisplayName || "—"}</dd></div>
          <div><dt>供应商</dt><dd>{details.model_provider ?? payloadString(latestBatchPlan, "provider") ?? "—"}</dd></div>
          <div><dt>接口</dt><dd>{details.model_protocol ?? payloadString(latestBatchPlan, "api_protocol") ?? "—"}</dd></div>
          <div><dt>推理档位</dt><dd>{reasoningEffortLabels[payloadString(latestBatchPlan, "reasoning_effort") ?? ""] ?? payloadString(latestBatchPlan, "reasoning_effort") ?? "—"}</dd></div>
          <div><dt>响应</dt><dd>{details.model_response_status ?? failureStatus ?? "—"}</dd></div>
          <div><dt>耗时</dt><dd>{formatDuration(details.model_duration_ms ?? failureDuration)}</dd></div>
          <div><dt>候选问题</dt><dd>{details.model_finding_count ?? "—"}</dd></div>
          {failureCode && <div><dt>错误码</dt><dd>{failureCode}</dd></div>}
          {failureRequestId && <div><dt>请求 ID</dt><dd><code title={failureRequestId}>{failureRequestId}</code></dd></div>}
        </dl>
      </section>

      <section className="review-panel review-context-panel">
        <div className="review-panel-heading"><div><span className="review-eyebrow">GITHUB CONTEXT</span><h2>代码与 CI</h2></div><span className={`review-ci-state ci-${details.ci_state ?? "unknown"}`}>{ciStateLabels[details.ci_state ?? ""] ?? "未知"}</span></div>
        <dl className="review-context-list">
          <div><dt>PR 状态</dt><dd>{details.pr_state ?? "—"}{details.pr_is_draft ? " · Draft" : ""}</dd></div>
          <div><dt>提起人</dt><dd>{details.pr_author_login ? `@${details.pr_author_login}` : "历史任务未记录"}</dd></div>
          <div><dt>来源分支</dt><dd><code>{hasBranchRoute ? headBranchLabel : "历史任务未记录"}</code></dd></div>
          <div><dt>目标分支</dt><dd><code>{hasBranchRoute ? baseBranchLabel : "历史任务未记录"}</code></dd></div>
          <div><dt>变更文件</dt><dd>{details.changed_files_count ?? "—"} 个</dd></div>
          <div><dt>文件快照</dt><dd>{details.files_complete === null ? "—" : details.files_complete ? "完整" : "部分"}</dd></div>
          <div><dt>Diff 快照</dt><dd>{details.diff_complete === null ? "—" : details.diff_complete ? "完整" : "部分"}</dd></div>
          <div><dt>CI 检查</dt><dd>{details.ci_checks.length} 项 · {details.ci_checks_complete ? "完整" : "持续刷新"}</dd></div>
          {details.pr_html_url && <div><dt>Pull Request</dt><dd><a href={details.pr_html_url} target="_blank" rel="noreferrer">在 GitHub 打开 ↗</a></dd></div>}
        </dl>
        {details.ci_checks.length > 0 && <div className="review-ci-check-list">{details.ci_checks.slice((ciPage - 1) * PAGE_SIZE, ciPage * PAGE_SIZE).map((check) => <div key={`${check.kind}:${check.name}`}><span className={`ci-check-dot ci-check-${check.conclusion ?? check.status}`} /><span>{check.name}</span><small>{check.conclusion ?? check.status}</small></div>)}</div>}
        {details.ci_checks.length > PAGE_SIZE && <Pagination page={ciPage} count={Math.min(PAGE_SIZE, details.ci_checks.length - (ciPage - 1) * PAGE_SIZE)} total={details.ci_checks.length} hasNext={ciPage * PAGE_SIZE < details.ci_checks.length} onPrevious={() => setCiPage(value => value - 1)} onNext={() => setCiPage(value => value + 1)} label="CI检查分页" />}
      </section>

      {details.review_plan_id && <section className="review-panel review-plan-panel"><div className="review-panel-heading"><div><span className="review-eyebrow">REVIEW COVERAGE</span><h2>文件覆盖</h2></div></div><div className="review-plan-stats"><div><strong>{details.plan_file_count ?? 0}</strong><span>变更文件</span></div><div><strong>{details.plan_unit_count ?? 0}</strong><span>送入 AI</span></div><div><strong>{details.plan_rule_count ?? 0}</strong><span>规则</span></div></div><div className="review-decision-list">{Object.entries(details.plan_file_decisions).map(([decision, count]) => <div key={decision}><span>{fileDecisionLabels[decision] ?? decision}</span><strong>{count}</strong></div>)}</div><div className="review-plan-bytes">可审查输入 {formatBytes(details.plan_input_bytes)} · 超长内容自动分批</div></section>}
    </aside>
  );
}
