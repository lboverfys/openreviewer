import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, ApiError, DASHBOARD_CACHE_TTL_MS, peekReadCache, primeReadCache } from "./api";
import { Brand } from "./Auth";
import CreateReviewForm from "./CreateReviewForm";
import {
  appendReviewPage,
  applyLiveDashboardSnapshot,
  DASHBOARD_FALLBACK_REFRESH_MS,
  DASHBOARD_INITIAL_FALLBACK_MS,
  DASHBOARD_STATUS_ORDER,
} from "./dashboard";
import type { DashboardRefreshOptions, DashboardStreamState } from "./dashboard";
import { hasPermission, roleLabels } from "./rbac";
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
    <span className={`warm-status-pill status-${status}`}>
      <span className="pill-dot" />
      {label ?? statusLabels[status]}
    </span>
  );
}
function ReviewRow({ review, onOpen }: { review: ReviewItem; onOpen: (reviewRunId: string) => void }) {
  const hasBranchRoute = Boolean(review.head_ref || review.base_ref);
  const headRepository = review.head_repository ?? review.repository;
  const baseRepository = review.base_repository ?? review.repository;

  return (
    <tr className="warm-table-row">
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
              <span className="warm-pr-badge">PR #{review.pull_request_number}</span>
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
              <span className="warm-sha-chip" title={review.head_sha}>
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
              <strong>{headRepository}</strong>
              <code>{review.head_ref ?? "未知分支"}</code>
            </div>
            <span className="review-branch-arrow" aria-hidden="true">→</span>
            <div className="review-branch-endpoint is-base">
              <span>目标</span>
              <strong>{baseRepository}</strong>
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
          <small className="warm-row-substatus warm-row-findings">
            {review.unreviewed_finding_count > 0
              ? `${review.unreviewed_finding_count} 条待裁决问题`
              : `${review.finding_count} 条审查问题`}
          </small>
        )}
        {review.last_error && (
          <small className="warm-row-error" title={review.last_error}>
            {review.last_error}
          </small>
        )}
      </td>
      <td>
        <div className="warm-attempts-track-block">
          <div className="attempts-num">
            <span>{review.attempt_count}</span>/{review.max_attempts}
          </div>
          <div className="warm-progress-bar-bg">
            <div
              className="warm-progress-bar-fill"
              style={{
                width: `${Math.min(100, (review.attempt_count / Math.max(1, review.max_attempts)) * 100)}%`,
              }}
            />
          </div>
        </div>
      </td>
      <td>
        <span className="warm-time-badge">
          {formatDate(review.updated_at)}
        </span>
      </td>
      <td>
        <button
          type="button"
          className="review-open-row-btn"
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
  onOpenSettings: () => void;
  onOpenKnowledge: () => void;
  onOpenRetrieval?: () => void;
  onOpenReview: (reviewRunId: string) => void;
}

