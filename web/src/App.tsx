import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";

import { api, ApiError } from "./api";
import { loadSavedCredentials, saveCredentials } from "./credentials";
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
} from "./utils";

type SessionState =
  | { phase: "checking" }
  | { phase: "guest"; message?: string }
  | { phase: "authenticated"; user: AuthUser };

type StreamState = "connecting" | "live" | "reconnecting";

const statusOrder: ExecutionStatus[] = [
  "queued",
  "running",
  "waiting_for_ci",
  "completed",
  "failed",
];

function Brand() {
  return (
    <div className="brand" aria-label="OpenReviewer">
      <div className="brand-mark-wrapper">
        <span className="brand-mark" aria-hidden="true">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="16 18 22 12 16 6" />
            <polyline points="8 6 2 12 8 18" />
            <line x1="14" y1="4" x2="10" y2="20" />
          </svg>
        </span>
        <span className="brand-pulse-ring" />
      </div>
      <div className="brand-titles">
        <div className="brand-row">
          <strong>OpenReviewer</strong>
          <span className="brand-tag">v0.2.0</span>
        </div>
        <small>AI 代码审查调度引擎</small>
      </div>
    </div>
  );
}

function LoadingScreen() {
  return (
    <main className="loading-screen">
      <div className="loading-content">
        <Brand />
        <div className="loading-bar-wrap">
          <div className="loading-bar-progress" />
        </div>
        <p className="loading-text">正在同步控制台运行状态…</p>
      </div>
    </main>
  );
}

interface LoginProps {
  initialMessage?: string;
  onAuthenticated: (user: AuthUser) => void;
}

