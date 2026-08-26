import { useCallback, useEffect, useMemo, useState } from "react";

import { api, ApiError } from "./api";
import type {
  AuthUser,
  FindingDecision,
  ReviewAction,
  ReviewDetails,
  ReviewEvent,
  ReviewFinding,
} from "./types";
import {
  errorMessage,
  formatDate,
  phaseLabels,
  shortSha,
  stageLabels,
} from "./utils";

interface ReviewDetailPageProps {
  user: AuthUser;
  reviewRunId: string;
  onBack: () => void;
  onOpenReview: (reviewRunId: string) => void;
  onSignedOut: (message?: string) => void;
}

const actionLabels: Record<ReviewAction, string> = {
  expedite: "立即唤醒",
  retry: "重试本阶段",
  cancel: "取消任务",
  rerun: "重新审查",
};

const actionIcons: Record<ReviewAction, string> = {
  expedite: "↯",
  retry: "↻",
  cancel: "×",
  rerun: "⟳",
};

const severityLabels: Record<string, string> = {
  critical: "严重",
  high: "高风险",
  medium: "中风险",
  low: "低风险",
};

const findingStatusLabels: Record<string, string> = {
  unverified: "未标记",
  verified: "已确认",
  rejected: "已忽略",
};

const eventLabels: Record<string, string> = {
  "review.requested": "任务已接收",
  "review.task.running": "Worker 开始处理",
  "review.waiting_for_ci": "等待 CI",
  "review.ready_for_review": "CI 已完成",
  "review.plan.prepared": "审查计划已生成",
  "review.model.batches_planned": "AI 批次已规划",
  "review.model.batch_started": "AI 批次开始",
  "review.model.batch_completed": "AI 批次完成",
  "review.model.completed": "AI 分析完成",
  "review.task.retry_scheduled": "已安排自动重试",
  "review.task.failed": "任务失败",
  "review.ci_timed_out": "CI 等待超时",
  "review.cancelled": "任务已取消",
  "review.superseded": "任务被新提交替代",
  "review.manual.expedite": "管理员立即唤醒任务",
  "review.manual.retry": "管理员重试任务",
  "review.manual.cancel": "管理员取消任务",
  "review.manual.rerun_requested": "管理员发起重新审查",
  "review.finding.decided": "管理员更新问题裁决",
};

const fileDecisionLabels: Record<string, string> = {
  planned: "已送 AI",
  binary: "二进制",
  generated: "生成文件",
  unsupported: "不支持的类型",
  patch_missing: "Diff 缺失",
  patch_too_large: "Diff 过大",
  rules_incomplete: "规则不完整",
  omitted_by_budget: "旧版范围省略",
};

function actionKey(): string {
  return `ui:${Date.now()}:${crypto.randomUUID()}`;
}

function formatBytes(value: number | null): string {
  if (value === null || value === undefined) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(2)} MiB`;
}

function formatDuration(value: number | null): string {
  if (value === null || value === undefined) return "—";
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(1)} s`;
}

