import type {
  ReviewAction,
  ReviewDetails,
  ReviewEvent,
  ReviewFinding,
} from "./types";
import { formatDate } from "./utils";

export type ReviewVerdict =
  | "issues_found"
  | "no_actionable_issue"
  | "insufficient_context";

export const reasoningEffortLabels: Record<string, string> = {
  none: "跟随服务商",
  low: "轻量",
  medium: "标准",
  high: "深入",
  max: "极致",
};

export const verdictLabels: Record<ReviewVerdict, string> = {
  issues_found: "发现需处理问题",
  no_actionable_issue: "当前范围未发现可报告问题",
  insufficient_context: "审查上下文不足",
};

export const agentDefinitions = [
  { key: "security", label: "安全审查", description: "检查漏洞、权限和敏感数据风险" },
  { key: "convention", label: "规范审查", description: "检查编码规范、可维护性和工程约定" },
  { key: "logic", label: "逻辑审查", description: "检查业务逻辑、边界条件和回归风险" },
  { key: "summary", label: "汇总 Agent", description: "去重、排序并生成最终审查结论" },
] as const;

export type ReviewAgentKey = (typeof agentDefinitions)[number]["key"];

type ActionKeyTarget = {
  agent?: string | null;
  batchNumber?: number | null;
  stateVersion?: string | null;
  captureModelOutputs?: boolean;
  reviewProfileId?: string;
};

// 同一页面状态下的网络重试必须复用一个幂等键；状态版本或失败批次变化
// 时则生成新的键，避免把两次不同的人工操作误当成同一次。只保留有限条
// 记录，防止长时间打开详情页造成内存增长。
const pendingActionKeys = new Map<string, string>();
const MAX_PENDING_ACTION_KEYS = 128;

export function actionKey(
  action?: ReviewAction,
  reviewRunId?: string,
  target?: ActionKeyTarget,
): string {
  if (action === "publish" && reviewRunId) {
    return `ui:publish:${reviewRunId}`;
  }
  if (!action || !reviewRunId) {
    return `ui:${Date.now()}:${crypto.randomUUID()}`;
  }
  const identity = [
    action,
    reviewRunId,
    target?.agent ?? "",
    target?.batchNumber == null ? "" : String(target.batchNumber),
    target?.stateVersion ?? "",
    String(target?.captureModelOutputs ?? false),
    target?.reviewProfileId ?? "",
  ].join(":");
  const existing = pendingActionKeys.get(identity);
  if (existing) return existing;
  const value = `ui:${crypto.randomUUID()}`;
  pendingActionKeys.set(identity, value);
  if (pendingActionKeys.size > MAX_PENDING_ACTION_KEYS) {
    const oldest = pendingActionKeys.keys().next().value;
    if (oldest) pendingActionKeys.delete(oldest);
  }
  return value;
}

