import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, ApiError, DASHBOARD_CACHE_TTL_MS, peekReadCache, primeReadCache, subscribeReadCache, reviewListKey } from "./api";
import CreateReviewForm from "./CreateReviewForm";
import Pagination, { PAGE_SIZE } from "./Pagination";
import { useCursorPage } from "./useCursorPage";
import { preloadPage } from "./page-loaders";
import {
  applyLiveDashboardSnapshot,
  DASHBOARD_FALLBACK_REFRESH_MS,
  DASHBOARD_INITIAL_FALLBACK_MS,
  DASHBOARD_STATUS_ORDER,
} from "./dashboard";
import type { DashboardRefreshOptions, DashboardStreamState } from "./dashboard";
import { hasPermission } from "./rbac";
import { WorkspaceHeader } from "./Workspace";
import type {
  AuthUser,
  DashboardSnapshot,
  ExecutionStatus,
  ReviewItem,
} from "./types";
import {
  errorMessage,
  formatDate,
  shortSha,
  statusLabels,
  workerLabels,
  reviewDisplayLabel,
} from "./utils";
function StatusBadge({ status, label }: { status: ExecutionStatus; label?: string }) {
  return (
    <span className={`status-pill status-${status}`}>
      <span className="pill-dot" />
      {label ?? statusLabels[status]}
    </span>
  );
}

function greetingByHour(): string {
  const hour = new Date().getHours();
  if (hour < 6) return "夜深了";
  if (hour < 12) return "上午好";
  if (hour < 18) return "下午好";
  return "晚上好";
}

const statCardIcons: Record<string, string> = {
  all: "▣",
  queued: "⏳",
  running: "⚡",
  waiting_for_ci: "🛠",
  ready_for_review: "🤖",
  completed: "✓",
  failed: "!",
};

function ReviewRow({ review, onOpen }: { review: ReviewItem; onOpen: (reviewRunId: string) => void }) {
  const hasBranchRoute = Boolean(review.head_ref || review.base_ref);
  const headRepository = review.head_repository ?? review.repository;
  const baseRepository = review.base_repository ?? review.repository;
  const modelAttempted = review.model_attempt_count > 0;
  const attempts = modelAttempted ? review.model_attempt_count : review.attempt_count;

  return (
    <tr className="dash-table-row">
      <td className="review-identity-column">
        <div className="table-review-identity">
          <div className="repo-avatar-icon">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
            </svg>
          </div>
          <div className="review-identity-copy">
            <div className="review-repository-line">
              <strong>{review.repository}</strong>
              <span className="dash-pr-badge">PR #{review.pull_request_number}</span>
              {review.pr_html_url && (
                <a
                  className="review-github-link"
                  href={review.pr_html_url}
                  target="_blank"
                  rel="noreferrer"
                  aria-label={`在 GitHub 查看 PR #${review.pull_request_number}`}
                  title="在 GitHub 查看 Pull Request"
                >
                  <span aria-hidden="true">↗</span>
                </a>
              )}
            </div>
            <span className="review-list-title">
              {review.pr_title || `Pull Request #${review.pull_request_number}`}
            </span>
            <div className="review-list-byline">
              <span className={review.pr_author_login ? "review-author" : "is-muted"}>
                {review.pr_author_login ? `@${review.pr_author_login}` : "作者信息未同步"}
              </span>
              <span className="run-id-pill">运行 {review.review_run_id.slice(0, 8).toUpperCase()}</span>
              <span className="dash-sha-chip" title={review.head_sha}>
                SHA <code>{shortSha(review.head_sha)}</code>
              </span>
            </div>
          </div>
        </div>
      </td>
      <td className="review-branch-column">
        {hasBranchRoute ? (
          <div className="review-branch-route" title={`${headRepository}:${review.head_ref ?? "?"} → ${baseRepository}:${review.base_ref ?? "?"}`}>
            <div className="review-branch-endpoint is-head">
              <span>来源</span>
              <code>{review.head_ref ?? "未知分支"}</code>
            </div>
            <span className="review-branch-arrow" aria-hidden="true">→</span>
            <div className="review-branch-endpoint is-base">
              <span>目标</span>
              <code>{review.base_ref ?? "未知分支"}</code>
            </div>
          </div>
        ) : (
          <span className="review-branch-missing">历史任务未记录分支</span>
        )}
      </td>
      <td>
        <StatusBadge status={review.execution_status} label={reviewDisplayLabel(review)} />
        {review.finding_count > 0 && (
          <small className="dash-row-substatus dash-row-findings">
            {review.unreviewed_finding_count > 0
              ? `${review.unreviewed_finding_count} 条待裁决问题`
              : `${review.finding_count} 条审查问题`}
          </small>
        )}
        {review.last_error && (
          <small className="dash-row-error" title={review.last_error}>
            {review.last_error}
          </small>
        )}
      </td>
      <td>
        <div className="dash-attempts-track-block" title={modelAttempted ? "模型阶段尝试次数" : "准备阶段尝试次数"}>
          <div className="attempts-num">
            <span>{attempts}</span>/{review.max_attempts}
          </div>
          <div className="dash-progress-bar-bg">
            <div
              className="dash-progress-bar-fill"
              style={{
                width: `${Math.min(100, (attempts / Math.max(1, review.max_attempts)) * 100)}%`,
              }}
            />
          </div>
        </div>
      </td>
      <td>
        <span className="dash-time-badge">
          {formatDate(review.updated_at)}
        </span>
      </td>
      <td>
        <button
          type="button"
          className="review-open-row-btn"
          onPointerEnter={() => preloadPage("review")}
          onFocus={() => preloadPage("review")}
          onClick={() => onOpen(review.review_run_id)}
          title="查看任务详情、结果和日志"
        >
          查看详情 <span aria-hidden="true">→</span>
        </button>
      </td>
    </tr>
  );
}

