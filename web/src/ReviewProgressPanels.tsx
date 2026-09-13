import { useMemo } from "react";
import ReviewBatchList from "./ReviewBatchList";
import {
  agentDefinitions,
  agentProgress,
  formatDuration,
  verdictLabels,
} from "./review-details";
import type { ReviewDetails } from "./types";
import { formatDate, phaseLabels, stageLabels } from "./utils";

export function DetailIcon({ children }: { children: string }) {
  return <span className="review-detail-icon" aria-hidden="true">{children}</span>;
}

function stageCaption(status: string, detail?: string | null): string {
  if (status === "current") return detail ? phaseLabels[detail] ?? "进行中" : "进行中";
  return ({ completed: "已完成", failed: "失败", blocked: "等待前置操作", pending: "待执行",
    cancelled: "已取消", superseded: "已被替代", rejected: "已驳回", paused: "已暂停", skipped: "未执行" } as Record<string, string>)[status] ?? status;
}

export function StageTimeline({ details }: { details: ReviewDetails }) {
  const terminated = ["cancelled", "superseded", "rejected"].includes(details.phase);
  const stopped = terminated || details.phase === "paused";
  const stages = details.stages.map(stage => ({ ...stage, status: stopped && stage.status === "current" ? details.phase : stage.status }));
  const groups = [
    { title: "获取代码", keys: ["intake", "context", "ci"] },
    { title: "AI 检查", keys: ["planning", "model", "agent_batches", "aggregating"] },
    { title: "核对结果", keys: ["approval"] },
    { title: "完成处理", keys: ["publish", "result"] },
  ];
  return <section className="review-panel review-stage-panel">
    <div className="review-panel-heading"><h2>审查流程</h2><span className="review-stage-readout">
      {phaseLabels[details.phase] ?? details.phase}
    </span></div>
    <div className="review-stage-timeline">{groups.map((group, index) => {
      const relevant = stages.filter(stage => group.keys.includes(stage.key));
      const required = relevant.filter(stage => !details.snapshot_review || !["ci", "approval", "publish"].includes(stage.key));
      const active = required.find(stage => ["cancelled", "superseded", "rejected", "failed", "paused", "current"].includes(stage.status));
      const status = active?.status ?? (required.length === 0 ? "skipped"
        : required.every(stage => stage.status === "completed") ? "completed"
        : terminated ? required.some(stage => stage.status === "completed") ? details.phase : "skipped" : "pending");
      return <div className={"review-stage-row stage-" + status} key={group.title}>
        <div className="review-stage-marker">{status === "completed" ? "✓" : index + 1}</div>
        <div className="review-stage-copy"><div className="review-stage-title-line"><strong>{group.title}</strong>
          <span>{required.length === 0 ? "无需此步" : stageCaption(status, active?.detail_code)}</span>
        </div></div>
      </div>;
    })}</div>
    <details className="review-stage-details"><summary>查看详细步骤</summary>
      {stages.map(stage => <div className="review-stage-history-row" key={stage.key}>
        <strong>{stageLabels[stage.key] ?? stage.key}</strong><span>{stageCaption(stage.status, stage.detail_code)}</span>
        {stage.completed_at && <time>{formatDate(stage.completed_at)}</time>}
      </div>)}
    </details>
  </section>;
}

