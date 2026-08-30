import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, ApiError } from "./api";
import { allowedReviewActions, hasPermission, roleLabels } from "./rbac";
import FindingCard from "./ReviewFindingCard";
import {
  DetailIcon,
  ModelBatchPanel,
  StageTimeline,
} from "./ReviewProgressPanels";
import {
  actionKey,
  agentDefinitions,
  agentProgress,
  appendFindingPage,
  applyRefreshedFindingPage,
  branchLabel,
  eventDetail,
  formatBytes,
  formatDuration,
  isErrorEvent,
  latestBatchPlanEvent,
  latestEvent,
  payloadNumber,
  payloadString,
  reasoningEffortLabels,
  retryDetail,
  verdictLabels,
  workflowReadout,
} from "./review-details";
import type {
  AuthUser,
  FindingDecision,
  ReviewAction,
  ReviewDetails,
  ReviewFinding,
} from "./types";
import {
  errorMessage,
  formatDate,
  phaseLabels,
  shortSha,
  stageLabels,
} from "./utils";
import { useReviewAutoRefresh } from "./useReviewAutoRefresh";

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
type ReviewDetailTab = "overview" | "agents" | "findings" | "logs";
type ReviewEventFilter = "all" | "model" | "workflow" | "errors";

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

const findingCategoryLabels: Record<string, string> = {
  architecture: "架构",
  authorization: "权限",
  security: "安全",
  database: "数据库",
  business_contract: "业务契约",
  test_gap: "测试缺口",
  reliability: "可靠性",
};

const evaluationGateReasonLabels: Record<string, string> = {
  admitted: "已准入",
  insufficient_samples: "样本不足",
  precision_below_threshold: "精确率不足",
  high_severity_false_positive_rate_above_threshold: "高风险否决偏高",
};