interface DashboardProps {
  user: AuthUser;
  onSignedOut: (message?: string) => void;
  onOpenReview: (reviewRunId: string) => void;
}

function Dashboard({ user, onSignedOut, onOpenReview }: DashboardProps) {
  const cachedSnapshot = peekReadCache<DashboardSnapshot>("dashboard:first:10");
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(cachedSnapshot ?? null);
  const [streamState, setStreamState] = useState<DashboardStreamState>("connecting");
  const [pageMessage, setPageMessage] = useState("");
  const [loading, setLoading] = useState(cachedSnapshot === undefined);
  const [activeFilter, setActiveFilter] = useState<string>("all");
  const [searchKeyword, setSearchKeyword] = useState<string>("");
  const [creatingReview, setCreatingReview] = useState(false);
  const refreshSequence = useRef(0);
  const streamLiveRef = useRef(false);
  const refresh = useCallback(async ({
    signal,
    force = false,
    preserveLiveSnapshot = false,
  }: DashboardRefreshOptions = {}) => {
    const sequence = ++refreshSequence.current;
    try {
      const next = await api.dashboard(undefined, PAGE_SIZE, signal, force);
      // SSE 首次事件可能比 HTTP 快；不要让较晚返回的旧快照覆盖实时数据。
      if (signal?.aborted || sequence !== refreshSequence.current) return;
      if (!preserveLiveSnapshot || !streamLiveRef.current) {
        setSnapshot((current) => applyLiveDashboardSnapshot(current, next));
      }
      setPageMessage("");
    } catch (error) {
      if (signal?.aborted || sequence !== refreshSequence.current) return;
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setPageMessage("暂时无法读取仪表盘，系统正在自动重试连接");
    } finally {
      if (!signal?.aborted && sequence === refreshSequence.current) setLoading(false);
    }
  }, [onSignedOut]);

  useEffect(() => {
    const controller = new AbortController();
    streamLiveRef.current = false;
    let source: EventSource | null = null;
    let disposed = false;
    let initialSnapshotReceived = false;
    let fallbackInFlight = false;
    let fallbackTimer: number | undefined;

    const clearFallback = () => {
      if (fallbackTimer !== undefined) {
        window.clearTimeout(fallbackTimer);
        fallbackTimer = undefined;
      }
    };
    const scheduleFallback = (delay = DASHBOARD_INITIAL_FALLBACK_MS) => {
      if (
        disposed
        || initialSnapshotReceived
        || fallbackInFlight
        || fallbackTimer !== undefined
        || document.visibilityState === "hidden"
      ) return;
      fallbackTimer = window.setTimeout(() => {
        fallbackTimer = undefined;
        if (
          disposed
          || initialSnapshotReceived
          || fallbackInFlight
          || document.visibilityState === "hidden"
        ) return;
        fallbackInFlight = true;
        void refresh({
          signal: controller.signal,
          preserveLiveSnapshot: true,
        }).finally(() => {
          fallbackInFlight = false;
          // SSE 不可用时继续用低频 HTTP 快照维持页面可用；一旦收到
          // SSE 快照，事件处理器会置位并取消后续兜底请求。
          if (!disposed && !initialSnapshotReceived) {
            scheduleFallback(DASHBOARD_FALLBACK_REFRESH_MS);
          }
        });
      }, delay);
    };

    const disconnect = () => {
      source?.close();
      source = null;
    };
    const connect = () => {
      if (disposed || document.visibilityState === "hidden" || source) return;
      const nextSource = new EventSource("/api/v1/reviews/stream");
      source = nextSource;
      nextSource.onopen = () => {
        setStreamState("live");
      };
      nextSource.addEventListener("dashboard", (event) => {
        if (disposed || controller.signal.aborted) return;
        try {
          const incoming = JSON.parse(
            (event as MessageEvent<string>).data,
          ) as DashboardSnapshot;
          initialSnapshotReceived = true;
          clearFallback();
          streamLiveRef.current = true;
          const cachedBeforeEvent = peekReadCache<DashboardSnapshot>("dashboard:first:10");
          if (!cachedBeforeEvent || incoming.generated_at >= cachedBeforeEvent.generated_at) {
            primeReadCache("dashboard:first:10", incoming, DASHBOARD_CACHE_TTL_MS);
            primeReadCache(`${reviewListKey()}:first`, {items: incoming.recent_reviews, total: incoming.total_reviews, next_cursor: incoming.next_cursor}, DASHBOARD_CACHE_TTL_MS);
          }
          setSnapshot((current) => applyLiveDashboardSnapshot(current, incoming));
          setStreamState("live");
          setLoading(false);
        } catch {
          setStreamState("reconnecting");
        }
      });
      nextSource.addEventListener("unavailable", () => {
        initialSnapshotReceived = false;
        streamLiveRef.current = false;
        setStreamState("reconnecting");
        scheduleFallback(0);
      });
      nextSource.addEventListener("auth-expired", () => {
        disconnect();
        onSignedOut("登录状态已失效，请重新登录");
      });
      nextSource.onerror = () => {
        // EventSource 会自行尝试重连；重连期间仍用低频 HTTP 快照保持
        // 数据新鲜，不能因为此前收到过首帧就永久关闭兜底。
        initialSnapshotReceived = false;
        streamLiveRef.current = false;
        setStreamState("reconnecting");
        scheduleFallback(0);
      };
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "hidden") {
        disconnect();
        clearFallback();
        // 页面隐藏期间 EventSource 会被主动关闭。下次恢复时必须重新等待
        // 首个实时快照；否则旧会话已经收到过快照，scheduleFallback(0)
        // 会被 initialSnapshotReceived 拦截，SSE 重连失败时页面就会一直
        // 停留在旧数据。
        initialSnapshotReceived = false;
        streamLiveRef.current = false;
        setStreamState("reconnecting");
      } else {
        // 先重置首帧标记，再建立新 SSE。这样无论新连接最终成功还是
        // 失败，首帧超时后的 HTTP 兜底都会重新生效。
        initialSnapshotReceived = false;
        streamLiveRef.current = false;
        connect();
        scheduleFallback(0);
      }
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    connect();
    scheduleFallback();
    return () => {
      disposed = true;
      controller.abort();
      clearFallback();
      disconnect();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [refresh]);

  const statusCards = useMemo(
    () =>
      DASHBOARD_STATUS_ORDER.map((status) => ({
        status,
        count: snapshot?.status_counts[status] ?? 0,
      })),
    [snapshot],
  );

  const [debouncedSearch, setDebouncedSearch] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(searchKeyword.trim().length >= 3 || /^\d+$/.test(searchKeyword.trim()) ? searchKeyword.trim() : ""), 300);
    return () => window.clearTimeout(timer);
  }, [searchKeyword]);
  const handleListError = useCallback((error: unknown) => {
    if (error instanceof ApiError && error.status === 401) onSignedOut("登录状态已失效，请重新登录");
    else setPageMessage(errorMessage(error));
  }, [onSignedOut]);
  const loadReviewPage = useCallback((cursor?: string, signal?: AbortSignal, force = false) =>
    api.reviews(cursor, PAGE_SIZE, signal, activeFilter, debouncedSearch, force),
  [activeFilter, debouncedSearch]);
  const reviewPage = useCursorPage<ReviewItem>({
    cacheKey: reviewListKey(activeFilter, debouncedSearch), load: loadReviewPage, onError: handleListError,
  });
  const filteredReviews = reviewPage.data?.items ?? [];

  useEffect(() => subscribeReadCache<DashboardSnapshot>("dashboard:first:10", (next) => {
    setSnapshot((current) => applyLiveDashboardSnapshot(current, next));
    setLoading(false);
  }), []);

  const workers = snapshot?.workers?.length
    ? snapshot.workers
    : snapshot?.worker.configured
      ? [snapshot.worker]
      : [];
  const onlineWorkers = workers.filter((item) => item.online && item.status !== "stopping");
  const workerPreview = onlineWorkers.slice(0, 3);
  const onlineCount = snapshot?.worker_online_count ?? onlineWorkers.length;
  const busyCount = snapshot?.worker_busy_count ?? onlineWorkers.filter(item => item.status === "busy").length;
  const worker = onlineWorkers[0] ?? workers[0] ?? snapshot?.worker;
  const workerHealthy = onlineCount > 0;
  const canManageReviews = hasPermission(user, "reviews:manage");
  const canInspectWorkers = hasPermission(user, "settings:manage");

  if (creatingReview && canManageReviews) return <main className="workspace-page">
    <WorkspaceHeader title="手动发起审查" icon="review" description="补充手动审查请求，日常 PR 继续通过 GitHub 事件触发。" />
    <div className="dash-create-view"><CreateReviewForm
      onCancel={() => setCreatingReview(false)}
      onCreated={message => { setCreatingReview(false); setPageMessage(message); reviewPage.reset(); void reviewPage.refresh(); }}
      onUnauthorized={() => onSignedOut("登录状态已失效，请重新登录")}
    /></div>
  </main>;

  return (
    <div className="dash-shell">
      <main className="dash-main">
        {pageMessage && (
          <div className="toast-banner" role="alert">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
            <span>{pageMessage}</span>
          </div>
        )}

        {/* 概览条：问候 + 任务概况 + 同步状态与刷新，单行紧凑 */}
        <section className="dash-hero">
          <div className="dash-hero-copy">
            <h1>{greetingByHour()}，{user.username}</h1>
            <p>共 {snapshot?.total_reviews ?? 0} 个任务 · {onlineCount} 个 Worker 节点在线</p>
          </div>
          <div className="dash-hero-actions">
            {canManageReviews && <button type="button" className="btn-aurora dash-create-button" onClick={() => setCreatingReview(true)}><span aria-hidden="true">＋</span>发起审查</button>}
            <div className={`dash-stream-pill state-${streamState}`}>
              <span className="beacon-circle" />
              <span className="beacon-label">
                {streamState === "live"
                  ? "实时同步中"
                  : streamState === "connecting"
                    ? "建立连接中"
                    : "正在重连"}
              </span>
            </div>
            <div className="dash-snapshot-chip" title="最近快照同步时间">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10" />
                <polyline points="12 6 12 12 16 14" />
              </svg>
              <span>快照 {formatDate(snapshot?.generated_at ?? null)}</span>
            </div>
            <button
              type="button"
              className="btn-ghost"
              onClick={() => { void reviewPage.refresh(); if (reviewPage.cursor || activeFilter !== "all" || debouncedSearch) void refresh({ force: true }); }}
              title="刷新数据快照"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <polyline points="23 4 23 10 17 10"/>
                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
              </svg>
              <span>刷新数据</span>
            </button>
          </div>
        </section>

        {/* 状态统计轨道：7 张卡，点击筛选表格 */}
        <section className="dash-stat-rail" aria-label="任务状态统计">
          <div
            className={`dash-stat-card total-card ${activeFilter === "all" ? "is-active-tab" : ""}`}
            onClick={() => setActiveFilter("all")}
          >
            <div className="stat-card-top">
              <span className="stat-icon-chip" aria-hidden="true">{statCardIcons.all}</span>
              <span className="stat-card-name">全部任务</span>
            </div>
            <div className="stat-card-number">{loading ? "—" : snapshot?.total_reviews ?? 0}</div>
            <div className="stat-card-bar" aria-hidden="true"><span style={{ width: "100%" }} /></div>
          </div>
          {statusCards.map(({ status, count }) => {
            const total = snapshot?.total_reviews ?? 0;
            const percent = total > 0 ? Math.round((count / total) * 100) : 0;
            return (
              <div
                key={status}
                className={`dash-stat-card status-card-${status} ${activeFilter === status ? "is-active-tab" : ""}`}
                onClick={() =>
                  setActiveFilter(activeFilter === status ? "all" : status)
                }
                title={`筛选 ${statusLabels[status]} 状态`}
              >
                <div className="stat-card-top">
                  <span className="stat-icon-chip" aria-hidden="true">{statCardIcons[status]}</span>
                  <span className="stat-card-name">{statusLabels[status]}</span>
                </div>
                <div className="stat-card-number">{loading ? "—" : count}</div>
                <div className="stat-card-bar" aria-hidden="true"><span style={{ width: `${percent}%` }} /></div>
              </div>
            );
          })}
        </section>

        {/* 主区域：表格 + 右侧栏（Worker + 发起审查） */}
        <div className={`dash-columns ${canManageReviews ? "" : "is-read-only"}`}>
          <section className="dash-table-card panel-card">
            <div className="dash-table-toolbar">
              <div className="toolbar-left-group">
                <h3>实时审查流水线</h3>
              </div>

              <div className="toolbar-right-group">
                <div className="dash-search-box">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                    <circle cx="11" cy="11" r="8"/>
                    <line x1="21" y1="21" x2="16.65" y2="16.65"/>
                  </svg>
                  <input
                    id="review-search"
                    name="review-search"
                    type="text"
                    placeholder="标题、作者、分支（至少3字）或 PR 编号"
                    value={searchKeyword}
                    onChange={(e) => setSearchKeyword(e.target.value)}
                  />
                  {searchKeyword && (
                    <button
                      type="button"
                      className="clear-x-btn"
                      onClick={() => setSearchKeyword("")}
                    >
                      ✕
                    </button>
                  )}
                </div>
              </div>
            </div>

            <div className="dash-table-wrap">
              <table className="dash-data-table">
                <thead>
                  <tr>
                    <th>Pull Request</th>
                    <th>合并方向</th>
                    <th>流转状态</th>
                    <th>重试次数</th>
                    <th>更新时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredReviews.map((review) => (
                    <ReviewRow review={review} key={review.review_run_id} onOpen={onOpenReview} />
                  ))}
                </tbody>
              </table>

              {!loading && !reviewPage.loading && filteredReviews.length === 0 && (
                <div className="empty-block">
                  <div className="empty-icon-bubble">
                    <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                      <circle cx="12" cy="12" r="10" />
                      <path d="M16 16s-1.5-2-4-2-4 2-4 2" />
                      <line x1="9" y1="9" x2="9.01" y2="9" />
                      <line x1="15" y1="9" x2="15.01" y2="9" />
                    </svg>
                  </div>
                  <h4>暂无匹配的审查任务</h4>
                  <p>
                    {searchKeyword || activeFilter !== "all"
                      ? "当前筛选条件下无记录，您可以重置筛选或清除搜索关键词。"
                      : "调度队列当前为空，请在右侧提交一条新的审查任务！"}
                  </p>
                  {(searchKeyword || activeFilter !== "all") && (
                    <button
                      type="button"
                      className="btn-ghost"
                      onClick={() => {
                        setSearchKeyword("");
                        setActiveFilter("all");
                      }}
                    >
                      重置所有筛选
                    </button>
                  )}
                </div>
              )}
              <Pagination page={reviewPage.page} count={filteredReviews.length} total={reviewPage.data?.total}
                hasNext={Boolean(reviewPage.data?.next_cursor)} busy={reviewPage.loading}
                onPrevious={reviewPage.previous} onNext={reviewPage.next} label="审查任务分页" />

            </div>
          </section>

          <aside className="dash-side-rail">
            {/* Worker 节点卡 */}
            <section className={`dash-worker-card panel-card ${workerHealthy ? "is-ready" : "is-offline"}`}>
              <div className="worker-header-bar">
                <div className="worker-core-badge">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <rect x="4" y="4" width="16" height="16" rx="2" />
                    <rect x="9" y="9" width="6" height="6" />
                    <line x1="9" y1="1" x2="9" y2="4" />
                    <line x1="15" y1="1" x2="15" y2="4" />
                    <line x1="9" y1="20" x2="9" y2="23" />
                    <line x1="15" y1="20" x2="15" y2="23" />
                    <line x1="20" y1="9" x2="23" y2="9" />
                    <line x1="20" y1="14" x2="23" y2="14" />
                    <line x1="1" y1="9" x2="4" y2="9" />
                    <line x1="1" y1="14" x2="4" y2="14" />
                  </svg>
                </div>
                <div className="worker-title-area">
                  <div className="worker-name-line">
                    <h4>Worker 执行节点</h4>
                    <span className="worker-status-badge">
                      <span className="status-ping-dot" />
                      {workerHealthy
                        ? `${onlineCount} ONLINE`
                        : "OFFLINE"}
                    </span>
                  </div>
                  <span className="worker-node-id">
                    {canInspectWorkers ? "当前活跃状态 · 历史记录在运行诊断中查看" : "当前活跃节点状态"}
                  </span>
                </div>
              </div>

              <div className="worker-specs-grid worker-overview-counts">
                <div className="spec-cell">
                  <span className="spec-label">在线节点</span>
                  <strong className="spec-val">
                    {onlineCount}
                  </strong>
                </div>
                <div className="spec-cell">
                  <span className="spec-label">忙碌节点</span>
                  <strong className="spec-val">
                    {busyCount}
                  </strong>
                </div>
              </div>
              {workerPreview.length > 0 && (
                <div className="worker-node-list" aria-label="Worker 节点列表">
                  {workerPreview.map((item) => (
                    <div className="worker-node-row" key={item.worker_id ?? "unknown"}>
                      <span className={item.online ? "is-online" : "is-offline"} />
                      <code>{item.worker_id}</code>
                      <strong>{item.status ? workerLabels[item.status] : "未知"}</strong>
                      <small title={formatDate(item.last_seen_at)}>{item.last_seen_at ? new Date(item.last_seen_at).toLocaleTimeString("zh-CN", { hour12: false }) : "—"}</small>
                    </div>
                  ))}
                </div>
              )}
              {!workerHealthy && <p className="worker-preview-note">暂无在线节点。{worker?.last_seen_at && <>最后心跳：{formatDate(worker.last_seen_at)}。</>}</p>}
              {onlineCount > workerPreview.length && <p className="worker-preview-note">首页仅展示前 {workerPreview.length} 个在线节点。</p>}
              {canInspectWorkers && <a className="worker-history-link" href="#platform?tab=diagnostics">查看节点与历史 →</a>}
            </section>
          </aside>
        </div>
      </main>
    </div>
  );
}

export default Dashboard;
