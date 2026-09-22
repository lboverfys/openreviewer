import { TableRow, TableCell, Table, TableHeader, TableHead, TableBody } from "./components/ui/table";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { Notice } from "./Feedback";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError, DASHBOARD_CACHE_TTL_MS, peekReadCache, primeReadCache, subscribeReadCache, reviewListKey } from "./api";
import CreateReviewForm from "./CreateReviewForm";
import Pagination, { PAGE_SIZE } from "./Pagination";
import { useCursorPage } from "./useCursorPage";
import { preloadPage } from "./page-loaders";
import {
  applyLiveDashboardSnapshot,
  DASHBOARD_FALLBACK_REFRESH_MS,
  DASHBOARD_INITIAL_FALLBACK_MS,
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
  reviewDisplayLabel,
  reviewDisplayStatus,
} from "./utils";
function StatusBadge({ status, label }: { status: ExecutionStatus; label?: string }) {
  return (
    <span className={`status-pill status-${status}`}>
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
    <TableRow className="dash-table-row">
      <TableCell className="review-identity-column">
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
              {review.snapshot_review && <span className="dash-pr-badge">历史复查</span>}
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
      </TableCell>
      <TableCell className="review-branch-column">
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
      </TableCell>
      <TableCell>
        <StatusBadge status={reviewDisplayStatus(review)} label={reviewDisplayLabel(review)} />
        {review.last_error && reviewDisplayStatus(review) === "failed" && (
          <small className="dash-row-error" title={review.last_error}>
            {review.last_error}
          </small>
        )}
      </TableCell>
      <TableCell className="review-result-column">
        {review.model_review_completed_at ? <><strong>{review.finding_count} 条候选问题</strong>
          <small>{review.unreviewed_finding_count > 0 ? review.unreviewed_finding_count + " 条待核对" : "查看详情核对检查范围"}</small></>
          : <span className="is-muted">{review.execution_status === "cancelled" ? "已停止" : reviewDisplayStatus(review) === "completed" ? "旧记录未保存 AI 结果" : "尚未产出结果"}</span>}
      </TableCell>
      <TableCell>
        <span className="dash-time-badge">
          {formatDate(review.updated_at)}
        </span>
      </TableCell>
      <TableCell>
        <Button variant="outline"
          type="button"
          className="review-open-row-btn"
          onPointerEnter={() => preloadPage("review")}
          onFocus={() => preloadPage("review")}
          onClick={() => onOpen(review.review_run_id)}
          title="查看任务详情、结果和日志"
        >
          查看详情 <span aria-hidden="true">→</span>
        </Button>
      </TableCell>
    </TableRow>
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

  const onlineCount = snapshot?.worker_online_count ?? (snapshot?.worker.online ? 1 : 0);
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
          <Notice onDismiss={() => setPageMessage("")}>{pageMessage}</Notice>
        )}

        <section className="dash-hero">
          <div className="dash-hero-copy">
            <h1>审查任务</h1>
            <p>共 {snapshot?.total_reviews ?? 0} 条审查记录。打开任务查看进度、问题和处理结果。</p>
          </div>
          <div className="dash-hero-actions">
            {canManageReviews && <Button type="button" className="dash-create-button" onClick={() => setCreatingReview(true)}><span aria-hidden="true">＋</span>管理员补录</Button>}
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
            <Button variant="outline"
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
            </Button>
          </div>
        </section>

        <section className="dash-status-filters" aria-label="任务状态筛选">
          {([["all", "全部"], ["running", "执行中"], ["awaiting_approval", "待核对"], ["awaiting_publish", "待发布"],
            ["paused", "已暂停"], ["failed", "失败"], ["cancelled", "已取消"], ["completed", "AI 已结束"]] as const).map(([value, label]) =>
              <Button variant="outline" type="button" key={value} aria-pressed={activeFilter === value} onClick={() => setActiveFilter(value)}>{label}</Button>)}
          {canInspectWorkers && <a href="#platform?tab=diagnostics">{loading ? "正在连接后台…" : onlineCount > 0 ? "后台可用" : "后台暂无在线节点"} →</a>}
        </section>

        {/* 主区域：表格 + 右侧栏（Worker + 发起审查） */}
        <div className={`dash-columns ${canManageReviews ? "" : "is-read-only"}`}>
          <section className="dash-table-card panel-card">
            <div className="dash-table-toolbar">
              <div className="toolbar-left-group">
                <h3>PR 审查记录</h3>
              </div>

              <div className="toolbar-right-group">
                <div className="dash-search-box">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                    <circle cx="11" cy="11" r="8"/>
                    <line x1="21" y1="21" x2="16.65" y2="16.65"/>
                  </svg>
                  <Input
                    id="review-search"
                    name="review-search"
                    type="text"
                    aria-label="搜索审查任务"
                    placeholder="关键词至少3字或 PR 编号"
                    title="标题、作者和分支至少输入 3 个字，也可按 PR 编号搜索"
                    value={searchKeyword}
                    onChange={(e) => setSearchKeyword(e.target.value)}
                  />
                  {searchKeyword && (
                    <Button variant="outline"
                      type="button"
                      className="clear-x-btn"
                      aria-label="清空搜索"
                      onClick={() => setSearchKeyword("")}
                    >
                      ✕
                    </Button>
                  )}
                </div>
              </div>
            </div>

            <div className="dash-table-wrap">
              <Table className="dash-data-table">
                <TableHeader>
                  <TableRow>
                    <TableHead>Pull Request</TableHead>
                    <TableHead>合并方向</TableHead>
                    <TableHead>当前状态</TableHead>
                    <TableHead>审查结果</TableHead>
                    <TableHead>更新时间</TableHead>
                    <TableHead>操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredReviews.map((review) => (
                    <ReviewRow review={review} key={review.review_run_id} onOpen={onOpenReview} />
                  ))}
                </TableBody>
              </Table>

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
                      : "在已接入的 GitHub 项目提交 PR 后，审查记录会自动出现在这里。"}
                  </p>
                  {(searchKeyword || activeFilter !== "all") && (
                    <Button variant="outline"
                      type="button"
                      className="btn-ghost"
                      onClick={() => {
                        setSearchKeyword("");
                        setActiveFilter("all");
                      }}
                    >
                      重置所有筛选
                    </Button>
                  )}
                </div>
              )}
              <Pagination page={reviewPage.page} count={filteredReviews.length} total={reviewPage.data?.total}
                hasNext={Boolean(reviewPage.data?.next_cursor)} busy={reviewPage.loading}
                onPrevious={reviewPage.previous} onNext={reviewPage.next} label="审查任务分页" />

            </div>
          </section>


        </div>
      </main>
    </div>
  );
}

export default Dashboard;