function Dashboard({ user, onSignedOut, onOpenSettings, onOpenKnowledge, onOpenRetrieval, onOpenReview }: DashboardProps) {
  const cachedSnapshot = peekReadCache<DashboardSnapshot>("dashboard:first:50");
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(cachedSnapshot ?? null);
  const [streamState, setStreamState] = useState<DashboardStreamState>("connecting");
  const [pageMessage, setPageMessage] = useState("");
  const [loading, setLoading] = useState(cachedSnapshot === undefined);
  const [loadingMore, setLoadingMore] = useState(false);
  const [activeFilter, setActiveFilter] = useState<string>("all");
  const [searchKeyword, setSearchKeyword] = useState<string>("");
  const refreshSequence = useRef(0);
  const streamLiveRef = useRef(false);
  const refresh = useCallback(async ({
    signal,
    force = false,
    preserveLiveSnapshot = false,
  }: DashboardRefreshOptions = {}) => {
    const sequence = ++refreshSequence.current;
    try {
      const next = await api.dashboard(undefined, 50, signal, force);
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
          const cachedBeforeEvent = peekReadCache<DashboardSnapshot>("dashboard:first:50");
          if (!cachedBeforeEvent || incoming.generated_at >= cachedBeforeEvent.generated_at) {
            primeReadCache("dashboard:first:50", incoming, DASHBOARD_CACHE_TTL_MS);
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

  const filteredReviews = useMemo(() => {
    if (!snapshot?.recent_reviews) return [];
    const keyword = searchKeyword.trim().toLocaleLowerCase();
    return snapshot.recent_reviews.filter((review) => {
      const matchStatus =
        activeFilter === "all" || review.execution_status === activeFilter;
      const matchKeyword =
        !keyword || [
          review.repository,
          String(review.pull_request_number),
          review.head_sha,
          review.pr_title ?? "",
          review.pr_author_login ?? "",
          review.head_repository ?? "",
          review.head_ref ?? "",
          review.base_repository ?? "",
          review.base_ref ?? "",
        ].some((value) => value.toLocaleLowerCase().includes(keyword));
      return matchStatus && matchKeyword;
    });
  }, [snapshot, activeFilter, searchKeyword]);

  async function logout() {
    try {
      await api.logout();
    } finally {
      onSignedOut();
    }
  }

  async function loadMoreReviews() {
    const cursor = snapshot?.next_cursor;
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.reviews(cursor);
      setSnapshot((current) => (current ? appendReviewPage(current, page) : current));
      setPageMessage("");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setPageMessage("暂时无法加载更多审查任务，请稍后重试");
    } finally {
      setLoadingMore(false);
    }
  }

  const workers = snapshot?.workers?.length
    ? snapshot.workers
    : snapshot?.worker.configured
      ? [snapshot.worker]
      : [];
  const onlineWorkers = workers.filter((item) => item.online);
  const worker = onlineWorkers[0] ?? workers[0] ?? snapshot?.worker;
  const workerHealthy = onlineWorkers.length > 0;
  const canManageReviews = hasPermission(user, "reviews:manage");
  const canManageSettings = hasPermission(user, "settings:manage");
  const canManageKnowledge = hasPermission(user, "knowledge:manage");

  return (
    <div className="bento-dashboard-layout">
      {/* Consolidated High-Efficiency Top Navigation */}
      <header className="bento-top-navbar">
        <div className="top-nav-left">
          <Brand />
          <div className="cluster-tag-chip">
            <span className="sparkle-symbol">⚡</span>
            <span>Cluster: Default</span>
          </div>
        </div>

        <div className="top-nav-center">
          <div className="page-crumb-tag">
            <span className="crumb-dot" />
            <strong>审查控制台</strong>
            <span className="crumb-slash">/</span>
            <span>任务监控</span>
          </div>
        </div>

        <div className="top-nav-right">
          {/* Live Telemetry Pill */}
          <div className={`bento-stream-pill state-${streamState}`}>
            <span className="beacon-circle" />
            <span className="beacon-label">
              {streamState === "live"
                ? "SSE 实时同步中"
                : streamState === "connecting"
                  ? "建立连接中"
                  : "正在重连"}
            </span>
          </div>

          <div className="nav-clock-tag" title="最近快照同步时间">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" />
              <polyline points="12 6 12 12 16 14" />
            </svg>
            <span>{formatDate(snapshot?.generated_at ?? null)}</span>
          </div>

          <button
            type="button"
            className="bento-refresh-icon-btn"
            onClick={() => void refresh({ force: true })}
            title="刷新数据快照"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
              <polyline points="23 4 23 10 17 10"/>
              <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
            </svg>
            <span>刷新</span>
          </button>

          {canManageKnowledge && onOpenRetrieval && <button type="button" className="bento-settings-icon-btn" onClick={onOpenRetrieval}>代码检索</button>}
          {canManageKnowledge && (
          <button
            type="button"
            className="bento-settings-icon-btn"
            onClick={onOpenKnowledge}
            title="管理 RAG 知识库"
            aria-label="管理 RAG 知识库"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
          </button>
          )}

          {canManageSettings && (
          <button
            type="button"
            className="bento-settings-icon-btn"
            onClick={onOpenSettings}
            title="AI 运行设置"
            aria-label="打开 AI 运行设置"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56V21h-4v-.08A1.7 1.7 0 0 0 8.96 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.56-1.03H3v-4h.08A1.7 1.7 0 0 0 4.6 8.96a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 8.96 4.6 1.7 1.7 0 0 0 10 3.08V3h4v.08a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9c.14.6.67 1.02 1.29 1.03H21v4h-.31c-.62 0-1.15.42-1.29 1.03Z" />
            </svg>
          </button>
          )}

          <div className="bento-nav-divider" />

          {/* User Profile */}
          <div className="bento-user-pill">
            <div className="user-avatar-sun">
              {user.username.slice(0, 1).toUpperCase()}
            </div>
            <span className="user-identity">
              <span className="user-username">{user.username}</span>
              <small>{roleLabels[user.role]}</small>
            </span>
          </div>

          <button type="button" className="bento-exit-btn" onClick={logout} title="退出登录">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
              <polyline points="16 17 21 12 16 7"/>
              <line x1="21" y1="12" x2="9" y2="12"/>
            </svg>
          </button>
        </div>
      </header>

      <main className="bento-main-viewport">
        {pageMessage && (
          <div className="bento-toast-banner" role="alert">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
            <span>{pageMessage}</span>
          </div>
        )}

        {/* Top Bento Deck: Worker Node + Interactive Telemetry Pipeline */}
        <section className="bento-deck-row">
          {/* Worker Node Card (Left 32%) */}
          <div className={`bento-worker-unit ${workerHealthy ? "is-ready" : "is-offline"}`}>
            <div className="worker-header-bar">
              <div className="worker-core-badge">
                <div className="core-orbit-halo" />
                <div className="core-chip-icon">
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
              </div>

              <div className="worker-title-area">
                <div className="worker-name-line">
                  <h4>Worker 智能执行节点</h4>
                  <span className="worker-status-badge">
                    <span className="status-ping-dot" />
                    {workerHealthy
                      ? `${onlineWorkers.length}/${workers.length} ONLINE`
                      : "OFFLINE"}
                  </span>
                </div>
                <span className="worker-node-id">
                  {workers.length > 1
                    ? `${workers.length} 个执行节点`
                    : worker?.worker_id ?? "未接入节点实例"}
                </span>
              </div>
            </div>

            <div className="worker-specs-grid">
              <div className="spec-cell">
                <span className="spec-label">当前运行状态</span>
                <strong className="spec-val highlight-orange">
                  {worker?.status ? workerLabels[worker.status] : "未就绪"}
                </strong>
              </div>
              <div className="spec-cell">
                <span className="spec-label">当前执行任务</span>
                <strong className="spec-val code-font">
                  {worker?.current_task_id
                    ? `TASK #${worker.current_task_id.slice(0, 8)}`
                    : "IDLE (空闲)"}
                </strong>
              </div>
              <div className="spec-cell">
                <span className="spec-label">最近心跳时间</span>
                <strong className="spec-val">
                  {formatDate(worker?.last_seen_at ?? null)}
                </strong>
              </div>
            </div>
            {workers.length > 1 && (
              <div className="worker-node-list" aria-label="Worker 节点列表">
                {workers.map((item) => (
                  <div className="worker-node-row" key={item.worker_id ?? "unknown"}>
                    <span className={item.online ? "is-online" : "is-offline"} />
                    <code>{item.worker_id}</code>
                    <strong>{item.status ? workerLabels[item.status] : "未知"}</strong>
                    <small>{formatDate(item.last_seen_at)}</small>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Unified Pipeline & Metrics Deck (Right 68%) */}
          <div className="bento-telemetry-unit">
            <div className="telemetry-unit-header">
              <div className="th-heading">
                <h4>全流水线任务状态</h4>
                <span className="total-count-pill">
                  总计 <strong>{snapshot?.total_reviews ?? 0}</strong> 个任务
                </span>
              </div>
              <span className="th-click-hint">点击卡片可快速筛选表格</span>
            </div>

            <div className="telemetry-cards-row">
              {/* All Total Card */}
              <div
                className={`telemetry-pill-box total-box ${activeFilter === "all" ? "is-active-tab" : ""}`}
                onClick={() => setActiveFilter("all")}
              >
                <div className="box-top">
                  <span className="box-tag">ALL</span>
                  <span className="box-name">全部任务</span>
                </div>
                <div className="box-number">{loading ? "—" : snapshot?.total_reviews ?? 0}</div>
              </div>

              {/* 5 Status Mini Cards */}
              {statusCards.map(({ status, count }) => (
                <div
                  key={status}
                  className={`telemetry-pill-box status-pill-${status} ${activeFilter === status ? "is-active-tab" : ""}`}
                  onClick={() =>
                    setActiveFilter(activeFilter === status ? "all" : status)
                  }
                  title={`筛选 ${statusLabels[status]} 状态`}
                >
                  <div className="box-top">
                    <span className="status-mini-orb" />
                    <span className="box-name">{statusLabels[status]}</span>
                  </div>
                  <div className="box-number">{loading ? "—" : count}</div>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* Main Content: 70% Table + 30% Sidebar Form */}
        <div className={`bento-content-columns ${canManageReviews ? "" : "is-read-only"}`}>
          {/* Left Table Section */}
          <div className="bento-table-column">
            <section className="bento-table-card">
              {/* Integrated Toolbar */}
              <div className="bento-table-toolbar">
                <div className="toolbar-left-group">
                  <h3>实时审查流水线</h3>
                  <div className="filter-pill-capsules">
                    <button
                      type="button"
                      className={`pill-btn ${activeFilter === "all" ? "active" : ""}`}
                      onClick={() => setActiveFilter("all")}
                    >
                      全部 ({snapshot?.total_reviews ?? 0})
                    </button>
                    <button
                      type="button"
                      className={`pill-btn ${activeFilter === "running" ? "active" : ""}`}
                      onClick={() => setActiveFilter("running")}
                    >
                      处理中 ({snapshot?.status_counts.running ?? 0})
                    </button>
                    <button
                      type="button"
                      className={`pill-btn ${activeFilter === "waiting_for_ci" ? "active" : ""}`}
                      onClick={() => setActiveFilter("waiting_for_ci")}
                    >
                      等待 CI ({snapshot?.status_counts.waiting_for_ci ?? 0})
                    </button>
                    <button
                      type="button"
                      className={`pill-btn ${activeFilter === "ready_for_review" ? "active" : ""}`}
                      onClick={() => setActiveFilter("ready_for_review")}
                    >
                      等待 AI ({snapshot?.status_counts.ready_for_review ?? 0})
                    </button>
                    <button
                      type="button"
                      className={`pill-btn ${activeFilter === "queued" ? "active" : ""}`}
                      onClick={() => setActiveFilter("queued")}
                    >
                      排队中 ({snapshot?.status_counts.queued ?? 0})
                    </button>
                    <button
                      type="button"
                      className={`pill-btn ${activeFilter === "completed" ? "active" : ""}`}
                      onClick={() => setActiveFilter("completed")}
                    >
                      已完成 ({snapshot?.status_counts.completed ?? 0})
                    </button>
                  </div>
                </div>

                <div className="toolbar-right-group">
                  <div className="bento-search-box">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                      <circle cx="11" cy="11" r="8"/>
                      <line x1="21" y1="21" x2="16.65" y2="16.65"/>
                    </svg>
                    <input
                      id="review-search"
                      name="review-search"
                      type="text"
                      placeholder="搜索标题、作者、仓库或分支…"
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

              {/* Scroller Table */}
              <div className="bento-table-wrap">
                <table className="bento-data-table">
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

                {!loading && filteredReviews.length === 0 && (
                  <div className="bento-empty-view">
                    <div className="empty-sun-bubble">
                      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
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
                        className="reset-pill-btn"
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
                {!loading && snapshot && snapshot.recent_reviews.length > 0 && (
                  <div className="bento-pagination-bar">
                    <span>
                      已加载 {snapshot.recent_reviews.length} / {snapshot.total_reviews}
                    </span>
                    {snapshot.next_cursor && (
                      <button
                        type="button"
                        disabled={loadingMore}
                        onClick={() => void loadMoreReviews()}
                      >
                        {loadingMore ? "加载中..." : "加载更多"}
                      </button>
                    )}
                  </div>
                )}
              </div>
            </section>
          </div>

          {/* Right Launchpad Sidebar */}
          {canManageReviews && (
          <aside className="bento-sidebar-column">
            <CreateReviewForm
              onCreated={(message) => {
                setPageMessage(message);
                void refresh({ force: true });
              }}
              onUnauthorized={() => onSignedOut("登录状态已失效，请重新登录")}
            />
          </aside>
          )}
        </div>
      </main>
    </div>
  );
}

export default Dashboard;