function payloadNumber(event: ReviewEvent | undefined, key: string): number | null {
  const value = event?.payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function payloadString(event: ReviewEvent | undefined, key: string): string | null {
  const value = event?.payload[key];
  return typeof value === "string" ? value : null;
}

function latestBatchPlanEvent(events: ReviewEvent[]): ReviewEvent | undefined {
  return events
    .filter((event) => event.event_type === "review.model.batches_planned")
    .reduce<ReviewEvent | undefined>((latest, event) => {
      if (!latest) return event;
      return (payloadNumber(event, "model_attempt_count") ?? -1)
        > (payloadNumber(latest, "model_attempt_count") ?? -1)
        ? event
        : latest;
    }, undefined);
}

function eventDetail(event: ReviewEvent): string | null {
  if (event.event_type === "review.model.batches_planned") {
    const batches = payloadNumber(event, "batch_count");
    const files = payloadNumber(event, "file_count");
    const context = payloadNumber(event, "context_window_tokens");
    return `${batches ?? "—"} 批 · ${files ?? "—"} 个文件 · 上下文 ${context?.toLocaleString() ?? "—"} Token`;
  }
  if (event.event_type === "review.model.batch_started") {
    const number = payloadNumber(event, "batch_number");
    const total = payloadNumber(event, "batch_count");
    const files = payloadNumber(event, "file_count");
    return `第 ${number ?? "—"}/${total ?? "—"} 批 · ${files ?? "—"} 个文件 · 约 ${payloadNumber(event, "estimated_input_tokens")?.toLocaleString() ?? "—"} Token`;
  }
  if (event.event_type === "review.model.batch_completed") {
    const number = payloadNumber(event, "batch_number");
    const total = payloadNumber(event, "batch_count");
    return `第 ${number ?? "—"}/${total ?? "—"} 批 · 输入 ${payloadNumber(event, "input_tokens")?.toLocaleString() ?? "—"} · 输出 ${payloadNumber(event, "output_tokens")?.toLocaleString() ?? "—"} · 推理 ${payloadNumber(event, "reasoning_tokens")?.toLocaleString() ?? "—"} Token · ${formatDuration(payloadNumber(event, "duration_ms"))}`;
  }
  return null;
}

function DetailIcon({ children }: { children: string }) {
  return <span className="review-detail-icon" aria-hidden="true">{children}</span>;
}

function StageTimeline({ details }: { details: ReviewDetails }) {
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

function ModelBatchPanel({ details }: { details: ReviewDetails }) {
  const planned = latestBatchPlanEvent(details.events);
  const latestAttempt = payloadNumber(planned, "model_attempt_count");
  const modelEvents = details.events.filter((event) =>
    event.event_type.startsWith("review.model.batch")
    && (latestAttempt === null || payloadNumber(event, "model_attempt_count") === latestAttempt),
  );
  if (!planned && modelEvents.length === 0) return null;

  const started = new Map<number, ReviewEvent>();
  const completed = new Map<number, ReviewEvent>();
  for (const event of modelEvents) {
    const number = payloadNumber(event, "batch_number");
    if (number === null) continue;
    if (event.event_type === "review.model.batch_started") started.set(number, event);
    if (event.event_type === "review.model.batch_completed") completed.set(number, event);
  }
  const batchCount = payloadNumber(planned, "batch_count")
    ?? Math.max(0, ...started.keys(), ...completed.keys());
  const completeCount = completed.size;

  return (
    <section className="review-panel review-batch-panel">
      <div className="review-panel-heading">
        <div><span className="review-eyebrow">LIVE MODEL PROGRESS</span><h2>AI 分批进度</h2></div>
        <span className={`review-batch-readout ${completeCount === batchCount ? "is-complete" : ""}`}>{completeCount}/{batchCount} 批</span>
      </div>
      <div className="review-batch-progress" aria-hidden="true"><span style={{ width: `${batchCount ? (completeCount / batchCount) * 100 : 0}%` }} /></div>
      <div className="review-batch-list">
        {Array.from({ length: batchCount }, (_, offset) => offset + 1).map((number) => {
          const start = started.get(number);
          const finish = completed.get(number);
          const failed = details.execution_status === "failed" && Boolean(start) && !finish;
          const status = finish ? "completed" : failed ? "failed" : start ? "running" : "pending";
          const firstFile = payloadString(start, "first_file");
          const lastFile = payloadString(start, "last_file");
          return (
            <div className={`review-batch-row is-${status}`} key={number}>
              <span className="review-batch-marker">{finish ? "✓" : number}</span>
              <div className="review-batch-copy">
                <div><strong>第 {number}/{batchCount} 批</strong><span>{finish ? "已完成" : failed ? "本批失败" : start ? "模型响应中" : "等待中"}</span></div>
                {start && <p>{payloadNumber(start, "file_count") ?? 0} 个文件 · {firstFile}{lastFile && lastFile !== firstFile ? ` 至 ${lastFile}` : ""}</p>}
                {finish ? <small>输入 {payloadNumber(finish, "input_tokens")?.toLocaleString() ?? "—"} · 输出 {payloadNumber(finish, "output_tokens")?.toLocaleString() ?? "—"} · 推理 {payloadNumber(finish, "reasoning_tokens")?.toLocaleString() ?? "—"} Token · {formatDuration(payloadNumber(finish, "duration_ms"))} · {payloadNumber(finish, "finding_count") ?? 0} 条候选</small> : start && <small>预计输入 {payloadNumber(start, "estimated_input_tokens")?.toLocaleString() ?? "—"} Token{start.payload.fragmented === true ? " · 含文件切片" : ""}</small>}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function FindingCard({
  finding,
  busy,
  onDecision,
}: {
  finding: ReviewFinding;
  busy: boolean;
  onDecision: (finding: ReviewFinding, decision: FindingDecision) => void;
}) {
  const reviewed = finding.verification_status !== "unverified";
  return (
    <article className={`review-finding-card finding-${finding.severity}`}>
      <div className="finding-card-topline">
        <div className="finding-severity">
          <span className="finding-severity-dot" />
          {severityLabels[finding.severity] ?? finding.severity}
        </div>
        <span className={`finding-status status-${finding.verification_status}`}>
          {findingStatusLabels[finding.verification_status] ?? finding.verification_status}
        </span>
      </div>
      <h3>{finding.title}</h3>
      <div className="finding-location">
        <code>{finding.location_file ?? "未定位到具体文件"}</code>
        {finding.location_start_line && (
          <span>
            第 {finding.location_start_line}
            {finding.location_end_line && finding.location_end_line !== finding.location_start_line
              ? `-${finding.location_end_line}`
              : ""} 行
          </span>
        )}
        <span>置信度 {Math.round(finding.confidence * 100)}%</span>
      </div>
      <div className="finding-copy-grid">
        <div><span>证据</span><p>{finding.evidence}</p></div>
        <div><span>影响</span><p>{finding.impact}</p></div>
        <div><span>建议</span><p>{finding.suggestion}</p></div>
        {finding.required_test && <div><span>建议补测</span><p>{finding.required_test}</p></div>}
      </div>
      {!reviewed && (
        <div className="finding-actions">
          <button
            type="button"
            className="review-action-btn finding-confirm-btn"
            disabled={busy}
            onClick={() => onDecision(finding, "verified")}
          >
            <DetailIcon>✓</DetailIcon>确认问题
          </button>
          <button
            type="button"
            className="review-action-btn finding-reject-btn"
            disabled={busy}
            onClick={() => onDecision(finding, "rejected")}
          >
            <DetailIcon>×</DetailIcon>忽略
          </button>
        </div>
      )}
      {reviewed && finding.reviewed_at && (
        <small className="finding-reviewed-note">由 {finding.reviewed_by ?? "管理员"} 于 {formatDate(finding.reviewed_at)} 更新</small>
      )}
    </article>
  );
}

function ReviewDetailPage({
  user,
  reviewRunId,
  onBack,
  onOpenReview,
  onSignedOut,
}: ReviewDetailPageProps) {
  const [details, setDetails] = useState<ReviewDetails | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionBusy, setActionBusy] = useState<ReviewAction | null>(null);
  const [findingBusy, setFindingBusy] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);

  const loadDetails = useCallback(async () => {
    try {
      const next = await api.reviewDetails(reviewRunId);
      setDetails(next);
      setError("");
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, [onSignedOut, reviewRunId]);

  useEffect(() => {
    setDetails(null);
    setLoading(true);
    void loadDetails();
    if (!autoRefresh) return undefined;
    const timer = window.setInterval(() => void loadDetails(), 2500);
    return () => window.clearInterval(timer);
  }, [autoRefresh, loadDetails]);

  const currentMessage = useMemo(
    () => (details ? phaseLabels[details.phase] ?? details.phase : "正在读取任务详情"),
    [details],
  );

  async function runAction(action: ReviewAction) {
    if (!details) return;
    if (action === "cancel" && !window.confirm("确定取消这个任务吗？")) return;
    setActionBusy(action);
    try {
      const result = await api.reviewAction(details.review_run_id, action, actionKey());
      if (action === "rerun" && result.review_run_id !== details.review_run_id) {
        onOpenReview(result.review_run_id);
      } else {
        await loadDetails();
      }
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
      } else {
        setError(errorMessage(reason));
      }
    } finally {
      setActionBusy(null);
    }
  }

  async function decideFinding(finding: ReviewFinding, decision: FindingDecision) {
    if (!details) return;
    setFindingBusy(finding.id);
    try {
      setDetails(
        await api.decideFinding(
          details.review_run_id,
          finding.id,
          decision,
          actionKey(),
        ),
      );
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
      } else {
        setError(errorMessage(reason));
      }
    } finally {
      setFindingBusy(null);
    }
  }

  if (loading && !details) {
    return (
      <main className="review-detail-shell">
        <div className="review-detail-loading">正在读取审查详情…</div>
      </main>
    );
  }

  if (!details) {
    return (
      <main className="review-detail-shell">
        <div className="review-detail-error-state">
          <strong>暂时无法读取这条任务</strong>
          <p>{error || "任务可能已被删除或服务暂时不可用"}</p>
          <div><button type="button" className="review-primary-btn" onClick={() => void loadDetails()}>重新读取</button><button type="button" className="review-quiet-btn" onClick={onBack}>返回列表</button></div>
        </div>
      </main>
    );
  }

  const hasActions = details.available_actions.length > 0;
  const latestBatchPlan = latestBatchPlanEvent(details.events);
  const modelProgressActive = details.execution_status === "running" && !details.model_review_completed_at && details.events.some(
    (event) => event.event_type === "review.model.batch_started",
  );
  const modelDisplayState = details.model_status === "succeeded"
    ? "成功"
    : details.execution_status === "failed" && latestBatchPlan
      ? "调用失败"
    : modelProgressActive
      ? "调用中"
      : latestBatchPlan
        ? "已分批"
        : details.model_status ?? "未调用";
  const modelDisplayName = details.model_name ?? payloadString(latestBatchPlan, "model");
  return (
    <div className="review-detail-shell">
      <header className="review-detail-navbar">
        <button type="button" className="review-back-btn" onClick={onBack} aria-label="返回任务列表" title="返回任务列表">←</button>
        <div className="review-detail-brand">
          <strong>审查任务详情</strong>
          <span>OpenReviewer / {details.repository}</span>
        </div>
        <div className="review-detail-nav-actions">
          <label className="review-live-toggle">
            <input
              id="review-auto-refresh"
              name="review-auto-refresh"
              type="checkbox"
              checked={autoRefresh}
              onChange={(event) => setAutoRefresh(event.target.checked)}
            />
            <span className="review-live-dot" />自动刷新
          </label>
          <button type="button" className="review-refresh-btn" onClick={() => void loadDetails()} disabled={loading} title="立即刷新详情">↻ <span>刷新</span></button>
          <span className="review-user-chip">{user.username}</span>
        </div>
      </header>

      <main className="review-detail-main">
        {error && <div className="review-inline-error" role="alert">{error}</div>}
        <section className={`review-hero review-hero-${details.phase}`}>
          <div className="review-hero-copy">
            <div className="review-hero-kicker"><span className="review-hero-pulse" />{details.repository} · PR #{details.pull_request_number}</div>
            <h1>{details.pr_title || `Pull Request #${details.pull_request_number}`}</h1>
            <p>{currentMessage}</p>
            <div className="review-hero-meta">
              <span><code>{shortSha(details.head_sha)}</code></span>
              <span>{details.changed_files_count ?? "—"} 个变更文件</span>
              <span>更新于 {formatDate(details.updated_at)}</span>
            </div>
          </div>
          <div className="review-hero-status">
            <span className="review-current-stage-label">当前节点</span>
            <strong>{stageLabels[details.current_stage] ?? details.current_stage}</strong>
            <span className="review-current-phase">{details.execution_status === "failed" ? "需要处理" : details.execution_status === "completed" ? "已完成" : "运行中"}</span>
          </div>
        </section>

        <section className="review-control-strip">
          <div className="review-control-summary">
            <span className="review-control-title">任务控制</span>
            <span className="review-control-hint">尝试 {details.attempt_count}/{details.max_attempts} · AI 阶段 {details.model_attempt_count}/{details.max_attempts}</span>
          </div>
          <div className="review-control-actions">
            {hasActions ? details.available_actions.map((action) => (
              <button
                key={action}
                type="button"
                className={`review-action-btn action-${action}`}
                disabled={actionBusy !== null}
                onClick={() => void runAction(action)}
              >
                <DetailIcon>{actionIcons[action]}</DetailIcon>{actionBusy === action ? "处理中…" : actionLabels[action]}
              </button>
            )) : <span className="review-no-actions">当前节点无需手动操作</span>}
          </div>
        </section>

        <div className="review-detail-grid">
          <div className="review-detail-primary">
            <StageTimeline details={details} />
            <ModelBatchPanel details={details} />

            <section className="review-panel review-result-panel">
              <div className="review-panel-heading">
                <div><span className="review-eyebrow">AI OUTPUT</span><h2>审查结果</h2></div>
                <div className="review-result-counts"><span className="result-count result-count-total">{details.findings.length} 条候选</span>{details.unverified_finding_count > 0 && <span className="result-count result-count-pending">{details.unverified_finding_count} 未标记</span>}</div>
              </div>
              {!details.model_review_completed_at && (
                <div className="review-result-empty"><DetailIcon>◌</DetailIcon><div><strong>AI 结果尚未生成</strong><p>模型完成后，候选问题会显示在这里。</p></div></div>
              )}
              {details.model_review_completed_at && details.findings.length === 0 && (
                <div className="review-result-empty result-empty-positive"><DetailIcon>✓</DetailIcon><div><strong>AI 没有返回候选问题</strong><p>这表示模型在当前审查范围内没有发现可报告的问题。</p></div></div>
              )}
              {details.findings.length > 0 && (
                <div className="review-findings-list">
                  {details.findings.map((finding) => <FindingCard key={finding.id} finding={finding} busy={findingBusy === finding.id} onDecision={decideFinding} />)}
                </div>
              )}
            </section>

            <section className="review-panel review-log-panel">
              <div className="review-panel-heading">
                <div><span className="review-eyebrow">EVENT LOG</span><h2>运行日志</h2></div>
                <span className="review-log-count">{details.events.length} 条事件</span>
              </div>
              {details.events.length === 0 ? <div className="review-empty-small">暂时没有结构化事件记录</div> : (
                <div className="review-event-list">
                  {details.events.map((event) => (
                    <div className="review-event-row" key={event.id}>
                      <span className="review-event-time">{formatDate(event.occurred_at)}</span>
                      <span className="review-event-line" />
                      <div className="review-event-copy"><strong>{eventLabels[event.event_type] ?? event.event_type}</strong><code>{event.event_type}</code>{eventDetail(event) && <small>{eventDetail(event)}</small>}{typeof event.payload.error_message === "string" && <p>{event.payload.error_message}</p>}{typeof event.payload.error_code === "string" && <small>错误码：{event.payload.error_code}{event.payload.error_retryable === true ? " · 可重试" : event.payload.error_retryable === false ? " · 不可重试" : ""}</small>}</div>
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>

          <aside className="review-detail-sidebar">
            <section className="review-panel review-summary-panel">
              <div className="review-panel-heading"><div><span className="review-eyebrow">RUN SUMMARY</span><h2>运行信息</h2></div></div>
              <dl className="review-summary-list">
                <div><dt>运行 ID</dt><dd><code title={details.review_run_id}>{details.review_run_id.slice(0, 12)}…</code></dd></div>
                <div><dt>任务 ID</dt><dd><code title={details.review_task_id}>{details.review_task_id.slice(0, 12)}…</code></dd></div>
                <div><dt>版本 SHA</dt><dd><code>{shortSha(details.head_sha)}</code></dd></div>
                <div><dt>覆盖状态</dt><dd>{details.coverage_status === "complete" ? "完整" : details.coverage_status === "partial" ? "部分" : details.coverage_status}</dd></div>
                <div><dt>创建时间</dt><dd>{formatDate(details.created_at)}</dd></div>
                <div><dt>可用时间</dt><dd>{formatDate(details.available_at)}</dd></div>
              </dl>
            </section>

            <section className="review-panel review-model-panel">
              <div className="review-panel-heading"><div><span className="review-eyebrow">MODEL CALL</span><h2>AI 调用</h2></div><span className={`review-model-state ${details.model_status === "succeeded" ? "is-good" : details.execution_status === "failed" && latestBatchPlan ? "is-error" : modelProgressActive ? "is-running" : latestBatchPlan ? "is-warning" : ""}`}>{modelDisplayState}</span></div>
              <dl className="review-metric-grid">
                <div><dt>模型</dt><dd>{modelDisplayName || "—"}</dd></div>
                <div><dt>供应商</dt><dd>{details.model_provider ?? payloadString(latestBatchPlan, "provider") ?? "—"}</dd></div>
                <div><dt>接口</dt><dd>{details.model_protocol ?? payloadString(latestBatchPlan, "api_protocol") ?? "—"}</dd></div>
                <div><dt>响应</dt><dd>{details.model_response_status ?? "—"}</dd></div>
                <div><dt>输入 Token</dt><dd>{details.model_input_tokens?.toLocaleString() ?? "—"}</dd></div>
                <div><dt>输出 Token</dt><dd>{details.model_output_tokens?.toLocaleString() ?? "—"}</dd></div>
                <div><dt>推理 Token</dt><dd>{details.model_reasoning_tokens?.toLocaleString() ?? "—"}</dd></div>
                <div><dt>耗时</dt><dd>{formatDuration(details.model_duration_ms)}</dd></div>
                <div><dt>候选问题</dt><dd>{details.model_finding_count ?? "—"}</dd></div>
              </dl>
              {details.model_cost_microusd !== null && details.model_cost_microusd !== undefined && <div className="review-cost-note">估算成本 ${(details.model_cost_microusd / 1_000_000).toFixed(4)}</div>}
            </section>

            <section className="review-panel review-context-panel">
              <div className="review-panel-heading"><div><span className="review-eyebrow">GITHUB CONTEXT</span><h2>代码与 CI</h2></div><span className={`review-ci-state ci-${details.ci_state ?? "unknown"}`}>{details.ci_state ?? "未知"}</span></div>
              <dl className="review-context-list">
                <div><dt>PR 状态</dt><dd>{details.pr_state ?? "—"}{details.pr_is_draft ? " · Draft" : ""}</dd></div>
                <div><dt>变更文件</dt><dd>{details.changed_files_count ?? "—"} 个</dd></div>
                <div><dt>文件快照</dt><dd>{details.files_complete === null ? "—" : details.files_complete ? "完整" : "部分"}</dd></div>
                <div><dt>Diff 快照</dt><dd>{details.diff_complete === null ? "—" : details.diff_complete ? "完整" : "部分"}</dd></div>
                <div><dt>CI 检查</dt><dd>{details.ci_checks.length} 项 · {details.ci_checks_complete ? "完整" : "持续刷新"}</dd></div>
              </dl>
              {details.ci_checks.length > 0 && <div className="review-ci-check-list">{details.ci_checks.slice(0, 8).map((check) => <div key={`${check.kind}:${check.name}`}><span className={`ci-check-dot ci-check-${check.conclusion ?? check.status}`} /><span>{check.name}</span><small>{check.conclusion ?? check.status}</small></div>)}</div>}
            </section>

            {details.review_plan_id && <section className="review-panel review-plan-panel"><div className="review-panel-heading"><div><span className="review-eyebrow">REVIEW COVERAGE</span><h2>文件覆盖</h2></div></div><div className="review-plan-stats"><div><strong>{details.plan_file_count ?? 0}</strong><span>变更文件</span></div><div><strong>{details.plan_unit_count ?? 0}</strong><span>送入 AI</span></div><div><strong>{details.plan_rule_count ?? 0}</strong><span>规则</span></div></div><div className="review-decision-list">{Object.entries(details.plan_file_decisions).map(([decision, count]) => <div key={decision}><span>{fileDecisionLabels[decision] ?? decision}</span><strong>{count}</strong></div>)}</div><div className="review-plan-bytes">可审查输入 {formatBytes(details.plan_input_bytes)} · 超长内容自动分批</div></section>}
          </aside>
        </div>
      </main>
    </div>
  );
}

export default ReviewDetailPage;