export function formatBytes(value: number | null): string {
  if (value === null || value === undefined) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(2)} MiB`;
}

export function formatDuration(value: number | null): string {
  if (value === null || value === undefined) return "—";
  if (value < 1000) return `${value} ms`;
  if (value >= 60_000) {
    const seconds = Math.round(value / 1000);
    return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  }
  return `${(value / 1000).toFixed(1)} s`;
}

export function payloadNumber(
  event: ReviewEvent | undefined,
  key: string,
): number | null {
  const value = event?.payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function payloadString(
  event: ReviewEvent | undefined,
  key: string,
): string | null {
  const value = event?.payload[key];
  return typeof value === "string" ? value : null;
}

export function payloadVerdict(event: ReviewEvent | undefined): ReviewVerdict | null {
  const value = payloadString(event, "verdict");
  return value === "issues_found"
    || value === "no_actionable_issue"
    || value === "insufficient_context"
    ? value
    : null;
}

export function branchLabel(repository: string | null, ref: string | null): string {
  return `${repository ?? "未知仓库"}:${ref ?? "未知分支"}`;
}

export function currentReviewEvents(events: ReviewEvent[]): ReviewEvent[] {
  const resetAt = Math.max(0, ...events
    .filter((event) => event.event_type === "review.manual.retry")
    .map((event) => Date.parse(event.occurred_at)));
  return resetAt === 0 ? events : events.filter((event) => Date.parse(event.occurred_at) >= resetAt);
}

export function latestBatchPlanEvent(events: ReviewEvent[]): ReviewEvent | undefined {
  return currentReviewEvents(events)
    .filter((event) => event.event_type === "review.model.batches_planned")
    .reduce<ReviewEvent | undefined>((latest, event) => {
      if (!latest) return event;
      return (payloadNumber(event, "model_attempt_count") ?? -1)
        > (payloadNumber(latest, "model_attempt_count") ?? -1)
        ? event
        : latest;
    }, undefined);
}

export function latestEvent(
  events: ReviewEvent[],
  eventType: string,
  modelAttempt?: number,
): ReviewEvent | undefined {
  const current = modelAttempt === undefined ? events : currentReviewEvents(events);
  return [...current].reverse().find((event) => (
    event.event_type === eventType
    && (modelAttempt === undefined
      || payloadNumber(event, "model_attempt_count") === modelAttempt)
  ));
}

export function retryDetail(
  event: ReviewEvent | undefined,
  now = Date.now(),
): string | null {
  const retryAt = payloadString(event, "retry_at");
  if (!retryAt) return null;
  const remainingSeconds = Math.max(
    0,
    Math.ceil((new Date(retryAt).getTime() - now) / 1000),
  );
  if (!Number.isFinite(remainingSeconds)) return `计划重试时间 ${formatDate(retryAt)}`;
  if (remainingSeconds === 0) return "已到重试时间，等待 Worker 领取";
  if (remainingSeconds < 60) return `${remainingSeconds} 秒后自动重试`;
  return `约 ${Math.ceil(remainingSeconds / 60)} 分钟后自动重试（${formatDate(retryAt)}）`;
}

export function eventDetail(event: ReviewEvent): string | null {
  if (event.event_type === "review.model.batches_planned") {
    const batches = payloadNumber(event, "batch_count");
    const files = payloadNumber(event, "file_count");
    const reasoning = payloadString(event, "reasoning_effort");
    const selected = payloadNumber(event, "context_selected_count");
    const candidates = payloadNumber(event, "context_candidate_count");
    const context = selected !== null && candidates !== null
      ? ` · 参考片段 ${selected}/${candidates}${selected < candidates ? "（按批次适用范围与模型容量筛选）" : ""}` : "";
    return `${batches ?? "—"} 批 · ${files ?? "—"} 个文件 · 系统将自动管理上下文与分批 · 推理 ${reasoningEffortLabels[reasoning ?? ""] ?? reasoning ?? "—"}${context}`;
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
    const unsupported = Array.isArray(event.payload.unsupported_parameters)
      ? event.payload.unsupported_parameters.filter(
        (item): item is string => typeof item === "string",
      )
      : [];
    const unsupportedDetail = unsupported.length > 0
      ? ` · 中转站不支持：${unsupported.join("、")}`
      : "";
    return `第 ${number ?? "—"}/${total ?? "—"} 批失败 · HTTP ${status ?? "—"} · ${formatDuration(payloadNumber(event, "duration_ms"))} · 错误码 ${code ?? "—"}${unsupportedDetail}${event.payload.error_retryable === true ? " · 可自动重试" : event.payload.error_retryable === false ? " · 不可自动重试" : ""}`;
  }
  if (event.event_type === "review.task.retry_scheduled") {
    return retryDetail(event);
  }
  return null;
}

export function isErrorEvent(event: ReviewEvent): boolean {
  return event.event_type.endsWith("failed")
    || event.event_type === "review.task.failed"
    || typeof event.payload.error_code === "string"
    || typeof event.payload.error_message === "string";
}

function eventAgent(event: ReviewEvent): string | null {
  const value = event.payload.agent;
  return typeof value === "string" ? value : null;
}

function latestAgentEvents(
  events: ReviewEvent[],
  agent: ReviewAgentKey,
): ReviewEvent[] {
  const matching = currentReviewEvents(events).filter((event) => (
    event.event_type.startsWith("review.model.") && eventAgent(event) === agent
  ));
  if (matching.length === 0) return [];
  const attempts = matching
    .map((event) => payloadNumber(event, "model_attempt_count"))
    .filter((value): value is number => value !== null);
  const latestAttempt = attempts.length > 0 ? Math.max(...attempts) : null;
  return matching.filter((event) => (
    latestAttempt === null
      || payloadNumber(event, "model_attempt_count") === latestAttempt
  ));
}

function latestAgentEvent(
  events: ReviewEvent[],
  eventType: string,
): ReviewEvent | undefined {
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
    // 同一批次可能先收到失败事件、随后收到迟到的完成事件（例如重试/并发
    // 写入后的事件排序）。生命周期等级相等时也要让后出现的事件覆盖旧值，
    // 否则 UI 会把已经完成的批次永久显示为失败。
    if (!current || lifecycleRank[event.event_type] >= lifecycleRank[current.event_type]) {
      result.set(number, event);
    }
  }
  return result;
}

export function workflowReadout(
  details: ReviewDetails,
  retryPending: boolean,
): string {
  if (details.phase === "rejected") return "已驳回";
  if (details.phase === "paused") return "已暂停";
  if (details.phase === "cancelled") return "已取消";
  if (details.phase === "superseded") return "已被新提交替代";
  if (details.phase === "completed") return "已完成";
  if (details.phase === "coverage_incomplete") return "覆盖待补齐";
  if (details.phase === "awaiting_coverage_confirmation") return "待确认审查范围";
  if (details.phase === "awaiting_finding_adjudication") return "待核对问题";
  if (details.phase === "awaiting_approval" || details.phase === "awaiting_publish") {
    return "等待人工操作";
  }
  if (details.phase === "approved") return "已批准，等待发布";
  if (retryPending || details.phase === "model_retry_waiting") return "等待自动重试";
  if (["queued", "planning_queued", "model_queued"].includes(details.phase)) return "排队中";
  if (details.phase === "waiting_ci") return "等待 CI";
  if (details.phase === "publishing") return "发布中";
  if (details.phase.endsWith("failed") || details.phase === "ci_timed_out") {
    return "需要处理";
  }
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
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

export function agentProgress(events: ReviewEvent[], agent: ReviewAgentKey, summary?: import("./types").BatchProgress) {
  const scoped = latestAgentEvents(events, agent);
  const planned = latestAgentEvent(scoped, "review.model.batches_planned");
  const completedEvent = latestAgentEvent(scoped, "review.model.agent_completed");
  const failedEvent = latestAgentEvent(scoped, "review.model.agent_failed");
  const summaryEvent = latestAgentEvent(scoped, "review.model.summary_completed");
  const summaryFailedEvent = latestAgentEvent(scoped, "review.model.summary_failed");
  const summarySkippedEvent = latestAgentEvent(scoped, "review.model.summary_skipped");
  const notApplicableEvent = latestAgentEvent(
    scoped,
    "review.model.agent_not_applicable",
  );
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
      || event.event_type === "review.model.agent_not_applicable"
      || event.event_type === "review.model.summary_completed"
      || event.event_type === "review.model.summary_failed"
      || event.event_type === "review.model.summary_skipped"
  ));
  const terminalIsSuccess = lastTerminal?.event_type === "review.model.agent_completed"
    && lastTerminal.payload.status !== "not_applicable"
    || (lastTerminal?.event_type === "review.model.summary_completed"
      && lastTerminal.payload.agent_status === "completed");
  const terminalIsDisabled = lastTerminal?.event_type === "review.model.agent_failed"
    && lastTerminal.payload.status === "disabled";
  const terminalIsNotApplicable = lastTerminal?.event_type
    === "review.model.agent_not_applicable"
    || (lastTerminal?.event_type === "review.model.agent_completed"
      && lastTerminal.payload.status === "not_applicable");
  const terminalIsFailure = (
    (lastTerminal?.event_type === "review.model.agent_failed" && !terminalIsDisabled)
    || lastTerminal?.event_type === "review.model.summary_failed"
    || (lastTerminal?.event_type === "review.model.summary_completed"
      && lastTerminal.payload.agent_status !== "completed")
  );
  const terminalIsSkipped = lastTerminal?.event_type === "review.model.summary_skipped";
  const status = terminalIsSuccess
    ? "completed"
    : terminalIsDisabled
      ? "disabled"
      : terminalIsNotApplicable
        ? "not_applicable"
      : terminalIsFailure
        ? "failed"
        : terminalIsSkipped
          ? "not_executed"
          : failedBatches.length > 0
            ? "failed"
            : scoped.some((event) => event.event_type === "review.model.request_started")
              ? "running"
              : planned
                ? "planned"
                : scoped.some(event => event.event_type === "review.model.agent_started")
                  ? "preparing" : "waiting";
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
  const latestBatchEventSet = new Set(batches.values());
  const errorEvent = [...scoped].reverse().find((event) => (
    (event.event_type === "review.model.batch_failed" && latestBatchEventSet.has(event))
      || event.event_type === "review.model.agent_failed"
      || ((event.event_type === "review.model.summary_completed"
        || event.event_type === "review.model.summary_failed") && terminalIsFailure)
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
    ...stringArrayPayload(summaryFailedEvent, "references"),
    ...stringArrayPayload(summarySkippedEvent, "references"),
    ...stringArrayPayload(notApplicableEvent, "references"),
  ])];
  const verdict = payloadVerdict(terminal);
  const conclusionSummary = payloadString(terminal, "summary");
  const checkedAreas = stringArrayPayload(terminal, "checked_areas");
  return {
    events: scoped,
    planned,
    batches,
    batchCount: summary?.total ?? batchCount,
    completedCount: summary?.completed ?? completedBatches.length,
    failedCount: summary?.failed ?? failedBatches.length,
    completedBatches,
    failedBatches,
    status: summary && summary.failed > 0 ? "failed"
      : summary && summary.running > 0 ? "running"
      : summary && summary.completed === summary.total && status !== "failed" ? "completed" : status,
    findingCount,
    duration: summary?.duration_ms ?? duration,
    inputTokens: summary?.input_tokens ?? inputTokens,
    outputTokens: summary?.output_tokens ?? outputTokens,
    reasoningTokens: summary?.reasoning_tokens ?? reasoningTokens,
    requestIds,
    errorCode,
    errorMessage,
    references,
    verdict,
    conclusionSummary,
    checkedAreas,
    hasStructuredConclusion: verdict !== null && conclusionSummary !== null,
  };
}

function uniqueFindings(groups: readonly (readonly ReviewFinding[])[]): ReviewFinding[] {
  const seen = new Set<string>();
  const merged: ReviewFinding[] = [];
  for (const group of groups) {
    for (const finding of group) {
      if (seen.has(finding.id)) continue;
      seen.add(finding.id);
      merged.push(finding);
    }
  }
  return merged;
}

export function applyRefreshedFindingPage(
  current: ReviewDetails | null,
  incoming: ReviewDetails,
): ReviewDetails {
  if (!current || current.review_run_id !== incoming.review_run_id) return incoming;
  // 自动轮询、手动刷新和操作回读可能乱序返回。服务端 updated_at 是
  // 单调的状态版本；较旧整体快照不能把新状态回填，只能继续保留当前页。
  const currentUpdatedAt = Date.parse(current.updated_at);
  const incomingUpdatedAt = Date.parse(incoming.updated_at);
  if (
    Number.isFinite(currentUpdatedAt)
    && Number.isFinite(incomingUpdatedAt)
    && incomingUpdatedAt < currentUpdatedAt
  ) {
    return current;
  }
  const findings = uniqueFindings([incoming.findings, current.findings]);
  return {
    ...incoming,
    findings,
    finding_next_cursor:
      findings.length >= incoming.finding_total_count
        ? null
        : current.finding_next_cursor ?? incoming.finding_next_cursor,
  };
}

export function appendFindingPage(
  current: ReviewDetails,
  incoming: ReviewDetails,
): ReviewDetails {
  if (current.review_run_id !== incoming.review_run_id) return incoming;
  const currentUpdatedAt = Date.parse(current.updated_at);
  const incomingUpdatedAt = Date.parse(incoming.updated_at);
  const incomingIsOlder =
    Number.isFinite(currentUpdatedAt)
    && Number.isFinite(incomingUpdatedAt)
    && incomingUpdatedAt < currentUpdatedAt;
  return {
    ...(incomingIsOlder ? current : incoming),
    findings: uniqueFindings([current.findings, incoming.findings]),
    finding_next_cursor: incoming.finding_next_cursor,
  };
}
