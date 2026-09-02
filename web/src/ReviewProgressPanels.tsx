import {
  agentDefinitions,
  agentProgress,
  formatDuration,
  payloadNumber,
  payloadString,
  verdictLabels,
} from "./review-details";
import type { ReviewDetails } from "./types";
import { formatDate, phaseLabels, stageLabels } from "./utils";

export function DetailIcon({ children }: { children: string }) {
  return <span className="review-detail-icon" aria-hidden="true">{children}</span>;
}

export function StageTimeline({ details }: { details: ReviewDetails }) {
  return (
    <section className="review-panel review-stage-panel">
      <div className="review-panel-heading">
        <div>
          <span className="review-eyebrow">PIPELINE</span>
          <h2>审查流程</h2>
        </div>
        <span className="review-stage-readout">
          当前：{stageLabels[details.current_stage] ?? details.current_stage}
        </span>
      </div>
      <div className="review-stage-timeline">
        {details.stages.map((stage, index) => (
          <div className={`review-stage-row stage-${stage.status}`} key={stage.key}>
            <div className="review-stage-marker">
              {stage.status === "completed" ? "✓" : stage.status === "failed" ? "!" : index + 1}
            </div>
            {index < details.stages.length - 1 && <span className="review-stage-connector" />}
            <div className="review-stage-copy">
              <div className="review-stage-title-line">
                <strong>{stageLabels[stage.key] ?? stage.key}</strong>
                <span>
                  {stage.status === "completed"
                    ? "已完成"
                    : stage.status === "current"
                      ? "进行中"
                      : stage.status === "failed"
                        ? "失败"
                        : stage.status === "blocked"
                          ? "等待前置操作"
                          : "等待中"}
                </span>
              </div>
              {stage.detail_code && stage.key === details.current_stage && (
                <small>{phaseLabels[stage.detail_code] ?? stage.detail_code}</small>
              )}
              {stage.completed_at && <time>{formatDate(stage.completed_at)}</time>}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
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
  const activeAgents = agentDefinitions.map((definition) => ({
    ...definition,
    progress: agentProgress(details.events, definition.key),
  }));
  const hasModelEvents = activeAgents.some((item) => item.progress.events.length > 0);
  if (!hasModelEvents && !details.model_review_completed_at) return null;

  return (
    <section className="review-panel review-batch-panel">
      <div className="review-panel-heading">
        <div><span className="review-eyebrow">LIVE MODEL PROGRESS</span><h2>四路 Agent 进度</h2></div>
        <span className="review-batch-readout">安全 · 规范 · 逻辑 · 汇总</span>
      </div>
      <div className="review-agent-grid">
        {activeAgents.map(({ key, label, description, progress }) => {
          const completeCount = progress.completedBatches.length;
          const failedCount = progress.failedBatches.length;
          const progressPercent = progress.batchCount > 0
            ? Math.min(100, (completeCount / progress.batchCount) * 100)
            : progress.status === "completed" ? 100 : 0;
          const statusLabel = progress.status === "completed"
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
          const displayStatusLabel = progress.status === "failed"
            && completeCount > 0
            ? "部分完成"
            : statusLabel;
          return (
            <article className={`review-agent-card is-${progress.status}`} key={key}>
              <header className="review-agent-card-header">
                <div><strong>{label}</strong><span>{description}</span></div>
                <b>{displayStatusLabel}</b>
              </header>
              <div className="review-agent-progress-meta">
                <span>{completeCount}/{progress.batchCount || "—"} 批</span>
                {failedCount > 0 && <span className="is-error">{failedCount} 批失败</span>}
                <span>{progress.findingCount} 条 Finding</span>
              </div>
              <div className="review-batch-progress" aria-hidden="true"><span style={{ width: `${progressPercent}%` }} /></div>
              <dl className="review-agent-metrics">
                <div><dt>耗时</dt><dd>{formatDuration(progress.duration)}</dd></div>
                <div><dt>输入 Token</dt><dd>{progress.inputTokens?.toLocaleString() ?? "—"}</dd></div>
                <div><dt>输出 Token</dt><dd>{progress.outputTokens?.toLocaleString() ?? "—"}</dd></div>
                <div><dt>推理 Token</dt><dd>{progress.reasoningTokens?.toLocaleString() ?? "—"}</dd></div>
              </dl>
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
                  <span>{details.coverage_status === "partial" ? "未执行" : "本地汇总"}</span>
                  {details.coverage_status === "partial"
                    ? "上游 Agent 未完成，汇总未执行"
                    : "没有重复或冲突候选，已使用本地确定性汇总"}
                </div>
              )}
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
              {progress.batchCount > 0 && (
                <div className="review-agent-batches">
                  {Array.from({ length: progress.batchCount }, (_, offset) => offset + 1).map((number) => {
                    const event = progress.batches.get(number);
                    const completed = event?.event_type === "review.model.batch_completed";
                    const failed = event?.event_type === "review.model.batch_failed";
                    const requestStarted = event?.event_type === "review.model.request_started";
                    const started = event?.event_type === "review.model.batch_started";
                    const batchStatus = completed ? "已完成" : failed ? "失败" : requestStarted ? "请求中" : started ? "准备发送" : "等待发送";
                    return (
                      <div className={`review-agent-batch-row ${completed ? "is-completed" : failed ? "is-failed" : requestStarted ? "is-running" : ""}`} key={number}>
                        <span>第 {number}/{progress.batchCount} 批</span><b>{batchStatus}</b>
                        {event && <small>{completed ? `输入 ${payloadNumber(event, "input_tokens")?.toLocaleString() ?? "—"} · 输出 ${payloadNumber(event, "output_tokens")?.toLocaleString() ?? "—"} · 推理 ${payloadNumber(event, "reasoning_tokens")?.toLocaleString() ?? "—"} · ${formatDuration(payloadNumber(event, "duration_ms"))}` : failed ? `${payloadString(event, "error_code") ?? "错误"} · ${payloadString(event, "error_message") ?? "模型请求失败"}` : `预计输入 ${payloadNumber(event, "estimated_input_tokens")?.toLocaleString() ?? "—"} Token`}</small>}
                        {failed && onRetry && (
                          <button
                            type="button"
                            className="review-agent-retry-btn"
                            disabled={retryBusy}
                            onClick={() => onRetry(key, number)}
                          >
                            {retryBusy ? "处理中…" : `重试第 ${number} 批`}
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