export function ModelBatchPanel({
  details,
  onRetry,
  retryBusy = false,
}: {
  details: ReviewDetails;
  onRetry?: (agent: string, batchNumber?: number) => void;
  retryBusy?: boolean;
}) {
  const activeAgents = useMemo(() => agentDefinitions.map((definition) => ({
    ...definition,
    progress: agentProgress(details.events, definition.key, details.batch_progress?.[definition.key]),
  })), [details.events, details.batch_progress]);
  const hasModelEvents = activeAgents.some((item) => item.progress.events.length > 0);
  if (!hasModelEvents && !details.model_review_completed_at) return null;
  const stopped = ["cancelled", "superseded", "rejected", "paused"].includes(details.phase);

  return (
    <section className="review-panel review-batch-panel">
      <div className="review-panel-heading">
        <div><span className="review-eyebrow">LIVE MODEL PROGRESS</span><h2>各项 AI 检查结果</h2></div>
        <span className="review-batch-readout">安全 · 规范 · 逻辑 · 汇总</span>
      </div>
      <div className="review-agent-grid">
        {activeAgents.map(({ key, label, description, progress }) => {
          const programSummary = key === "summary" && details.aggregation_status === "local"
            && details.summary_status === "skipped" && details.coverage_status === "complete";
          const completeCount = progress.completedCount;
          const failedCount = progress.failedCount;
          const progressPercent = progress.batchCount > 0
            ? Math.min(100, (completeCount / progress.batchCount) * 100)
            : progress.status === "completed" ? 100 : 0;
          const statusLabel = programSummary ? "已完成程序汇总" : progress.status === "completed"
            ? "已完成"
            : progress.status === "disabled"
              ? "未启用"
            : progress.status === "not_applicable"
              ? "不适用"
            : progress.status === "failed"
              ? "失败"
              : progress.status === "not_executed"
                ? "未执行"
                : progress.status === "running"
                  ? "进行中"
                  : progress.status === "planned"
                    ? "已规划"
                    : "等待开始";
          const displayStatusLabel = stopped && progress.status !== "completed" && !programSummary ? details.phase === "paused" ? "已暂停" : "已停止" : progress.status === "failed"
            && completeCount > 0
            ? "部分完成"
            : statusLabel;
          return (
            <article className={`review-agent-card is-${stopped && progress.status !== "completed" ? "cancelled" : progress.status}`} key={key}>
              <header className="review-agent-card-header">
                <div><strong>{label}</strong><span>{description}</span></div>
                <b>{displayStatusLabel}</b>
              </header>
              {!programSummary && <div className="review-agent-progress-meta">
                <span>{completeCount}/{progress.batchCount || "—"} 批</span>
                {failedCount > 0 && <span className="is-error">{failedCount} 批失败</span>}
                <span>{progress.findingCount} 条候选问题</span>
              </div>}
              <div className="review-batch-progress" aria-hidden="true"><span style={{ width: `${programSummary ? 100 : progressPercent}%` }} /></div>
              {!programSummary && <dl className="review-agent-metrics">
                <div><dt>耗时</dt><dd>{formatDuration(progress.duration)}</dd></div>
                <div><dt>输入 Token</dt><dd>{progress.inputTokens?.toLocaleString() ?? "—"}</dd></div>
                <div><dt>输出 Token</dt><dd>{progress.outputTokens?.toLocaleString() ?? "—"}</dd></div>
                <div><dt>推理 Token</dt><dd>{progress.reasoningTokens?.toLocaleString() ?? "—"}</dd></div>
              </dl>}
              {progress.requestIds.length > 0 && (
                <div className="review-agent-detail"><span>请求 ID</span><code>{progress.requestIds.join(" · ")}</code></div>
              )}
              {progress.status === "completed" && progress.hasStructuredConclusion && progress.verdict && (
                <div className={`review-agent-conclusion verdict-${progress.verdict}`}>
                  <div className="review-agent-conclusion-title">
                    <span>Agent 结论</span>
                    <strong>{verdictLabels[progress.verdict]}</strong>
                  </div>
                  <p>{progress.conclusionSummary}</p>
                  {progress.checkedAreas.length > 0 && (
                    <div className="review-checked-areas" aria-label="实际检查范围">
                      {progress.checkedAreas.map((area) => <span key={area}>{area}</span>)}
                    </div>
                  )}
                </div>
              )}
              {progress.status === "completed" && !progress.hasStructuredConclusion && (
                <div className="review-agent-conclusion is-legacy">
                  <div className="review-agent-conclusion-title">
                    <span>Agent 结论</span>
                    <strong>历史任务未保存结论</strong>
                  </div>
                  <p>这条记录只保存了执行状态和问题数量，不能据此判断 Agent 的实际分析结论；重新审查后会生成完整摘要。</p>
                </div>
              )}
              {progress.errorMessage && progress.status === "failed" && (
                <div className="review-agent-error">
                  <strong>{progress.errorCode ? `错误码 ${progress.errorCode}` : "Agent 执行失败"}</strong>
                  <p>{progress.errorMessage}</p>
                </div>
              )}
              {progress.status === "not_executed" && key === "summary" && (
                <div className="review-agent-empty">
                  <span>{details.coverage_status === "partial" ? "等待上游" : "程序汇总"}</span>
                  {details.coverage_status === "partial"
                    ? "上游 Agent 未完成，汇总未执行"
                    : `三路结果已合并，共 ${details.finding_total_count} 条候选问题。程序完成去重和排序，本轮无需额外调用汇总模型。`}
                </div>
              )}
              {programSummary && <div className="program-summary-notes"><h4>本轮检查结论</h4>{activeAgents.filter(item => item.key !== "summary" && item.progress.conclusionSummary).map(item => <p key={item.key}><strong>{item.label}：</strong>{item.progress.conclusionSummary}</p>)}<small>以上为各审查 Agent 的原始结论摘要；未报告问题不代表代码绝对没有缺陷。</small></div>}
              {progress.status === "not_applicable" && (
                <div className="review-agent-empty">
                  <span>不适用</span>
                  当前变更没有落入此 Agent 的职责范围
                </div>
              )}
              {progress.status === "failed"
                && onRetry
                && (key === "summary" || progress.batchCount === 0 || failedCount > 1) && (
                  <button
                    type="button"
                    className="review-agent-retry-btn"
                    disabled={retryBusy}
                    onClick={() => onRetry(key)}
                  >
                    {retryBusy
                      ? "处理中…"
                      : key === "summary"
                        ? "重试汇总"
                        : failedCount > 1
                          ? `重试失败批次（${failedCount}）`
                          : `重试${label}`}
                  </button>
                )}
              {progress.references.length > 0 && (
                <details className="review-agent-references">
                  <summary>RAG 引用（{progress.references.length}）</summary>
                  <ul>{progress.references.map((reference, index) => <li key={`${reference}-${index}`}>{reference}</li>)}</ul>
                </details>
              )}
              {progress.batchCount > 0 && <ReviewBatchList runId={details.review_run_id} agent={key}
                total={progress.batchCount} changeToken={details.change_token} onRetry={onRetry} busy={retryBusy} stopped={stopped} paused={details.phase === "paused"} />}
            </article>
          );
        })}
      </div>
    </section>
  );
}
