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
  start: "开始审查",
  pause: "暂停",
  resume: "继续",
  retry_stage: "重试本阶段",
  approve: "批准审查",
  reject: "驳回",
  publish: "发布到 GitHub",
  expedite: "立即唤醒",
  retry: "重试本阶段",
  cancel: "取消任务",
  rerun: "重新审查",
};

type RetryTargetStage = "ci" | "planning" | "agent_batches" | "aggregating";

const retryTargetOptions: ReadonlyArray<[RetryTargetStage, string]> = [
  ["ci", "CI 检查"],
  ["planning", "审查规划"],
  ["agent_batches", "三路 Agent"],
  ["aggregating", "结果汇总"],
];

const actionIcons: Record<ReviewAction, string> = {
  start: "▶",
  pause: "Ⅱ",
  resume: "▶",
  retry_stage: "↻",
  approve: "✓",
  reject: "×",
  publish: "⇧",
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
  "review.model.request_started": "AI 请求已发出",
  "review.model.request_completed": "AI 响应已返回",
  "review.model.batch_completed": "AI 批次完成",
  "review.model.batch_failed": "AI 批次失败",
  "review.model.agent_completed": "审查 Agent 已完成",
  "review.model.agent_failed": "审查 Agent 失败",
  "review.model.aggregating_started": "开始汇总审查结果",
  "review.model.summary_completed": "汇总 Agent 已完成",
  "review.model.completed": "AI 分析完成",
  "review.model.batches_persisted": "批次已保存",
  "review.workflow.approve": "审查已批准，等待发布",
  "review.workflow.advance": "批准状态已记录，开放人工发布",
  "review.workflow.reject": "审查已驳回",
  "review.workflow.retry_stage": "从指定阶段重新审查",
  "review.workflow.pause": "审查已暂停",
  "review.workflow.resume": "审查已继续",
  "review.manual.publish_started": "开始人工发布",
  "review.manual.publish_completed": "已发布到 GitHub",
  "review.manual.publish_failed": "GitHub 发布失败",
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

const reasoningEffortLabels: Record<string, string> = {
  none: "跟随服务商",
  low: "轻量",
  medium: "标准",
  high: "深入",
  max: "极致",
};

function actionKey(action?: ReviewAction, reviewRunId?: string): string {
  if (action === "publish" && reviewRunId) {
    // 一次审查最多人工发布一次；稳定幂等键让网络超时或页面刷新后的
    // 再次点击能够恢复同一发布，而不是创建第二条 GitHub 评论。
    return `ui:publish:${reviewRunId}`;
  }
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
  if (value >= 60_000) {
    const seconds = Math.round(value / 1000);
    return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  }
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

function latestEvent(
  events: ReviewEvent[],
  eventType: string,
  modelAttempt?: number,
): ReviewEvent | undefined {
  return [...events].reverse().find((event) => (
    event.event_type === eventType
    && (modelAttempt === undefined
      || payloadNumber(event, "model_attempt_count") === modelAttempt)
  ));
}

function retryDetail(event: ReviewEvent | undefined): string | null {
  const retryAt = payloadString(event, "retry_at");
  if (!retryAt) return null;
  const remainingSeconds = Math.max(
    0,
    Math.ceil((new Date(retryAt).getTime() - Date.now()) / 1000),
  );
  if (!Number.isFinite(remainingSeconds)) return `计划重试时间 ${formatDate(retryAt)}`;
  if (remainingSeconds === 0) return `已到重试时间，等待 Worker 领取`;
  if (remainingSeconds < 60) return `${remainingSeconds} 秒后自动重试`;
  return `约 ${Math.ceil(remainingSeconds / 60)} 分钟后自动重试（${formatDate(retryAt)}）`;
}

function eventDetail(event: ReviewEvent): string | null {
  if (event.event_type === "review.model.batches_planned") {
    const batches = payloadNumber(event, "batch_count");
    const files = payloadNumber(event, "file_count");
    const context = payloadNumber(event, "context_window_tokens");
    const batchBudget = payloadNumber(event, "input_budget_tokens");
    const reasoning = payloadString(event, "reasoning_effort");
    return `${batches ?? "—"} 批 · ${files ?? "—"} 个文件 · 模型总容量 ${context?.toLocaleString() ?? "—"} · 单批约 ${batchBudget?.toLocaleString() ?? "—"} Token · 推理 ${reasoningEffortLabels[reasoning ?? ""] ?? reasoning ?? "—"}`;
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
  if (event.event_type === "review.model.request_started") {
    const number = payloadNumber(event, "batch_number");
    const total = payloadNumber(event, "batch_count");
    const model = payloadString(event, "model");
    const protocol = payloadString(event, "api_protocol");
    return `第 ${number ?? "—"}/${total ?? "—"} 批已发送 · ${model ?? "—"} · ${protocol ?? "—"}`;
  }
  if (event.event_type === "review.model.request_completed") {
    const number = payloadNumber(event, "batch_number");
    const total = payloadNumber(event, "batch_count");
    const status = payloadNumber(event, "response_status");
    const requestId = payloadString(event, "provider_request_id");
    return `第 ${number ?? "—"}/${total ?? "—"} 批已返回 · HTTP ${status ?? "—"} · ${formatDuration(payloadNumber(event, "duration_ms"))}${requestId ? ` · 请求 ID ${requestId}` : ""}`;
  }
  if (event.event_type === "review.model.batch_failed") {
    const number = payloadNumber(event, "batch_number");
    const total = payloadNumber(event, "batch_count");
    const status = payloadNumber(event, "status_code");
    const code = payloadString(event, "error_code");
    return `第 ${number ?? "—"}/${total ?? "—"} 批失败 · HTTP ${status ?? "—"} · ${formatDuration(payloadNumber(event, "duration_ms"))} · 错误码 ${code ?? "—"}${event.payload.error_retryable === true ? " · 可自动重试" : event.payload.error_retryable === false ? " · 不可自动重试" : ""}`;
  }
  if (event.event_type === "review.task.retry_scheduled") {
    return retryDetail(event);
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

const agentDefinitions = [
  { key: "security", label: "安全审查", description: "检查漏洞、权限和敏感数据风险" },
  { key: "convention", label: "规范审查", description: "检查编码规范、可维护性和工程约定" },
  { key: "logic", label: "逻辑审查", description: "检查业务逻辑、边界条件和回归风险" },
  { key: "summary", label: "汇总 Agent", description: "去重、排序并生成最终审查结论" },
] as const;

type ReviewAgentKey = (typeof agentDefinitions)[number]["key"];

function eventAgent(event: ReviewEvent): string | null {
  const value = event.payload.agent;
  return typeof value === "string" ? value : null;
}

function latestAgentEvents(events: ReviewEvent[], agent: ReviewAgentKey): ReviewEvent[] {
  const matching = events.filter((event) => (
    event.event_type.startsWith("review.model.") && eventAgent(event) === agent
  ));
  if (matching.length === 0) return [];
  const attempts = matching
    .map((event) => payloadNumber(event, "model_attempt_count"))
    .filter((value): value is number => value !== null);
  const latestAttempt = attempts.length > 0 ? Math.max(...attempts) : null;
  return matching.filter((event) => (
    latestAttempt === null
      || payloadNumber(event, "model_attempt_count") === null
      || payloadNumber(event, "model_attempt_count") === latestAttempt
  ));
}

function latestAgentEvent(events: ReviewEvent[], eventType: string): ReviewEvent | undefined {
  return [...events].reverse().find((event) => event.event_type === eventType);
}

function latestBatchEvents(events: ReviewEvent[]): Map<number, ReviewEvent> {
  const lifecycle = new Set([
    "review.model.batch_started",
    "review.model.request_started",
    "review.model.request_completed",
    "review.model.batch_completed",
    "review.model.batch_failed",
  ]);
  const lifecycleRank: Record<string, number> = {
    "review.model.batch_started": 1,
    "review.model.request_started": 2,
    "review.model.request_completed": 3,
    "review.model.batch_completed": 4,
    "review.model.batch_failed": 4,
  };
  const result = new Map<number, ReviewEvent>();
  for (const event of events) {
    if (!lifecycle.has(event.event_type)) continue;
    const number = payloadNumber(event, "batch_number");
    if (number === null) continue;
    const current = result.get(number);
    if (!current || lifecycleRank[event.event_type] > lifecycleRank[current.event_type]) {
      result.set(number, event);
    }
  }
  return result;
}

function workflowReadout(details: ReviewDetails, retryPending: boolean): string {
  if (retryPending) return "等待自动重试";
  if (details.phase === "rejected") return "已驳回";
  if (details.phase === "paused") return "已暂停";
  if (details.phase === "awaiting_approval" || details.phase === "awaiting_publish") return "等待人工操作";
  if (details.phase === "publishing") return "发布中";
  if (details.phase === "completed") return "已完成";
  if (details.phase.endsWith("failed") || details.phase === "ci_timed_out") return "需要处理";
  if (details.phase === "cancelled" || details.phase === "superseded") return "已结束";
  return "运行中";
}

function numericPayloadSum(events: ReviewEvent[], key: string): number | null {
  const values = events
    .map((event) => payloadNumber(event, key))
    .filter((value): value is number => value !== null);
  return values.length > 0 ? values.reduce((sum, value) => sum + value, 0) : null;
}

function stringArrayPayload(event: ReviewEvent | undefined, key: string): string[] {
  const value = event?.payload[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function agentProgress(events: ReviewEvent[], agent: ReviewAgentKey) {
  const scoped = latestAgentEvents(events, agent);
  const planned = latestAgentEvent(scoped, "review.model.batches_planned");
  const completedEvent = latestAgentEvent(scoped, "review.model.agent_completed");
  const failedEvent = latestAgentEvent(scoped, "review.model.agent_failed");
  const summaryEvent = latestAgentEvent(scoped, "review.model.summary_completed");
  const batches = latestBatchEvents(scoped);
  const batchCount = payloadNumber(planned, "batch_count")
    ?? Math.max(0, ...batches.keys());
  const completedBatches = [...batches.values()].filter(
    (event) => event.event_type === "review.model.batch_completed",
  );
  const failedBatches = [...batches.values()].filter(
    (event) => event.event_type === "review.model.batch_failed",
  );
  const lastTerminal = [...scoped].reverse().find((event) => (
    event.event_type === "review.model.agent_completed"
      || event.event_type === "review.model.agent_failed"
      || event.event_type === "review.model.summary_completed"
  ));
  const terminalIsSuccess = lastTerminal?.event_type === "review.model.agent_completed"
    || (lastTerminal?.event_type === "review.model.summary_completed"
      && lastTerminal.payload.agent_status === "completed");
  const terminalIsFailure = lastTerminal?.event_type === "review.model.agent_failed"
    || (lastTerminal?.event_type === "review.model.summary_completed"
      && lastTerminal.payload.agent_status !== "completed");
  const status = terminalIsSuccess
    ? "completed"
    : terminalIsFailure
      ? "failed"
      : failedBatches.length > 0
        ? "failed"
        : scoped.some((event) => event.event_type === "review.model.request_started")
          ? "running"
          : planned
            ? "planned"
            : "waiting";
  const terminal = terminalIsSuccess || terminalIsFailure ? lastTerminal : undefined;
  const findingCount = payloadNumber(terminal, "finding_count")
    ?? (agent === "summary" ? payloadNumber(summaryEvent, "finding_count") : null)
    ?? numericPayloadSum(completedBatches, "finding_count")
    ?? 0;
  const duration = payloadNumber(terminal, "duration_ms")
    ?? numericPayloadSum(completedBatches, "duration_ms");
  const inputTokens = numericPayloadSum(completedBatches, "input_tokens");
  const outputTokens = numericPayloadSum(completedBatches, "output_tokens");
  const reasoningTokens = numericPayloadSum(completedBatches, "reasoning_tokens");
  const requestIds = [...new Set(
    scoped
      .map((event) => payloadString(event, "provider_request_id"))
      .filter((value): value is string => Boolean(value)),
  )];
  const errorEvent = [...scoped].reverse().find((event) => (
    event.event_type === "review.model.batch_failed"
      || event.event_type === "review.model.agent_failed"
      || (event.event_type === "review.model.summary_completed" && terminalIsFailure)
  ));
  const errorCode = payloadString(errorEvent, "error_code")
    ?? payloadString(errorEvent, "code");
  const errorMessage = payloadString(errorEvent, "error_message")
    ?? payloadString(errorEvent, "error")
    ?? (terminalIsFailure ? "该 Agent 未返回可用结果" : null);
  const references = [...new Set([
    ...stringArrayPayload(completedEvent, "references"),
    ...stringArrayPayload(failedEvent, "references"),
    ...stringArrayPayload(summaryEvent, "references"),
  ])];
  return {
    events: scoped,
    planned,
    batches,
    batchCount,
    completedBatches,
    failedBatches,
    status,
    findingCount,
    duration,
    inputTokens,
    outputTokens,
    reasoningTokens,
    requestIds,
    errorCode,
    errorMessage,
    references,
  };
}

function ModelBatchPanel({ details }: { details: ReviewDetails }) {
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
            : progress.status === "failed"
              ? "失败"
              : progress.status === "running"
                ? "进行中"
                : progress.status === "planned"
                  ? "已规划"
                  : "等待开始";
          return (
            <article className={`review-agent-card is-${progress.status}`} key={key}>
              <header className="review-agent-card-header">
                <div><strong>{label}</strong><span>{description}</span></div>
                <b>{statusLabel}</b>
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
              {progress.status === "completed" && progress.findingCount === 0 && (
                <p className="review-agent-empty"><span aria-hidden="true">✓</span>该 Agent 已完成，未发现问题</p>
              )}
              {progress.errorMessage && progress.status === "failed" && (
                <div className="review-agent-error">
                  <strong>{progress.errorCode ? `错误码 ${progress.errorCode}` : "Agent 执行失败"}</strong>
                  <p>{progress.errorMessage}</p>
                </div>
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
  const [retryTargetStage, setRetryTargetStage] = useState<RetryTargetStage>("agent_batches");

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
    if (action === "approve" && !window.confirm("批准后才会开放人工 GitHub 发布，继续吗？")) return;
    if (action === "reject" && !window.confirm("确定驳回本次审查结果吗？")) return;
    if (action === "retry_stage" && !window.confirm("将清除所选阶段及之后的结果，并从该阶段重新审查。继续吗？")) return;
    if (action === "publish" && !window.confirm("确定把已批准结果人工发布到 GitHub 吗？")) return;
    setActionBusy(action);
    try {
      const result = await api.reviewAction(
        details.review_run_id,
        action,
        actionKey(action, details.review_run_id),
        action === "retry_stage" ? retryTargetStage : undefined,
      );
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
  const currentModelFailure = latestEvent(
    details.events,
    "review.model.batch_failed",
    details.model_attempt_count,
  );
  const currentRetryEvent = latestEvent(
    details.events,
    "review.task.retry_scheduled",
    details.model_attempt_count,
  );
  const latestRequestLifecycleEvent = [...details.events].reverse().find((event) => (
    payloadNumber(event, "model_attempt_count") === details.model_attempt_count
    && [
      "review.model.request_started",
      "review.model.request_completed",
      "review.model.batch_failed",
    ].includes(event.event_type)
  ));
  const retryPending = Boolean(
    currentRetryEvent
    && details.execution_status === "ready_for_review",
  );
  const retryStatus = retryDetail(currentRetryEvent);
  const requestInFlight = latestRequestLifecycleEvent?.event_type
    === "review.model.request_started";
  const modelProgressActive = details.execution_status === "running" && !details.model_review_completed_at && details.events.some(
    (event) => event.event_type === "review.model.batch_started",
  );
  const modelDisplayState = details.model_status === "succeeded"
    ? "成功"
    : retryPending
      ? "等待重试"
      : currentModelFailure
      ? "调用失败"
      : requestInFlight
        ? "请求中"
        : modelProgressActive
          ? "处理中"
          : latestBatchPlan
            ? "已规划"
            : details.model_status ?? "未调用";
  const modelStateClass = details.model_status === "succeeded"
    ? "is-good"
    : retryPending
        ? "is-warning"
        : currentModelFailure
          ? "is-error"
          : requestInFlight || modelProgressActive
            ? "is-running"
            : latestBatchPlan
              ? "is-warning"
              : "";
  const modelDisplayName = details.model_name ?? payloadString(latestBatchPlan, "model");
  const displayMessage = retryPending
    ? `上一轮 AI 请求失败，${retryStatus ?? "系统已安排自动重试"}`
    : currentMessage;
  const failureStatus = payloadNumber(currentModelFailure, "status_code");
  const failureDuration = payloadNumber(currentModelFailure, "duration_ms");
  const failureCode = payloadString(currentModelFailure, "error_code");
  const failureRequestId = payloadString(currentModelFailure, "provider_request_id");
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
            <p>{displayMessage}</p>
            <div className="review-hero-meta">
              <span><code>{shortSha(details.head_sha)}</code></span>
              <span>{details.changed_files_count ?? "—"} 个变更文件</span>
              <span>更新于 {formatDate(details.updated_at)}</span>
            </div>
          </div>
          <div className="review-hero-status">
            <span className="review-current-stage-label">当前节点</span>
            <strong>{stageLabels[details.current_stage] ?? details.current_stage}</strong>
            <span className="review-current-phase">{workflowReadout(details, retryPending)}</span>
          </div>
        </section>

        <section className="review-control-strip">
          <div className="review-control-summary">
            <span className="review-control-title">任务控制</span>
            <span className={`review-control-hint ${retryPending ? "is-retry" : ""}`}>{retryPending ? retryStatus : `尝试 ${details.attempt_count}/${details.max_attempts} · AI 阶段 ${details.model_attempt_count}/${details.max_attempts}`}</span>
          </div>
          <div className="review-control-actions">
            {details.available_actions.includes("retry_stage") && (
              <label className="review-retry-target">
                <span>重审起点</span>
                <select
                  value={retryTargetStage}
                  disabled={actionBusy !== null}
                  onChange={(event) => setRetryTargetStage(event.target.value as RetryTargetStage)}
                >
                  {retryTargetOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </label>
            )}
            {hasActions ? details.available_actions.map((action) => (
              <button
                key={action}
                type="button"
                className={`review-action-btn action-${action}`}
                disabled={actionBusy !== null}
                onClick={() => void runAction(action)}
              >
                <DetailIcon>{actionIcons[action]}</DetailIcon>{actionBusy === action ? "处理中…" : action === "expedite" && retryPending ? "立即重试" : action === "retry_stage" && details.workflow_status === "rejected" ? "从所选阶段重审" : actionLabels[action]}
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
              {!details.model_review_completed_at && (currentModelFailure || retryPending) && (
                <div className="review-result-empty result-empty-error"><DetailIcon>!</DetailIcon><div><strong>{retryPending ? "AI 请求失败，已安排自动重试" : "AI 请求失败"}</strong><p>{payloadString(currentModelFailure, "error_message") ?? details.last_error ?? "模型服务未返回可用结果"}</p><small>HTTP {failureStatus ?? "—"} · {formatDuration(failureDuration)} · 错误码 {failureCode ?? "—"}{retryStatus ? ` · ${retryStatus}` : ""}</small></div></div>
              )}
              {!details.model_review_completed_at && !currentModelFailure && !retryPending && (
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
                    <div className={`review-event-row ${event.event_type === "review.model.batch_failed" || event.event_type === "review.task.failed" ? "is-error" : ""}`} key={event.id}>
                      <span className="review-event-time">{formatDate(event.occurred_at)}</span>
                      <span className="review-event-line" />
                      <div className="review-event-copy"><strong>{eventLabels[event.event_type] ?? event.event_type}</strong><code>{event.event_type}</code>{eventDetail(event) && <small>{eventDetail(event)}</small>}{typeof event.payload.error_message === "string" && <p>{event.payload.error_message}</p>}{event.event_type !== "review.model.batch_failed" && typeof event.payload.error_code === "string" && <small>错误码：{event.payload.error_code}{event.payload.error_retryable === true ? " · 可重试" : event.payload.error_retryable === false ? " · 不可重试" : ""}</small>}</div>
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
                <div><dt>{retryPending ? "自动重试" : "可用时间"}</dt><dd>{retryPending ? retryStatus ?? formatDate(details.available_at) : formatDate(details.available_at)}</dd></div>
              </dl>
            </section>

            <section className="review-panel review-model-panel">
              <div className="review-panel-heading"><div><span className="review-eyebrow">MODEL CALL</span><h2>AI 调用</h2></div><span className={`review-model-state ${modelStateClass}`}>{modelDisplayState}</span></div>
              <dl className="review-metric-grid">
                <div><dt>模型</dt><dd>{modelDisplayName || "—"}</dd></div>
                <div><dt>供应商</dt><dd>{details.model_provider ?? payloadString(latestBatchPlan, "provider") ?? "—"}</dd></div>
                <div><dt>接口</dt><dd>{details.model_protocol ?? payloadString(latestBatchPlan, "api_protocol") ?? "—"}</dd></div>
                <div><dt>推理档位</dt><dd>{reasoningEffortLabels[payloadString(latestBatchPlan, "reasoning_effort") ?? ""] ?? payloadString(latestBatchPlan, "reasoning_effort") ?? "—"}</dd></div>
                <div><dt>单批预算</dt><dd>{payloadNumber(latestBatchPlan, "input_budget_tokens")?.toLocaleString() ?? "—"} Token</dd></div>
                <div><dt>响应</dt><dd>{details.model_response_status ?? failureStatus ?? "—"}</dd></div>
                <div><dt>输入 Token</dt><dd>{details.model_input_tokens?.toLocaleString() ?? "—"}</dd></div>
                <div><dt>输出 Token</dt><dd>{details.model_output_tokens?.toLocaleString() ?? "—"}</dd></div>
                <div><dt>推理 Token</dt><dd>{details.model_reasoning_tokens?.toLocaleString() ?? "—"}</dd></div>
                <div><dt>耗时</dt><dd>{formatDuration(details.model_duration_ms ?? failureDuration)}</dd></div>
                <div><dt>候选问题</dt><dd>{details.model_finding_count ?? "—"}</dd></div>
                {failureCode && <div><dt>错误码</dt><dd>{failureCode}</dd></div>}
                {failureRequestId && <div><dt>请求 ID</dt><dd><code title={failureRequestId}>{failureRequestId}</code></dd></div>}
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
