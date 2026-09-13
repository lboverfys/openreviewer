import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, ApiError, peekReadCache, subscribeReadCache } from "./api";
import { allowedReviewActions, hasPermission } from "./rbac";
import FindingCard from "./ReviewFindingCard";
import Pagination from "./Pagination";
import { useCursorPage } from "./useCursorPage";
import RetrievalTracePanel from "./RetrievalTracePanel";
import StaticAnalysisPanel from "./StaticAnalysisPanel";
import "./styles/retrieval.css";
import {
  DetailIcon,
  ModelBatchPanel,
  StageTimeline,
} from "./ReviewProgressPanels";
import { ciDisplayStatus, ReviewSidebar } from "./ReviewSidebarPanels";
import {
  actionKey,
  agentDefinitions,
  agentProgress,
  branchLabel,
  currentReviewEvents,
  eventDetail,
  formatDuration,
  isErrorEvent,
  latestBatchPlanEvent,
  latestEvent,
  payloadNumber,
  payloadString,
  retryDetail,
  verdictLabels,
  workflowReadout,
} from "./review-details";
import type {
  AuthUser,
  ContextEvidence,
  RetrievalTrace,
  FindingDecision,
  ReviewAction,
  ReviewDetails,
  ReviewFinding,
  ReviewEvent,
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
  retry_stage: "从指定阶段重试",
  approve: "批准审查",
  reject: "驳回",
  publish: "发布到 GitHub",
  expedite: "立即执行",
  retry: "重试当前失败节点",
  cancel: "取消任务",
  rerun: "检查最新提交",
  retry_failed_node: "重试当前失败节点",
  new_review: "检查最新提交",
  review_snapshot: "复查此版本",
};

type RetryTargetStage = "ci" | "planning" | "agent_batches" | "aggregating";
type ReviewDetailTab = "overview" | "agents" | "findings" | "logs";
type ReviewEventFilter = "all" | "model" | "workflow" | "errors";

// 这些状态不会再产生后台事件。详情页仍可由用户手动刷新，
// 但没有必要继续每隔几秒请求变更令牌。
const terminalReviewStatuses = new Set([
  "completed",
  "failed",
  "timed_out",
  "cancelled",
  "superseded",
]);

const retryTargetOptions: ReadonlyArray<[RetryTargetStage, string]> = [
  ["ci", "CI 检查"],
  ["planning", "审查规划"],
  ["agent_batches", "三路 Agent"],
  ["aggregating", "结果汇总"],
];

function retryStageNotice(targetStage: RetryTargetStage): string {
  switch (targetStage) {
    case "ci":
      return "将清理 CI 之后的规划、Agent 批次、Finding 和汇总结果；保留任务身份、提交 SHA 与审计日志。";
    case "planning":
      return "将清理当前规划及后续 Agent 批次、Finding 和汇总结果；保留任务身份、CI 结果、提交 SHA 与审计日志。";
    case "agent_batches":
      return "将清理全部模型批次、模型 Finding 与汇总结果；保留任务身份、CI 结果、审查计划、提交 SHA 与审计日志。";
    case "aggregating":
      return "将清理当前汇总快照；保留安全、规范、逻辑 Agent 的成功批次，Finding 会在重试时由这些批次重新生成，并保留任务身份及审计日志。";
  }
}

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
  retry_failed_node: "↻",
  new_review: "＋",
  review_snapshot: "↻",
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
  "review.model.agent_not_applicable": "审查 Agent 不适用",
  "review.model.aggregating_started": "开始汇总审查结果",
  "review.model.summary_completed": "汇总 Agent 已完成",
  "review.model.summary_failed": "汇总 Agent 失败",
  "review.model.summary_skipped": "汇总未执行",
  "review.model.aggregation_completed": "本地汇总已完成",
  "review.model.workflow_partial": "部分结果已保存",
  "review.model.retry_requested": "已请求节点重试",
  "review.model.retry_started": "节点重试已开始",
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
  "review.manual.retry_failed_node": "管理员重试失败节点",
  "review.manual.new_review_requested": "已创建最新提交审查",
  "review.manual.cancel": "管理员取消任务",
  "review.manual.rerun_requested": "管理员发起重新审查",
  "review.finding.decided": "管理员更新问题裁决",
};