function Login({ initialMessage, onAuthenticated }: LoginProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState(initialMessage ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [rememberCredentials, setRememberCredentials] = useState(true);

  useEffect(() => {
    let active = true;
    void loadSavedCredentials().then((saved) => {
      if (!active || !saved) return;
      setUsername((current) => current || saved.username);
      setPassword((current) => current || saved.password);
    });
    return () => {
      active = false;
    };
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setMessage("");
    try {
      const normalizedUsername = username.trim();
      const user = await api.login(normalizedUsername, password);
      if (rememberCredentials) {
        void saveCredentials(normalizedUsername, password);
      }
      setPassword("");
      onAuthenticated(user);
    } catch (error) {
      setPassword("");
      if (error instanceof ApiError && error.status === 401) {
        setMessage("账号或密码不正确，请重新输入");
      } else if (error instanceof ApiError && error.status === 429) {
        setMessage("尝试次数过多，请稍后再试");
      } else {
        setMessage("暂时无法登录，请检查网络后重试");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="login-page">
      {/* Dynamic Ambient Backdrops */}
      <div className="login-glow-1" />
      <div className="login-glow-2" />
      <div className="login-grid-bg" />

      {/* Left Showcase Side */}
      <section className="login-showcase">
        <div className="showcase-header">
          <Brand />
          <div className="system-pill">
            <span className="live-dot-pulse" />
            <span>M2 MILESTONE ACTIVE</span>
          </div>
        </div>

        <div className="showcase-hero">
          <div className="hero-badge">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
            </svg>
            下一代自动化 PR 审查调度系统
          </div>
          <h1>
            让每一次代码审查
            <br />
            <span className="gradient-text">清晰可见 · 稳如磐石</span>
          </h1>
          <p className="hero-desc">
            全流程状态流转追踪、Worker 心跳感知与幂等任务隔离。
            在 CI 构建边界前严密把控，为高可靠大模型审查奠定基石。
          </p>

          {/* Interactive Pipeline Showcase Mock */}
          <div className="showcase-pipeline">
            <div className="pipeline-title-bar">
              <span className="code-dot red" />
              <span className="code-dot yellow" />
              <span className="code-dot green" />
              <span className="pipeline-title-text">Review Pipeline Flow</span>
            </div>
            <div className="pipeline-steps">
              <div className="pipeline-node done">
                <span className="node-icon">✓</span>
                <div>
                  <strong>任务入队</strong>
                  <small>Idempotent Key</small>
                </div>
              </div>
              <div className="pipeline-arrow">➔</div>
              <div className="pipeline-node active">
                <span className="node-icon spin">⚙</span>
                <div>
                  <strong>Worker 调度</strong>
                  <small>Task Claim</small>
                </div>
              </div>
              <div className="pipeline-arrow">➔</div>
              <div className="pipeline-node waiting">
                <span className="node-icon">⏳</span>
                <div>
                  <strong>等待 CI 结果</strong>
                  <small>Waiting for CI</small>
                </div>
              </div>
              <div className="pipeline-arrow">➔</div>
              <div className="pipeline-node future">
                <span className="node-icon">✨</span>
                <div>
                  <strong>模型深度审查</strong>
                  <small>AI Review (M3)</small>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div className="showcase-footer">
          <div className="showcase-metric">
            <span className="metric-val">0ms</span>
            <span className="metric-lbl">SSE 流式同步延迟</span>
          </div>
          <div className="metric-divider" />
          <div className="showcase-metric">
            <span className="metric-val">100%</span>
            <span className="metric-lbl">幂等重放保护</span>
          </div>
          <div className="metric-divider" />
          <div className="showcase-metric">
            <span className="metric-val">AES-GCM</span>
            <span className="metric-lbl">凭据隔离保护</span>
          </div>
        </div>
      </section>

      {/* Right Login Form Side */}
      <section className="login-form-side">
        <div className="login-card-container">
          <form className="modern-auth-card" onSubmit={submit} autoComplete="on">
            <div className="auth-card-header">
              <div className="auth-icon-box">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 2a5 5 0 0 0-5 5v3H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-1V7a5 5 0 0 0-5-5zM9 7a3 3 0 0 1 6 0v3H9V7z"/>
                </svg>
              </div>
              <h2>控制台登录</h2>
              <p>请输入管理员身份凭据进入审查运行控制台</p>
            </div>

            <div className="auth-fields">
              <label className="modern-field">
                <span className="field-label">账号 / Username</span>
                <div className="modern-input-shell">
                  <svg className="input-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                    <circle cx="12" cy="7" r="4" />
                  </svg>
                  <input
                    name="username"
                    value={username}
                    onChange={(event) => setUsername(event.target.value)}
                    autoComplete="username"
                    maxLength={100}
                    placeholder="输入管理员账号"
                    required
                    autoFocus
                  />
                </div>
              </label>

              <label className="modern-field">
                <span className="field-label">密码 / Password</span>
                <div className="modern-input-shell">
                  <svg className="input-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
                    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                  </svg>
                  <input
                    name="password"
                    type="password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    autoComplete="current-password"
                    maxLength={512}
                    placeholder="••••••••••••"
                    required
                  />
                </div>
              </label>
            </div>

            <label className="modern-checkbox-row">
              <input
                type="checkbox"
                checked={rememberCredentials}
                onChange={(event) => setRememberCredentials(event.target.checked)}
              />
              <span className="custom-checkbox" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </span>
              <span className="checkbox-labels">
                <strong>记住登录状态</strong>
                <small>通过浏览器原生凭据库安全保存</small>
              </span>
            </label>

            {message && (
              <div className="auth-alert" role="alert" aria-live="polite">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <circle cx="12" cy="12" r="10" />
                  <line x1="12" y1="8" x2="12" y2="12" />
                  <line x1="12" y1="16" x2="12.01" y2="16" />
                </svg>
                <span>{message}</span>
              </div>
            )}

            <button className="modern-primary-button" disabled={submitting}>
              {submitting ? (
                <>
                  <span className="btn-spinner" />
                  正在验证身份…
                </>
              ) : (
                <>
                  <span>进入审查控制台</span>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                    <line x1="5" y1="12" x2="19" y2="12" />
                    <polyline points="12 5 19 12 12 19" />
                  </svg>
                </>
              )}
            </button>

            <div className="auth-card-footer">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
                <path d="M7 11V7a5 5 0 0 1 10 0v4" />
              </svg>
              <span>端到端安全隔离 · 密码不写入 localStorage</span>
            </div>
          </form>
          <p className="auth-footnote">OpenReviewer Infrastructure · Powered by NiuMa</p>
        </div>
      </section>
    </main>
  );
}

function StatusBadge({ status }: { status: ExecutionStatus }) {
  return (
    <span className={`status-badge-chip status-${status}`}>
      <span className="badge-glow-dot" />
      {statusLabels[status]}
    </span>
  );
}

function ReviewRow({ review }: { review: ReviewItem }) {
  return (
    <tr className="review-table-row">
      <td>
        <div className="repo-info-cell">
          <div className="repo-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
            </svg>
          </div>
          <div>
            <strong>{review.repository}</strong>
            <span className="run-id-tag">
              RUN-{review.review_run_id.slice(0, 8).toUpperCase()}
            </span>
          </div>
        </div>
      </td>
      <td>
        <span className="pr-badge">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="18" cy="18" r="3"/>
            <circle cx="6" cy="6" r="3"/>
            <path d="M13 6h3a2 2 0 0 1 2 2v7"/>
            <line x1="6" y1="9" x2="6" y2="21"/>
          </svg>
          #{review.pull_request_number}
        </span>
      </td>
      <td>
        <span className="sha-code-pill" title={review.head_sha}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="4" />
            <line x1="1.05" y1="12" x2="7" y2="12" />
            <line x1="17.01" y1="12" x2="22.96" y2="12" />
          </svg>
          <code>{shortSha(review.head_sha)}</code>
        </span>
      </td>
      <td>
        <StatusBadge status={review.execution_status} />
      </td>
      <td>
        <div className="attempts-cell">
          <span className="attempts-text">
            {review.attempt_count} / {review.max_attempts}
          </span>
          <div className="attempts-bar-track">
            <div
              className="attempts-bar-fill"
              style={{
                width: `${Math.min(100, (review.attempt_count / Math.max(1, review.max_attempts)) * 100)}%`,
              }}
            />
          </div>
        </div>
      </td>
      <td>
        <span className="time-cell">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="10" />
            <polyline points="12 6 12 12 16 14" />
          </svg>
          {formatDate(review.updated_at)}
        </span>
      </td>
    </tr>
  );
}

interface CreateReviewFormProps {
  onCreated: (message: string) => void;
  onUnauthorized: () => void;
}

function CreateReviewForm({ onCreated, onUnauthorized }: CreateReviewFormProps) {
  const [installationId, setInstallationId] = useState("");
  const [repositoryId, setRepositoryId] = useState("");
  const [repository, setRepository] = useState("lboverfys/NiuMa");
  const [pullRequest, setPullRequest] = useState("");
  const [headSha, setHeadSha] = useState("");
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // Quick Preset Helper
  function applyPreset(type: "niuma" | "demo") {
    if (type === "niuma") {
      setInstallationId("10001");
      setRepositoryId("20001");
      setRepository("lboverfys/NiuMa");
      setPullRequest("42");
      setHeadSha("a1b2c3d4e5f60718293a4b5c6d7e8f9012345678");
    } else {
      setInstallationId("10002");
      setRepositoryId("20002");
      setRepository("test-org/code-review-demo");
      setPullRequest("108");
      setHeadSha("fe98dc76ba543210fe98dc76ba543210fe98dc76");
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setMessage("");
    try {
      const result = await api.createReview(
        {
          installation_id: Number(installationId),
          repository_id: Number(repositoryId),
          repository,
          pull_request_number: Number(pullRequest),
          head_sha: headSha,
        },
        `manual:${crypto.randomUUID()}`,
      );
      setPullRequest("");
      setHeadSha("");
      const successMessage = `任务 ${result.review_task_id.slice(0, 8)} 已成功推入调度队列`;
      setMessage(successMessage);
      onCreated(successMessage);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        onUnauthorized();
        return;
      }
      const friendly =
        error instanceof ApiError && error.status === 422
          ? "输入内容不符合契约规范，请检查 ID 数值、仓库命名与 40 位 SHA"
          : errorMessage(error);
      setMessage(friendly);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="create-review-card" onSubmit={submit}>
      <div className="card-top-title">
        <div className="title-with-badge">
          <span className="card-icon-chip">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </span>
          <div>
            <h3>手动发起审查</h3>
            <p>向 Worker 分配新的 PR 审查任务</p>
          </div>
        </div>
      </div>

      {/* Fast fill presets */}
      <div className="presets-row">
        <span className="preset-label">快捷填入:</span>
        <button
          type="button"
          className="preset-pill"
          onClick={() => applyPreset("niuma")}
        >
          ⚡ NiuMa 主库
        </button>
        <button
          type="button"
          className="preset-pill"
          onClick={() => applyPreset("demo")}
        >
          🧪 Demo 样例
        </button>
      </div>

      <div className="form-fields-stack">
        <div className="two-cols-row">
          <label className="form-input-group">
            <span className="input-group-label">Installation ID</span>
            <input
              name="installation_id"
              type="number"
              min="1"
              step="1"
              placeholder="例如 10001"
              value={installationId}
              onChange={(event) => setInstallationId(event.target.value)}
              required
            />
          </label>
          <label className="form-input-group">
            <span className="input-group-label">Repository ID</span>
            <input
              name="repository_id"
              type="number"
              min="1"
              step="1"
              placeholder="例如 20001"
              value={repositoryId}
              onChange={(event) => setRepositoryId(event.target.value)}
              required
            />
          </label>
        </div>

        <label className="form-input-group">
          <span className="input-group-label">仓库名称 (Owner/Repo)</span>
          <input
            name="repository"
            value={repository}
            onChange={(event) => setRepository(event.target.value)}
            placeholder="如 lboverfys/NiuMa"
            pattern={"[A-Za-z0-9_.\\-]+/[A-Za-z0-9_.\\-]+"}
            required
          />
        </label>

        <label className="form-input-group">
          <span className="input-group-label">Pull Request 编号</span>
          <input
            name="pull_request_number"
            type="number"
            min="1"
            step="1"
            placeholder="例如 42"
            value={pullRequest}
            onChange={(event) => setPullRequest(event.target.value)}
            required
          />
        </label>

        <label className="form-input-group">
          <span className="input-group-label">Head SHA (40位哈希)</span>
          <input
            name="head_sha"
            className="code-font"
            value={headSha}
            onChange={(event) => setHeadSha(event.target.value)}
            minLength={40}
            maxLength={64}
            pattern="[0-9a-fA-F]{40,64}"
            placeholder="例如 a1b2c3d4..."
            required
          />
        </label>
      </div>

      <button className="submit-action-button" disabled={submitting}>
        {submitting ? (
          <>
            <span className="btn-spinner" />
            正在入队…
          </>
        ) : (
          <>
            <span>提交到审查队列</span>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <path d="M5 12h14M12 5l7 7-7 7" />
            </svg>
          </>
        )}
      </button>

      {message && (
        <div className="submit-feedback-toast" role="status" aria-live="polite">
          {message}
        </div>
      )}
    </form>
  );
}

interface DashboardProps {
  user: AuthUser;
  onSignedOut: (message?: string) => void;
}

function Dashboard({ user, onSignedOut }: DashboardProps) {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [streamState, setStreamState] = useState<StreamState>("connecting");
  const [pageMessage, setPageMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [activeFilter, setActiveFilter] = useState<string>("all");
  const [searchKeyword, setSearchKeyword] = useState<string>("");

  const refresh = useCallback(async () => {
    try {
      setSnapshot(await api.dashboard());
      setPageMessage("");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setPageMessage("暂时无法读取仪表盘，系统正在自动重试连接");
    } finally {
      setLoading(false);
    }
  }, [onSignedOut]);

  useEffect(() => {
    void refresh();
    const source = new EventSource("/api/v1/reviews/stream");
    source.onopen = () => {
      setStreamState("live");
    };
    source.addEventListener("dashboard", (event) => {
      try {
        setSnapshot(JSON.parse((event as MessageEvent<string>).data));
        setStreamState("live");
        setLoading(false);
      } catch {
        setStreamState("reconnecting");
      }
    });
    source.addEventListener("unavailable", () => {
      setStreamState("reconnecting");
    });
    source.onerror = () => {
      setStreamState("reconnecting");
    };
    return () => source.close();
  }, [refresh]);

  const statusCards = useMemo(
    () =>
      statusOrder.map((status) => ({
        status,
        count: snapshot?.status_counts[status] ?? 0,
      })),
    [snapshot],
  );

  // Filter reviews by status and search keyword
  const filteredReviews = useMemo(() => {
    if (!snapshot?.recent_reviews) return [];
    return snapshot.recent_reviews.filter((review) => {
      const matchStatus =
        activeFilter === "all" || review.execution_status === activeFilter;
      const matchKeyword =
        !searchKeyword.trim() ||
        review.repository.toLowerCase().includes(searchKeyword.toLowerCase()) ||
        String(review.pull_request_number).includes(searchKeyword) ||
        review.head_sha.toLowerCase().includes(searchKeyword.toLowerCase());
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

  const worker = snapshot?.worker;
  const workerHealthy = Boolean(worker?.configured && worker.online);

  return (
    <div className="dashboard-layout">
      {/* Top Navbar */}
      <header className="dashboard-navbar">
        <div className="nav-left-section">
          <Brand />
          <div className="nav-workspace-chip">
            <span className="slash-divider">/</span>
            <span className="workspace-icon">⚡</span>
            <span>Cluster: Default</span>
          </div>
        </div>

        <div className="nav-right-section">
          {/* Live SSE Stream Badge */}
          <div className={`live-telemetry-badge is-${streamState}`}>
            <span className="pulse-beacon" />
            <span className="telemetry-text">
              {streamState === "live"
                ? "SSE 实时流在线"
                : streamState === "connecting"
                  ? "正在建立连接"
                  : "正在尝试重连"}
            </span>
          </div>

          <div className="nav-separator" />

          {/* User Profile Chip */}
          <div className="user-profile-pill">
            <div className="user-avatar-gradient">
              {user.username.slice(0, 1).toUpperCase()}
            </div>
            <div className="user-meta">
              <span className="user-name">{user.username}</span>
              <span className="user-role">SUPER ADMIN</span>
            </div>
          </div>

          <button className="nav-icon-btn" onClick={logout} title="退出登录">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
              <polyline points="16 17 21 12 16 7"/>
              <line x1="21" y1="12" x2="9" y2="12"/>
            </svg>
          </button>
        </div>
      </header>

      <main className="dashboard-main-container">
        {/* Header Hero Section */}
        <section className="dashboard-hero-header">
          <div className="hero-text-block">
            <div className="hero-eyebrow-tag">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10" />
                <polyline points="12 6 12 12 14 14" />
              </svg>
              CONTROL TOWER
            </div>
            <h1>审查任务总控大厅</h1>
            <p>实时监控代码审查流水线、Worker 心跳探测、任务分发与重试状态</p>
          </div>

          <div className="hero-actions-block">
            <div className="sync-clock-card">
              <span className="clock-lbl">最近数据同步时间</span>
              <span className="clock-val">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/>
                </svg>
                {formatDate(snapshot?.generated_at ?? null)}
              </span>
            </div>

            <button
              className="refresh-circle-button"
              onClick={() => void refresh()}
              title="立即刷新仪表盘"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="23 4 23 10 17 10"/>
                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
              </svg>
              <span>刷新</span>
            </button>
          </div>
        </section>

        {pageMessage && (
          <div className="dashboard-toast-alert" role="alert">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
            <span>{pageMessage}</span>
          </div>
        )}

        {/* Visual Pipeline Stage Topology */}
        <section className="pipeline-topology-bar">
          <div className="topology-step-item">
            <div className="step-circle queued">1</div>
            <div className="step-text">
              <span className="step-name">排队中 (Queued)</span>
              <span className="step-count">{snapshot?.status_counts.queued ?? 0} 个任务</span>
            </div>
          </div>
          <div className="topology-line active" />
          <div className="topology-step-item">
            <div className="step-circle running">2</div>
            <div className="step-text">
              <span className="step-name">处理中 (Running)</span>
              <span className="step-count">{snapshot?.status_counts.running ?? 0} 个任务</span>
            </div>
          </div>
          <div className="topology-line active" />
          <div className="topology-step-item">
            <div className="step-circle waiting">3</div>
            <div className="step-text">
              <span className="step-name">等待 CI (Waiting CI)</span>
              <span className="step-count">{snapshot?.status_counts.waiting_for_ci ?? 0} 个任务</span>
            </div>
          </div>
          <div className="topology-line" />
          <div className="topology-step-item">
            <div className="step-circle completed">4</div>
            <div className="step-text">
              <span className="step-name">完成 / 归档 (Done)</span>
              <span className="step-count">{snapshot?.status_counts.completed ?? 0} 个任务</span>
            </div>
          </div>
        </section>

        {/* Telemetry Metrics Deck */}
        <section className="telemetry-deck" aria-busy={loading}>
          {/* Hero Metric Card */}
          <div
            className={`metric-glass-card hero-total ${activeFilter === "all" ? "is-filter-active" : ""}`}
            onClick={() => setActiveFilter("all")}
          >
            <div className="card-header-line">
              <span className="metric-chip-tag">TOTAL TASKS</span>
              <div className="metric-icon-bubble">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <polygon points="12 2 2 7 12 12 22 7 12 2"/>
                  <polyline points="2 17 12 22 22 17"/>
                  <polyline points="2 12 12 17 22 12"/>
                </svg>
              </div>
            </div>
            <div className="metric-number-hero">
              {loading ? "—" : snapshot?.total_reviews ?? 0}
            </div>
            <div className="metric-sub-footer">
              <span>全生命周期审查任务</span>
              <span className="filter-hint">点击重置筛选</span>
            </div>
          </div>

          {/* 5 Status Mini Cards */}
          {statusCards.map(({ status, count }) => (
            <div
              key={status}
              className={`metric-glass-card status-${status} ${activeFilter === status ? "is-filter-active" : ""}`}
              onClick={() =>
                setActiveFilter(activeFilter === status ? "all" : status)
              }
              title={`点击筛选 ${statusLabels[status]} 状态`}
            >
              <div className="card-header-line">
                <span className="status-dot-indicator" />
                <span className="metric-title">{statusLabels[status]}</span>
              </div>
              <div className="metric-number-value">{loading ? "—" : count}</div>
              <div className="metric-mini-bar">
                <div
                  className="mini-bar-fill"
                  style={{
                    width: `${Math.min(100, ((count || 0) / Math.max(1, snapshot?.total_reviews || 1)) * 100)}%`,
                  }}
                />
              </div>
            </div>
          ))}
        </section>

        {/* Main Grid: 2 Columns */}
        <div className="dashboard-grid-layout">
          {/* Left Large Column */}
          <div className="left-stream-column">
            {/* Worker Health Radar Panel */}
            <section className={`worker-radar-panel ${workerHealthy ? "is-online" : "is-offline"}`}>
              <div className="radar-left-side">
                <div className="orbital-node-container">
                  <div className="orbital-spinner-ring" />
                  <div className="orbital-core-chip">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
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

                <div className="worker-header-copy">
                  <div className="status-title-row">
                    <h3>{workerHealthy ? "Worker 节点在线" : "Worker 节点离线"}</h3>
                    <span className="live-status-pill">
                      <span className="dot" />
                      {workerHealthy ? "READY & POLLING" : "DISCONNECTED"}
                    </span>
                  </div>
                  <p className="worker-id-code">
                    <span>NODE ID:</span>
                    <code>{worker?.worker_id ?? "未接入任何 Worker 实例"}</code>
                  </p>
                </div>
              </div>

              <div className="radar-telemetry-cells">
                <div className="telemetry-item">
                  <span className="t-label">节点工作状态</span>
                  <strong className="t-val highlight">
                    {worker?.status ? workerLabels[worker.status] : "未就绪"}
                  </strong>
                </div>
                <div className="telemetry-item">
                  <span className="t-label">当前执行任务</span>
                  <strong className="t-val code-font">
                    {worker?.current_task_id
                      ? `TASK-${worker.current_task_id.slice(0, 8)}`
                      : "IDLE (无活跃任务)"}
                  </strong>
                </div>
                <div className="telemetry-item">
                  <span className="t-label">最近心跳回报</span>
                  <strong className="t-val">
                    {formatDate(worker?.last_seen_at ?? null)}
                  </strong>
                </div>
              </div>
            </section>

            {/* Task Stream Table Panel */}
            <section className="reviews-table-panel">
              <div className="table-action-toolbar">
                <div className="toolbar-left">
                  <div className="toolbar-heading">
                    <h3>实时审查任务流</h3>
                    <span className="total-badge">{filteredReviews.length} 条记录</span>
                  </div>

                  {/* Filter Pills */}
                  <div className="filter-pill-group">
                    <button
                      className={`filter-btn ${activeFilter === "all" ? "active" : ""}`}
                      onClick={() => setActiveFilter("all")}
                    >
                      全部
                    </button>
                    <button
                      className={`filter-btn ${activeFilter === "running" ? "active" : ""}`}
                      onClick={() => setActiveFilter("running")}
                    >
                      处理中
                    </button>
                    <button
                      className={`filter-btn ${activeFilter === "waiting_for_ci" ? "active" : ""}`}
                      onClick={() => setActiveFilter("waiting_for_ci")}
                    >
                      等待 CI
                    </button>
                    <button
                      className={`filter-btn ${activeFilter === "queued" ? "active" : ""}`}
                      onClick={() => setActiveFilter("queued")}
                    >
                      排队中
                    </button>
                    <button
                      className={`filter-btn ${activeFilter === "completed" ? "active" : ""}`}
                      onClick={() => setActiveFilter("completed")}
                    >
                      已完成
                    </button>
                  </div>
                </div>

                <div className="toolbar-right">
                  {/* Search Bar */}
                  <div className="table-search-box">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <circle cx="11" cy="11" r="8"/>
                      <line x1="21" y1="21" x2="16.65" y2="16.65"/>
                    </svg>
                    <input
                      type="text"
                      placeholder="搜索仓库、PR、SHA…"
                      value={searchKeyword}
                      onChange={(e) => setSearchKeyword(e.target.value)}
                    />
                    {searchKeyword && (
                      <button
                        className="clear-search-btn"
                        onClick={() => setSearchKeyword("")}
                      >
                        ✕
                      </button>
                    )}
                  </div>
                </div>
              </div>

              <div className="table-scroll-container">
                <table className="modern-data-table">
                  <thead>
                    <tr>
                      <th>仓库 / 运行批次</th>
                      <th>PR 编号</th>
                      <th>Head Commit SHA</th>
                      <th>流转状态</th>
                      <th>重试次数</th>
                      <th>最后更新</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredReviews.map((review) => (
                      <ReviewRow review={review} key={review.review_run_id} />
                    ))}
                  </tbody>
                </table>

                {!loading && filteredReviews.length === 0 && (
                  <div className="table-empty-hero">
                    <div className="empty-icon-orbit">
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
                        ? "当前筛选条件无结果，请尝试清除搜索词或切换状态分类。"
                        : "调度队列目前为空。请在右侧控制板提交第一条任务！"}
                    </p>
                    {(searchKeyword || activeFilter !== "all") && (
                      <button
                        className="clear-all-filters-btn"
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
              </div>
            </section>
          </div>

          {/* Right Sidebar Column */}
          <aside className="right-sidebar-column">
            {/* Create Task Form */}
            <CreateReviewForm
              onCreated={(message) => {
                setPageMessage(message);
                void refresh();
              }}
              onUnauthorized={() => onSignedOut("登录状态已失效，请重新登录")}
            />

            {/* Architecture Boundary Card */}
            <div className="architecture-boundary-card">
              <div className="boundary-card-top">
                <div className="shield-icon-badge">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
                  </svg>
                </div>
                <div>
                  <h4>M2 里程碑系统契约</h4>
                  <span className="contract-tag">SAFETY PROTOCOL</span>
                </div>
              </div>

              <div className="contract-points">
                <div className="contract-point">
                  <span className="point-dot green" />
                  <div>
                    <strong>Worker 容灾与恢复</strong>
                    <p>自动检测超时并触发指数退避重试</p>
                  </div>
                </div>
                <div className="contract-point">
                  <span className="point-dot yellow" />
                  <div>
                    <strong>CI 门禁等待安全区</strong>
                    <p>任务在 <code>waiting_for_ci</code> 挂起等待 GitHub 验证</p>
                  </div>
                </div>
                <div className="contract-point">
                  <span className="point-dot purple" />
                  <div>
                    <strong>M3 路线图规划</strong>
                    <p>接入 LLM 模型推理与 GitHub Comment 自动回写</p>
                  </div>
                </div>
              </div>
            </div>
          </aside>
        </div>
      </main>
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState<SessionState>({ phase: "checking" });

  useEffect(() => {
    let active = true;
    api
      .me()
      .then((user) => {
        if (active) setSession({ phase: "authenticated", user });
      })
      .catch((error) => {
        if (!active) return;
        setSession({
          phase: "guest",
          message:
            error instanceof ApiError && error.status === 401
              ? undefined
              : "服务暂时不可用，请稍后重试",
        });
      });
    return () => {
      active = false;
    };
  }, []);

  if (session.phase === "checking") return <LoadingScreen />;
  if (session.phase === "guest") {
    return (
      <Login
        initialMessage={session.message}
        onAuthenticated={(user) => setSession({ phase: "authenticated", user })}
      />
    );
  }
  return (
    <Dashboard
      user={session.user}
      onSignedOut={(message) => setSession({ phase: "guest", message })}
    />
  );
}