const ciStateLabels: Record<string, string> = {
  not_configured: "未配置",
  unknown: "未知",
  pending: "进行中",
  success: "通过",
  failure: "失败",
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
  "review.model.budget_exhausted": "模型硬预算已耗尽",
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
  const [findingPageBusy, setFindingPageBusy] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [retryTargetStage, setRetryTargetStage] = useState<RetryTargetStage>("agent_batches");
  const [activeTab, setActiveTab] = useState<ReviewDetailTab>("overview");
  const [findingSeverity, setFindingSeverity] = useState("all");
  const [findingStatus, setFindingStatus] = useState("all");
  const [findingQuery, setFindingQuery] = useState("");
  const [eventFilter, setEventFilter] = useState<ReviewEventFilter>("all");
  const [identitySyncBusy, setIdentitySyncBusy] = useState(false);
  const [identitySyncError, setIdentitySyncError] = useState("");
  const identitySyncAttempted = useRef<string | null>(null);
  const canAdjudicate = hasPermission(user, "findings:adjudicate");
  const canManageReviews = hasPermission(user, "reviews:manage");

  const loadDetails = useCallback(async (signal?: AbortSignal) => {
    try {
      const next = await api.reviewDetails(reviewRunId, undefined, 50, signal);
      setDetails((current) => applyRefreshedFindingPage(current, next));
      setError("");
      return true;
    } catch (reason) {
      if (signal?.aborted) return false;
      if (reason instanceof ApiError && reason.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return false;
      }
      setError(errorMessage(reason));
      return false;
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [onSignedOut, reviewRunId]);

  useEffect(() => {
    const controller = new AbortController();
    setDetails(null);
    setLoading(true);
    void loadDetails(controller.signal);
    return () => controller.abort();
  }, [loadDetails]);

  useReviewAutoRefresh({
    enabled: autoRefresh,
    reviewRunId,
    changeToken: details?.change_token ?? null,
    onChanged: loadDetails,
    onSignedOut,
    onError: (reason) => setError(errorMessage(reason)),
  });

  const identityNeedsSync = Boolean(
    details
      && !details.identity_fetched_at
      && (
        !details.pr_author_login
        || !details.pr_html_url
        || !details.head_repository
        || !details.head_ref
        || !details.base_repository
        || !details.base_ref
      ),
  );

  const syncIdentity = useCallback(async () => {
    if (!details || identitySyncBusy || !canManageReviews) return;
    setIdentitySyncBusy(true);
    setIdentitySyncError("");
    try {
      const next = await api.syncReviewIdentity(
        details.review_run_id,
        `ui:identity:${details.review_run_id}`,
      );
      setDetails((current) => applyRefreshedFindingPage(current, next));
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
      } else {
        setIdentitySyncError(errorMessage(reason));
      }
    } finally {
      setIdentitySyncBusy(false);
    }
  }, [canManageReviews, details, identitySyncBusy, onSignedOut]);

  useEffect(() => {
    if (
      !canManageReviews
      || !details
      || !identityNeedsSync
      || identitySyncAttempted.current === details.review_run_id
    ) {
      return;
    }
    identitySyncAttempted.current = details.review_run_id;
    void syncIdentity();
  }, [canManageReviews, details, identityNeedsSync, syncIdentity]);

  const currentMessage = useMemo(
    () => (details ? phaseLabels[details.phase] ?? details.phase : "正在读取任务详情"),
    [details],
  );

  async function runAction(action: ReviewAction) {
    if (!details || !allowedReviewActions(user, [action]).length) return;
    if (action === "cancel" && !window.confirm("确定取消这个任务吗？")) return;
    if (action === "approve" && !window.confirm("批准后才会开放人工 GitHub 发布，继续吗？")) return;
    if (action === "reject" && !window.confirm("确定驳回本次审查结果吗？")) return;
    if (action === "retry_stage" && !window.confirm("将清除所选阶段及之后的结果，并从该阶段重新审查。继续吗？")) return;
    if (action === "publish" && !window.confirm("确定把已批准结果人工发布到 GitHub 吗？")) return;
    if (
      action === "resume"
      && details.last_error_code === "model_budget_exceeded"
      && !window.confirm("本任务已耗尽模型预算。继续会追加一个同等预算窗口，并记录到审计日志；确定继续吗？")
    ) return;
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
    if (!details || !canAdjudicate) return;
    setFindingBusy(finding.id);
    try {
      const next = await api.decideFinding(
        details.review_run_id,
        finding.id,
        decision,
        actionKey(),
      );
      const reviewedAt = new Date().toISOString();
      setDetails((current) => {
        const merged = applyRefreshedFindingPage(current, next);
        return {
          ...merged,
          findings: merged.findings.map((item) => (
            item.id === finding.id
              ? {
                  ...item,
                  adjudication_status: decision,
                  reviewed_at: reviewedAt,
                  reviewed_by: user.username,
                }
              : item
          )),
        };
      });
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

  async function loadMoreFindings() {
    if (!details?.finding_next_cursor || findingPageBusy) return;
    setFindingPageBusy(true);
    try {
      const next = await api.reviewDetails(
        details.review_run_id,
        details.finding_next_cursor,
      );
      setDetails((current) => (
        current ? appendFindingPage(current, next) : next
      ));
      setError("");
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
      } else {
        setError(errorMessage(reason));
      }
    } finally {
      setFindingPageBusy(false);
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

  const availableActions = allowedReviewActions(user, details.available_actions);
  const hasActions = availableActions.length > 0;
  const budgetPaused = details.last_error_code === "model_budget_exceeded";
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
  const agentSummaries = agentDefinitions.map((definition) => ({
    ...definition,
    progress: agentProgress(details.events, definition.key),
  }));
  const completedAgentCount = agentSummaries.filter(
    (item) => item.progress.status === "completed",
  ).length;
  const failedAgentCount = agentSummaries.filter(
    (item) => item.progress.status === "failed",
  ).length;
  const finalAgentProgress = agentSummaries.find(
    (item) => item.key === "summary",
  )!.progress;
  const hasBranchRoute = Boolean(details.head_ref || details.base_ref);
  const headBranchLabel = branchLabel(
    details.head_repository ?? details.repository,
    details.head_ref,
  );
  const baseBranchLabel = branchLabel(
    details.base_repository ?? details.repository,
    details.base_ref,
  );
  const normalizedFindingQuery = findingQuery.trim().toLocaleLowerCase();
  const filteredFindings = details.findings.filter((finding) => (
    (findingSeverity === "all" || finding.severity === findingSeverity)
    && (findingStatus === "all" || finding.adjudication_status === findingStatus)
    && (!normalizedFindingQuery || [
      finding.title,
      finding.category,
      finding.location_file ?? "",
      finding.evidence,
    ].some((value) => value.toLocaleLowerCase().includes(normalizedFindingQuery)))
  ));
  const evaluatedGates = details.evaluation_gates.filter(
    (gate) => gate.sample_count > 0,
  );
  const admittedGateCount = details.evaluation_gates.filter(
    (gate) => gate.admitted,
  ).length;
  const filteredEvents = details.events.filter((event) => {
    if (eventFilter === "errors") return isErrorEvent(event);
    if (eventFilter === "model") return event.event_type.startsWith("review.model.");
    if (eventFilter === "workflow") return !event.event_type.startsWith("review.model.");
    return true;
  }).reverse();
  const tabs: ReadonlyArray<[ReviewDetailTab, string, string]> = [
    ["overview", "任务概览", details.current_stage],
    ["agents", "Agent 进度", `${completedAgentCount}/4`],
    ["findings", "审查问题", String(details.findings.length)],
    ["logs", "运行日志", String(details.events.length)],
  ];
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
          <span className="review-user-chip">{user.username} · {roleLabels[user.role]}</span>
        </div>
      </header>

      <main className="review-detail-main">
        {error && <div className="review-inline-error" role="alert">{error}</div>}
        <section className={`review-hero review-hero-${details.phase}`}>
          <div className="review-hero-copy">
            <div className="review-hero-kicker"><span className="review-hero-pulse" />{details.repository} · PR #{details.pull_request_number}</div>
            <h1>{details.pr_title || `Pull Request #${details.pull_request_number}`}</h1>
            <p>{displayMessage}</p>
            <div className="review-pr-identity">
              <div className="review-pr-author">
                <span>提起人</span>
                <strong>{details.pr_author_login
                  ? `@${details.pr_author_login}`
                  : details.identity_fetched_at
                    ? "GitHub 未返回作者"
                    : "历史任务未记录作者"}</strong>
              </div>
              {hasBranchRoute ? (
                <div className="review-pr-branch-flow" title={`${headBranchLabel} → ${baseBranchLabel}`}>
                  <div><span>来源</span><code>{headBranchLabel}</code></div>
                  <b aria-hidden="true">→</b>
                  <div><span>目标</span><code>{baseBranchLabel}</code></div>
                </div>
              ) : (
                <span className="review-pr-branch-legacy">{details.identity_fetched_at
                  ? "GitHub 未返回完整的来源与目标分支"
                  : "历史任务未记录来源与目标分支"}</span>
              )}
              {identityNeedsSync && canManageReviews && (
                <div className="review-pr-identity-missing">
                  <span className="review-pr-branch-legacy">PR 身份信息不完整</span>
                  <button
                    type="button"
                    className="review-identity-sync-btn"
                    onClick={() => void syncIdentity()}
                    disabled={identitySyncBusy}
                  >
                    {identitySyncBusy ? "同步中…" : "从 GitHub 同步"}
                  </button>
                </div>
              )}
              {identitySyncError && <small className="review-identity-sync-error" role="alert">{identitySyncError}</small>}
            </div>
            <div className="review-hero-meta">
              <span><code>{shortSha(details.head_sha)}</code></span>
              <span>{details.changed_files_count ?? "—"} 个变更文件</span>
              <span>更新于 {formatDate(details.updated_at)}</span>
              {details.pr_html_url && (
                <a href={details.pr_html_url} target="_blank" rel="noreferrer">
                  在 GitHub 查看 PR ↗
                </a>
              )}
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
            {availableActions.includes("retry_stage") && (
              <label className="review-retry-target">
                <span>重审起点</span>
                <select
                  id="review-retry-stage"
                  name="review-retry-stage"
                  value={retryTargetStage}
                  disabled={actionBusy !== null}
                  onChange={(event) => setRetryTargetStage(event.target.value as RetryTargetStage)}
                >
                  {retryTargetOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </label>
            )}
            {hasActions ? availableActions.map((action) => (
              <button
                key={action}
                type="button"
                className={`review-action-btn action-${action}`}
                disabled={actionBusy !== null}
                onClick={() => void runAction(action)}
              >
                <DetailIcon>{actionIcons[action]}</DetailIcon>{actionBusy === action ? "处理中…" : action === "resume" && budgetPaused ? "追加预算并继续" : action === "expedite" && retryPending ? "立即重试" : action === "retry_stage" && details.workflow_status === "rejected" ? "从所选阶段重审" : actionLabels[action]}
              </button>
            )) : <span className="review-no-actions">当前节点无需手动操作</span>}
          </div>
        </section>

        <section className="review-summary-strip" aria-label="任务关键指标">
          <div><span>当前状态</span><strong>{workflowReadout(details, retryPending)}</strong><small>{stageLabels[details.current_stage] ?? details.current_stage}</small></div>
          <div><span>CI 检查</span><strong>{ciStateLabels[details.ci_state ?? ""] ?? "等待"}</strong><small>{details.ci_checks.length} 项检查</small></div>
          <div><span>文件覆盖</span><strong>{details.plan_unit_count ?? 0}/{details.changed_files_count ?? 0}</strong><small>送入 AI / 变更文件</small></div>
          <div><span>Agent</span><strong className={failedAgentCount > 0 ? "is-negative" : ""}>{completedAgentCount}/4</strong><small>{failedAgentCount > 0 ? `${failedAgentCount} 路失败` : "完成进度"}</small></div>
          <div><span>候选问题</span><strong>{details.findings.length}</strong><small>{details.unreviewed_finding_count} 条待裁决</small></div>
          <div><span>模型用量</span><strong>{(details.model_input_tokens ?? 0).toLocaleString()}</strong><small>输入 Token</small></div>
        </section>

        <nav className="review-detail-tabs" aria-label="详情视图">
          {tabs.map(([tab, label, count]) => (
            <button key={tab} type="button" className={activeTab === tab ? "is-active" : ""} aria-current={activeTab === tab ? "page" : undefined} onClick={() => setActiveTab(tab)}>
              <span>{label}</span><b>{tab === "overview" ? stageLabels[count] ?? count : count}</b>
            </button>
          ))}
        </nav>

        <div className={`review-detail-grid active-${activeTab}`}>
          <div className="review-detail-primary">
            <StageTimeline details={details} />
            <ModelBatchPanel details={details} />
            {!agentSummaries.some((item) => item.progress.events.length > 0) && !details.model_review_completed_at && (
              <section className="review-panel review-agent-tab-empty"><DetailIcon>◌</DetailIcon><div><strong>Agent 尚未开始执行</strong><p>完成 CI 和审查规划后，四路 Agent 的实时进度会显示在这里。</p></div></section>
            )}

            <section className="review-panel review-result-panel">
              <div className="review-panel-heading">
                <div><span className="review-eyebrow">AI OUTPUT</span><h2>审查结果</h2></div>
                <div className="review-result-counts"><span className="result-count result-count-total">{details.findings.length} 条候选</span>{details.new_finding_count > 0 && <span className="result-count result-count-new">{details.new_finding_count} 条新增</span>}{details.fixed_finding_count > 0 && <span className="result-count result-count-fixed">{details.fixed_finding_count} 条已修复</span>}{details.unreviewed_finding_count > 0 && <span className="result-count result-count-pending">{details.unreviewed_finding_count} 待裁决</span>}</div>
              </div>
              {(finalAgentProgress.status === "completed" || details.model_review_completed_at) && (
                finalAgentProgress.hasStructuredConclusion && finalAgentProgress.verdict ? (
                  <div className={`review-final-conclusion verdict-${finalAgentProgress.verdict}`}>
                    <div className="review-final-conclusion-heading">
                      <div>
                        <span>汇总 Agent 最终结论</span>
                        <strong>{verdictLabels[finalAgentProgress.verdict]}</strong>
                      </div>
                      <b>{finalAgentProgress.findingCount} 条候选问题</b>
                    </div>
                    <p>{finalAgentProgress.conclusionSummary}</p>
                    {finalAgentProgress.checkedAreas.length > 0 && (
                      <div className="review-checked-areas" aria-label="最终结论覆盖范围">
                        {finalAgentProgress.checkedAreas.map((area) => <span key={area}>{area}</span>)}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="review-final-conclusion is-legacy">
                    <div className="review-final-conclusion-heading">
                      <div><span>汇总 Agent 最终结论</span><strong>历史任务未保存结构化结论</strong></div>
                    </div>
                    <p>这条任务完成时还没有保存结论摘要和检查范围。当前只能确认候选问题数量，不能把“0 条”直接解释成“未发现问题”；重新审查后会显示真实结论。</p>
                  </div>
                )
              )}
              <div className="review-evaluation-gates">
                <div className="review-evaluation-heading">
                  <div><strong>行内评论评测准入</strong><span>最近人工裁决</span></div>
                  <b>{admittedGateCount}/{details.evaluation_gates.length} 个风险域</b>
                </div>
                {evaluatedGates.length === 0 ? (
                  <p className="review-evaluation-empty">暂无已裁决样本，本轮仅发布 Check 与汇总评论。</p>
                ) : (
                  <div className="review-evaluation-list">
                    {evaluatedGates.map((gate) => (
                      <div className={`review-evaluation-row ${gate.admitted ? "is-admitted" : "is-blocked"}`} key={gate.category}>
                        <strong>{findingCategoryLabels[gate.category] ?? gate.category}</strong>
                        <span>{gate.sample_count} 个样本</span>
                        <span>精确率 {Math.round(gate.precision * 100)}%</span>
                        <span>高风险否决 {Math.round(gate.high_severity_false_positive_rate * 100)}%</span>
                        <b>{evaluationGateReasonLabels[gate.reason] ?? gate.reason}</b>
                      </div>
                    ))}
                  </div>
                )}
              </div>
              {details.finding_total_count > 0 && (
                <div className="review-finding-toolbar">
                  <label><span>严重程度</span><select id="review-finding-severity" name="review-finding-severity" value={findingSeverity} onChange={(event) => setFindingSeverity(event.target.value)}><option value="all">全部级别</option><option value="critical">严重</option><option value="high">高风险</option><option value="medium">中风险</option><option value="low">低风险</option></select></label>
                  <label><span>人工裁决</span><select id="review-finding-status" name="review-finding-status" value={findingStatus} onChange={(event) => setFindingStatus(event.target.value)}><option value="all">全部状态</option><option value="unreviewed">待裁决</option><option value="valid">有效问题</option><option value="false_positive">误报</option><option value="duplicate">重复问题</option><option value="out_of_scope">超出范围</option><option value="known_issue">已知问题</option></select></label>
                  <label className="review-finding-search"><span>搜索</span><input id="review-finding-query" name="review-finding-query" value={findingQuery} onChange={(event) => setFindingQuery(event.target.value)} placeholder="标题、文件或证据" /></label>
                  <strong>{filteredFindings.length}/{details.finding_total_count} 条结果</strong>
                </div>
              )}
              {!details.model_review_completed_at && (currentModelFailure || retryPending) && (
                <div className="review-result-empty result-empty-error"><DetailIcon>!</DetailIcon><div><strong>{retryPending ? "AI 请求失败，已安排自动重试" : "AI 请求失败"}</strong><p>{payloadString(currentModelFailure, "error_message") ?? details.last_error ?? "模型服务未返回可用结果"}</p><small>HTTP {failureStatus ?? "—"} · {formatDuration(failureDuration)} · 错误码 {failureCode ?? "—"}{retryStatus ? ` · ${retryStatus}` : ""}</small></div></div>
              )}
              {!details.model_review_completed_at && !currentModelFailure && !retryPending && (
                <div className="review-result-empty"><DetailIcon>◌</DetailIcon><div><strong>AI 结果尚未生成</strong><p>模型完成后，候选问题会显示在这里。</p></div></div>
              )}
              {details.model_review_completed_at && details.finding_total_count === 0 && (
                <div className={`review-result-empty ${finalAgentProgress.verdict === "no_actionable_issue" ? "result-empty-positive" : finalAgentProgress.verdict === "insufficient_context" ? "result-empty-limited" : ""}`}><DetailIcon>{finalAgentProgress.verdict === "insufficient_context" ? "!" : "✓"}</DetailIcon><div><strong>本次没有候选问题</strong><p>{finalAgentProgress.hasStructuredConclusion ? "具体判断、依据范围和限制见上方汇总 Agent 结论。" : "这条历史记录没有保存结论摘要，不能仅凭候选问题数量推断分析结果。"}</p></div></div>
              )}
              {details.findings.length > 0 && filteredFindings.length === 0 && (
                <div className="review-result-empty"><DetailIcon>⌕</DetailIcon><div><strong>没有符合筛选条件的问题</strong><p>调整严重程度、状态或搜索关键词后再查看。</p></div></div>
              )}
              {filteredFindings.length > 0 && (
                <div className="review-findings-list">
                  {filteredFindings.map((finding) => (
                    <FindingCard
                      key={finding.id}
                      finding={finding}
                      busy={findingBusy === finding.id}
                      editable={canAdjudicate}
                      onDecision={decideFinding}
                    />
                  ))}
                </div>
              )}
              {details.finding_next_cursor && (
                <div className="review-finding-pagination">
                  <button
                    type="button"
                    className="review-quiet-btn"
                    disabled={findingPageBusy}
                    onClick={() => void loadMoreFindings()}
                  >
                    {findingPageBusy ? "正在加载" : "加载更多"}
                  </button>
                </div>
              )}
            </section>

            <section className="review-panel review-log-panel">
              <div className="review-panel-heading">
                <div><span className="review-eyebrow">EVENT LOG</span><h2>运行日志</h2></div>
                <div className="review-log-filters" role="group" aria-label="日志筛选">
                  {(["all", "model", "workflow", "errors"] as ReviewEventFilter[]).map((filter) => <button key={filter} type="button" className={eventFilter === filter ? "is-active" : ""} onClick={() => setEventFilter(filter)}>{filter === "all" ? "全部" : filter === "model" ? "模型" : filter === "workflow" ? "流程" : "异常"}</button>)}
                  <span className="review-log-count">{filteredEvents.length} 条</span>
                </div>
              </div>
              {filteredEvents.length === 0 ? <div className="review-empty-small">当前筛选下没有结构化事件记录</div> : (
                <div className="review-event-list">
                  {filteredEvents.map((event) => (
                    <div className={`review-event-row ${isErrorEvent(event) ? "is-error" : ""}`} key={event.id}>
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