function ReviewDetailPage({
  user,
  reviewRunId,
  onBack,
  onOpenReview,
  onSignedOut,
}: ReviewDetailPageProps) {
  const [activeTab, setActiveTab] = useState<ReviewDetailTab>("overview");
  const detailView = activeTab === "findings" ? "findings" : "overview";
  const detailsKey = `review-details:${reviewRunId}:first:10:${detailView}`;
  const cachedDetails = peekReadCache<ReviewDetails>(detailsKey);
  const [details, setDetails] = useState<ReviewDetails | null>(cachedDetails ?? null);

  const [retrievalTraces, setRetrievalTraces] = useState<RetrievalTrace[]>([]);
  const [retrievalLoadError, setRetrievalLoadError] = useState("");
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const retrievalEvidence = useMemo<Record<string, ContextEvidence>>(
    () => Object.fromEntries(retrievalTraces.flatMap(trace => trace.candidates).map(item => [item.reference_id, item])),
    [retrievalTraces],
  );

  const [loading, setLoading] = useState(cachedDetails === undefined);
  const [error, setError] = useState("");
  const [actionBusy, setActionBusy] = useState<ReviewAction | null>(null);
  const [findingBusy, setFindingBusy] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [retryTargetStage, setRetryTargetStage] = useState<RetryTargetStage>("agent_batches");
  const [findingSeverity, setFindingSeverity] = useState("all");
  const [findingStatus, setFindingStatus] = useState("all");
  const [findingQuery, setFindingQuery] = useState("");
  const [eventFilter, setEventFilter] = useState<ReviewEventFilter>("all");
  const [identitySyncBusy, setIdentitySyncBusy] = useState(false);
  const [identitySyncError, setIdentitySyncError] = useState("");
  const identitySyncAttempted = useRef<string | null>(null);
  const detailsRequestSequence = useRef(0);
  const canAdjudicate = hasPermission(user, "findings:adjudicate");
  const canManageReviews = hasPermission(user, "reviews:manage");

  const handlePageError = useCallback((reason: unknown) => {
    if (reason instanceof ApiError && reason.status === 401) onSignedOut("登录状态已失效，请重新登录");
    else setError(errorMessage(reason));
  }, [onSignedOut]);
  const [debouncedFindingQuery, setDebouncedFindingQuery] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedFindingQuery(findingQuery.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [findingQuery]);
  const loadFindings = useCallback((cursor?: string, signal?: AbortSignal, force = false) =>
    api.findingPage(reviewRunId, cursor, signal, force, findingSeverity, findingStatus, debouncedFindingQuery),
  [reviewRunId, findingSeverity, findingStatus, debouncedFindingQuery]);
  const loadEvents = useCallback((cursor?: string, signal?: AbortSignal, force = false) =>
    api.eventPage(reviewRunId, cursor, signal, force, eventFilter), [reviewRunId, eventFilter]);
  const findingPage = useCursorPage<ReviewFinding>({cacheKey: `findings:${reviewRunId}:${findingSeverity}:${findingStatus}:${debouncedFindingQuery}`, load: loadFindings, onError: handlePageError, enabled: activeTab === "findings"});
  const eventPage = useCursorPage<ReviewEvent>({cacheKey: `events:${reviewRunId}:${eventFilter}`, load: loadEvents, onError: handlePageError, enabled: activeTab === "logs"});
  const previousPageToken = useRef<string | undefined>(undefined);
  useEffect(() => {
    const token = details?.change_token;
    const previous = previousPageToken.current;
    previousPageToken.current = token;
    if (!token || !previous || token === previous) return;
    const controller = new AbortController();
    if (activeTab === "findings") void findingPage.refresh(true, controller.signal);
    if (activeTab === "logs") void eventPage.refresh(true, controller.signal);
    return () => controller.abort();
  }, [details?.change_token, activeTab, findingPage.refresh, eventPage.refresh]);
  useEffect(() => {
    if (!details || (activeTab !== "findings" && (activeTab !== "overview" || !evidenceOpen))) return;
    const controller = new AbortController();
    api.reviewRetrieval(reviewRunId, controller.signal).then(items => {
      if (!controller.signal.aborted) {setRetrievalTraces(items); setRetrievalLoadError("");}
    }).catch(failure => {if (!controller.signal.aborted) setRetrievalLoadError(errorMessage(failure));});
    return () => controller.abort();
  }, [reviewRunId, details?.change_token, activeTab, evidenceOpen]);

  const loadDetails = useCallback(async (signal?: AbortSignal, force = true) => {
    const sequence = ++detailsRequestSequence.current;
    try {
      const next = await api.reviewDetails(reviewRunId, undefined, 10, signal, force, detailView);
      if (signal?.aborted || sequence !== detailsRequestSequence.current) return null;
      setDetails(next);
      setError("");
      return next.change_token;
    } catch (reason) {
      if (signal?.aborted) return null;
      if (reason instanceof ApiError && reason.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return null;
      }
      setError(errorMessage(reason));
      return null;
    } finally {
      if (!signal?.aborted && sequence === detailsRequestSequence.current) {
        setLoading(false);
      }
    }
  }, [onSignedOut, reviewRunId, detailView]);

  useEffect(() => {
    const controller = new AbortController();
    const cached = peekReadCache<ReviewDetails>(detailsKey);
    setDetails(cached ?? null);
    setLoading(!cached);
    const unsubscribe = subscribeReadCache<ReviewDetails>(detailsKey, setDetails);
    void loadDetails(controller.signal, false);
    return () => {controller.abort(); unsubscribe();};
  }, [loadDetails]);

  useReviewAutoRefresh({
    enabled: autoRefresh && !terminalReviewStatuses.has(details?.execution_status ?? ""),
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
      setDetails(next);
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
    if (action === "cancel" && !window.confirm("取消后停止后续执行并保留已有记录。已发出的模型请求无法撤回，可能仍会计费。确定取消吗？")) return;
    if (action === "review_snapshot" && !window.confirm("将使用已保存的代码和当前审查配置真实调用模型，另存一条复查记录。不会重新运行 CI 或发布到 GitHub，继续吗？")) return;
    if (action === "approve" && !window.confirm("批准后才会开放人工 GitHub 发布，继续吗？")) return;
    if (action === "reject" && !window.confirm("确定驳回本次审查结果吗？")) return;
    if (action === "retry_stage" && !window.confirm(`${retryStageNotice(retryTargetStage)}\n\n确定从${retryTargetOptions.find(([value]) => value === retryTargetStage)?.[1] ?? "所选阶段"}重新审查吗？`)) return;
    if (action === "publish" && !window.confirm("确定把已批准结果人工发布到 GitHub 吗？")) return;
    if ((action === "rerun" || action === "new_review") && !window.confirm(`将使用提交 ${shortSha(details.head_sha)} 创建一条新的审查记录，当前任务和结果不会被覆盖。继续吗？`)) return;
    setActionBusy(action);
    try {
      const failedNode = action === "retry_failed_node"
        || (action === "retry" && details.coverage_status === "partial");
      const newReview = action === "new_review"
        || (action === "rerun" && details.coverage_status === "partial");
      const result = await api.reviewAction(
        details.review_run_id,
        action,
        actionKey(action, details.review_run_id, {
          stateVersion: details.change_token,
        }),
        action === "retry_stage" ? retryTargetStage : undefined,
        {
          retryScope: failedNode
            ? "failed_node"
            : newReview
              ? "new_review"
              : action === "retry_stage"
                ? "stage"
              : undefined,
          // 顶部按钮表示“所有失败节点”；卡片按钮走 retryNode，
          // 才会携带具体 Agent/批次，避免只重置失败列表中的第一项。
          agent: undefined,
          batchNumber: undefined,
          stateVersion: details.change_token,
          headSha: details.head_sha,
        },
      );
      if ((action === "rerun" || action === "new_review" || action === "review_snapshot") && result.review_run_id !== details.review_run_id) {
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

  async function retryNode(agent: string, batchNumber?: number) {
    if (!details || !canManageReviews || actionBusy !== null) return;
    const action = "retry_failed_node" as ReviewAction;
    setActionBusy(action);
    try {
      await api.reviewAction(
        details.review_run_id,
        action,
        actionKey(action, details.review_run_id, {
          agent,
          batchNumber,
          stateVersion: details.change_token,
        }),
        undefined,
        {
          retryScope: "failed_node",
          agent,
          batchNumber,
          stateVersion: details.change_token,
          headSha: details.head_sha,
        },
      );
      await loadDetails();
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
      setDetails(next);
      await findingPage.refresh();
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

  const availableActions = allowedReviewActions(user, details.available_actions).filter(action =>
    !(action === "start" && details.available_actions.includes("expedite"))
    && !((action === "new_review" || action === "rerun") && (details.pr_state === "closed" || details.snapshot_review))
  );
  const hasActions = availableActions.length > 0;
  const latestBatchPlan = latestBatchPlanEvent(details.events);
  const currentEvents = currentReviewEvents(details.events);
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
  const latestRequestLifecycleEvent = [...currentEvents].reverse().find((event) => (
    payloadNumber(event, "model_attempt_count") === details.model_attempt_count
    && [
      "review.model.request_started",
      "review.model.request_completed",
      "review.model.batch_failed",
    ].includes(event.event_type)
  ));
  const stopped = ["paused", "cancelled", "superseded", "rejected"].includes(details.phase);
  const retryPending = Boolean(
    currentRetryEvent
    && details.execution_status === "ready_for_review" && !stopped,
  );
  const waitingForIndex = retryPending && payloadString(currentRetryEvent, "error_code") === "retrieval_index_pending";
  const taskStateLabel = waitingForIndex ? "准备代码索引" : workflowReadout(details, retryPending);
  const retryStatus = retryDetail(currentRetryEvent);
  const requestInFlight = latestRequestLifecycleEvent?.event_type
    === "review.model.request_started";
  const modelProgressActive = details.execution_status === "running" && !details.model_review_completed_at && currentEvents.some(
    (event) => event.event_type === "review.model.batch_started",
  );
  const modelDisplayState = details.model_status === "succeeded"
    ? "成功"
    : stopped ? details.phase === "paused" ? "已暂停" : "已停止"
    : waitingForIndex ? "等待代码索引"
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
    : stopped ? ""
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
    ? waitingForIndex ? "正在准备关联代码索引，完成后自动继续 AI 检查" : `上一轮处理未完成，${retryStatus ?? "系统已安排自动重试"}`
    : currentMessage;
  const failureStatus = payloadNumber(currentModelFailure, "status_code");
  const failureDuration = payloadNumber(currentModelFailure, "duration_ms");
  const failureCode = payloadString(currentModelFailure, "error_code");
  const failureRequestId = payloadString(currentModelFailure, "provider_request_id");
  const agentSummaries = agentDefinitions.map((definition) => ({
    ...definition,
    progress: agentProgress(details.events, definition.key, details.batch_progress?.[definition.key]),
  }));
  const completedAgentCount = agentSummaries.filter(
    (item) => item.progress.status === "completed",
  ).length;
  const failedAgentCount = agentSummaries.filter(
    (item) => item.progress.status === "failed",
  ).length;
  const requiredAgentCount = agentSummaries.filter(item =>
    item.progress.status !== "not_applicable" && !(item.key === "summary"
      && details.aggregation_status === "local" && details.summary_status === "skipped")
  ).length;
  const finalAgentProgress = agentSummaries.find(
    (item) => item.key === "summary",
  )!.progress;
  const hasBranchRoute = Boolean(details.head_ref || details.base_ref);
  const completedStageCount = details.stages.filter(
    (stage) => stage.status === "completed",
  ).length;
  const applicableStageCount = details.stages.filter(stage => stage.status !== "skipped").length;
  const headBranchLabel = branchLabel(
    details.head_repository ?? details.repository,
    details.head_ref,
  );
  const baseBranchLabel = branchLabel(
    details.base_repository ?? details.repository,
    details.base_ref,
  );
  const filteredFindings = findingPage.data?.items ?? [];
  const evaluatedGates = details.evaluation_gates.filter(
    (gate) => gate.sample_count > 0,
  );
  const admittedGateCount = details.evaluation_gates.filter(
    (gate) => gate.admitted,
  ).length;
  const filteredEvents = eventPage.data?.items ?? [];
  const tabs: ReadonlyArray<[ReviewDetailTab, string, string]> = [
    ["overview", "任务概览", details.current_stage],
    ["agents", "AI 检查过程", `${completedAgentCount}/${requiredAgentCount}`],
    ["findings", "问题与结论", String(details.finding_total_count)],
    ["logs", "运行日志", "按页查看"],
  ];
  return (
    <div className="review-detail-shell">
      <main className="review-detail-main">
        <div className="review-detail-toolbar">
          <button type="button" className="review-back-link" onClick={onBack}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
            返回审查任务
          </button>
          <div className="review-detail-toolbar-actions">
            {hasPermission(user, "findings:adjudicate") && details.model_status === "succeeded" && details.coverage_status === "complete" && (
              <button type="button" className="btn-ghost" onClick={() => {window.location.hash="evaluations?review="+encodeURIComponent(reviewRunId);}}>加入评测</button>
            )}
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
            <button type="button" className="btn-ghost" onClick={() => void loadDetails().then(token => {
              if (token !== details.change_token) return;
              if (activeTab === "findings") void findingPage.refresh();
              if (activeTab === "logs") void eventPage.refresh();
            })} disabled={loading} title="立即刷新详情">↻ <span>刷新</span></button>
          </div>
        </div>
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
            <span className="review-current-stage-label">任务状态</span>
            <strong>{taskStateLabel}</strong>
            <span className="review-current-phase">{stageLabels[details.current_stage] ?? details.current_stage}</span>
            <div className="review-hero-stage-progress" aria-hidden="true">
              <span style={{ width: `${applicableStageCount > 0 ? (completedStageCount / applicableStageCount) * 100 : 0}%` }} />
            </div>
            <small>{["cancelled", "superseded", "rejected"].includes(details.phase) ? "后续步骤已停止" : `${completedStageCount}/${applicableStageCount} 步骤已完成`}</small>
          </div>
        </section>

        <section className="review-control-strip">
          <div className="review-control-summary">
            <span className="review-control-title">任务控制</span>
            <span className={`review-control-hint ${retryPending ? "is-retry" : ""}`}>{retryPending ? retryStatus : details.phase === "cancelled" ? "本次已结束，可另建复查记录" : "按当前状态提供可执行操作"}</span>
          </div>
          <div className="review-control-actions">
            {hasActions ? availableActions.filter(action => action !== "retry_stage").map((action) => (
              <div className="review-action-with-hint" key={action}>
                <button
                  type="button"
                  className={`review-action-btn action-${action}`}
                  disabled={actionBusy !== null}
                  onClick={() => void runAction(action)}
                >
                  <DetailIcon>{actionIcons[action]}</DetailIcon>{actionBusy === action ? "处理中…" : action === "expedite" && retryPending ? "立即重试" : actionLabels[action]}
                </button>
                {(action === "retry_failed_node" || action === "retry") && <small>不会重复调用已成功的模型请求</small>}
                {(action === "new_review" || action === "rerun") && <small>创建新记录，不覆盖当前任务</small>}
                {action === "review_snapshot" && <small>使用已保存代码，另存检查结果</small>}
              </div>
            )) : <span className="review-no-actions">当前节点无需手动操作</span>}
          </div>
        </section>

        {availableActions.includes("retry_stage") && <details className="review-advanced-actions">
          <summary>高级重试：从指定步骤重新执行</summary>
          <label className="review-retry-target">重审起点<select value={retryTargetStage} disabled={actionBusy !== null}
            onChange={event => setRetryTargetStage(event.target.value as RetryTargetStage)}>
            {retryTargetOptions.filter(([value]) => !details.snapshot_review || value !== "ci").map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select></label>
          <p>{retryStageNotice(retryTargetStage)}</p><button type="button" disabled={actionBusy !== null} onClick={() => void runAction("retry_stage")}>从所选步骤重试</button>
        </details>}
        {details.snapshot_review && <section className="review-snapshot-banner"><strong>历史版本复查</strong>
          本次分析已保存的提交 {shortSha(details.head_sha)}，使用当前审查配置。不会重新运行 CI 或发布到 GitHub，原任务与结果保留。
        </section>}

        {details.coverage_status === "partial" && (
          <section className="review-coverage-warning" role="status">
            <DetailIcon>!</DetailIcon>
            <div><strong>部分覆盖</strong><p>已有结果可以查看，但仍有 Agent 或批次待重试；完成前不能批准或发布。</p></div>
          </section>
        )}

        <section className="review-summary-strip" aria-label="任务关键指标">
          <div><span className="review-metric-icon is-state" aria-hidden="true">◈</span><div><span>当前状态</span><strong>{taskStateLabel}</strong><small>{stageLabels[details.current_stage] ?? details.current_stage}</small></div></div>
          <div><span className="review-metric-icon is-ci" aria-hidden="true">🛠</span><div><span>CI 检查</span><strong>{ciDisplayStatus(details)}</strong><small>{details.snapshot_review ? "采用保存的代码快照" : `${details.ci_checks.length} 项已保存检查`}</small></div></div>
          <div><span className="review-metric-icon is-cover" aria-hidden="true">▦</span><div><span>文件覆盖</span><strong>{details.plan_unit_count ?? 0}/{details.changed_files_count ?? 0}</strong><small>送入 AI / 变更文件</small></div></div>
          <div><span className="review-metric-icon is-agent" aria-hidden="true">🤖</span><div><span>Agent</span><strong className={failedAgentCount > 0 ? "is-negative" : ""}>{completedAgentCount}/{requiredAgentCount}</strong><small>{failedAgentCount > 0 ? `${failedAgentCount} 路失败` : "完成进度"}</small></div></div>
          <div><span className="review-metric-icon is-finding" aria-hidden="true">⚑</span><div><span>候选问题</span><strong>{details.finding_total_count}</strong><small>{details.unreviewed_finding_count} 条待裁决</small></div></div>
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
            {activeTab === "overview" && <>
            <StageTimeline details={details} />

            <section className="review-panel review-overview-result"><h2>本次结果</h2>
              <p>{details.model_review_completed_at
                ? details.finding_total_count > 0 ? `报告了 ${details.finding_total_count} 条候选问题，请结合代码证据核对。` : "本次未报告候选问题，可继续查看实际检查范围；这不代表代码绝对没有缺陷。"
                : details.phase === "cancelled" ? "任务已经停止，本次没有生成最终 AI 结论。需要重新测试时可复查已保存的版本。" : "尚未产生最终审查结果，请查看当前步骤。"}</p>
              {details.model_name && <p>模型记录：<strong>{details.model_name}</strong>，各路检查的请求和用量可在“AI 检查过程”中查看。</p>}
              {details.model_review_completed_at && <button type="button" onClick={() => setActiveTab("findings")}>查看问题与结论</button>}
            </section>

            <details className="review-panel review-evidence-group" onToggle={event => setEvidenceOpen(event.currentTarget.open)}><summary>代码依据与辅助检查<span>需要核对上下文时展开</span></summary>
            <section className="review-panel review-retrieval-panel">
              <div className="review-panel-heading"><h2>检索上下文</h2>
                {hasPermission(user, "knowledge:manage") && <button type="button" onClick={() => {window.location.hash = `retrieval/${encodeURIComponent(reviewRunId)}`;}}>打开代码索引与检索</button>}
              </div>
              {retrievalLoadError ? <p className="retrieval-warning">{retrievalLoadError}</p> : <RetrievalTracePanel traces={retrievalTraces} compact />}
            </section>
            <StaticAnalysisPanel key={reviewRunId} runId={reviewRunId} headSha={details.head_sha} editable={hasPermission(user, "findings:adjudicate")} onError={handlePageError} />
            </details>
            </>}
            {activeTab === "agents" && <>
            <ModelBatchPanel
              details={details}
              onRetry={canManageReviews && !["cancelled", "superseded", "paused", "rejected"].includes(details.phase) ? retryNode : undefined}
              retryBusy={actionBusy !== null}
            />
            {!agentSummaries.some((item) => item.progress.events.length > 0) && !details.model_review_completed_at && (
              <section className="review-panel review-agent-tab-empty"><DetailIcon>◌</DetailIcon><div><strong>Agent 尚未开始执行</strong><p>完成 CI 和审查规划后，四路 Agent 的实时进度会显示在这里。</p></div></section>
            )}

            </>}
            {activeTab === "findings" && <>
            <section className="review-panel review-result-panel">
              <div className="review-panel-heading">
                <div><span className="review-eyebrow">AI OUTPUT</span><h2>审查结果</h2></div>
                <div className="review-result-counts"><span className="result-count result-count-total">{details.finding_total_count} 条候选</span>{details.new_finding_count > 0 && <span className="result-count result-count-new">{details.new_finding_count} 条新增</span>}{details.fixed_finding_count > 0 && <span className="result-count result-count-fixed">{details.fixed_finding_count} 条已修复</span>}{details.unreviewed_finding_count > 0 && <span className="result-count result-count-pending">{details.unreviewed_finding_count} 待裁决</span>}</div>
              </div>
              {details.summary_status === "skipped" && details.coverage_status === "partial" && (
                <div className="review-summary-status is-warning"><strong>上游 Agent 未完成，汇总未执行</strong><span>已保留可用的部分结果</span></div>
              )}
              {details.aggregation_status === "local" && details.coverage_status !== "partial" && (
                <div className="review-summary-status is-local"><strong>本地汇总已完成</strong><span>结果已按身份去重并排序</span></div>
              )}
              {details.summary_status === "failed" && (
                <div className="review-summary-status is-warning"><strong>汇总失败</strong><span>已继续使用本地确定性汇总</span></div>
              )}
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
              {waitingForIndex && <div className="review-result-empty"><DetailIcon>◌</DetailIcon><div><strong>正在准备关联代码</strong><p>索引完成后自动继续，本次尚未获得 AI 结果。</p></div></div>}
              {!details.model_review_completed_at && !waitingForIndex && !stopped && (currentModelFailure || retryPending) && (
                <div className="review-result-empty result-empty-error"><DetailIcon>!</DetailIcon><div><strong>{retryPending ? "AI 请求失败，已安排自动重试" : "AI 请求失败"}</strong><p>{payloadString(currentModelFailure, "error_message") ?? details.last_error ?? "模型服务未返回可用结果"}</p><small>HTTP {failureStatus ?? "—"} · {formatDuration(failureDuration)} · 错误码 {failureCode ?? "—"}{retryStatus ? ` · ${retryStatus}` : ""}</small>{(availableActions.includes("retry_failed_node") || (availableActions.includes("retry") && details.coverage_status === "partial")) && <button type="button" className="review-inline-retry-btn" disabled={actionBusy !== null} onClick={() => void runAction(availableActions.includes("retry_failed_node") ? "retry_failed_node" : "retry")}>立即重试当前失败节点</button>}</div></div>
              )}
              {!details.model_review_completed_at && ((!currentModelFailure && !retryPending) || stopped) && (
                <div className="review-result-empty"><DetailIcon>◌</DetailIcon><div><strong>{stopped ? details.phase === "paused" ? "任务已暂停" : "任务已停止" : "AI 结果尚未生成"}</strong><p>{stopped ? "本次尚无最终结论。需要继续检查时，请使用任务控制中的可用操作。" : "模型完成后，候选问题会显示在这里。"}</p></div></div>
              )}
              {details.model_review_completed_at && details.finding_total_count === 0 && (
                <div className={`review-result-empty ${finalAgentProgress.verdict === "no_actionable_issue" ? "result-empty-positive" : finalAgentProgress.verdict === "insufficient_context" ? "result-empty-limited" : ""}`}><DetailIcon>{finalAgentProgress.verdict === "insufficient_context" ? "!" : "✓"}</DetailIcon><div><strong>本次没有候选问题</strong><p>{finalAgentProgress.hasStructuredConclusion ? "具体判断、依据范围和限制见上方汇总 Agent 结论。" : "这条历史记录没有保存结论摘要，不能仅凭候选问题数量推断分析结果。"}</p></div></div>
              )}
              {!findingPage.loading && details.finding_total_count > 0 && filteredFindings.length === 0 && (
                <div className="review-result-empty"><DetailIcon>⌕</DetailIcon><div><strong>没有符合筛选条件的问题</strong><p>调整严重程度、状态或搜索关键词后再查看。</p></div></div>
              )}
              {filteredFindings.length > 0 && (
                <div className="review-findings-list">
                  {filteredFindings.map((finding) => (
                    <FindingCard
                      contextEvidence={retrievalEvidence}
                      key={finding.id}
                      finding={finding}
                      busy={findingBusy === finding.id}
                      editable={canAdjudicate}
                      onDecision={decideFinding}
                    />
                  ))}
                </div>
              )}
              <Pagination page={findingPage.page} count={filteredFindings.length} hasNext={Boolean(findingPage.data?.next_cursor)} busy={findingPage.loading} onPrevious={findingPage.previous} onNext={findingPage.next} label="审查问题分页" />
            </section>

            </>}
            {activeTab === "logs" && <>
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
              <Pagination page={eventPage.page} count={filteredEvents.length} hasNext={Boolean(eventPage.data?.next_cursor)} busy={eventPage.loading} onPrevious={eventPage.previous} onNext={eventPage.next} label="运行日志分页" />
            </section>
            </>}
          </div>

          {activeTab === "overview" && <details className="review-panel review-runtime-details"><summary>运行信息（高级）</summary><ReviewSidebar
            details={details}
            retryPending={retryPending}
            retryStatus={retryStatus}
            modelDisplayState={modelDisplayState}
            modelStateClass={modelStateClass}
            modelDisplayName={modelDisplayName}
            latestBatchPlan={latestBatchPlan}
            failureStatus={failureStatus}
            failureDuration={failureDuration}
            failureCode={failureCode}
            failureRequestId={failureRequestId}
            hasBranchRoute={hasBranchRoute}
            headBranchLabel={headBranchLabel}
            baseBranchLabel={baseBranchLabel}
          /></details>}
        </div>
      </main>
    </div>
  );
}

export default ReviewDetailPage;
