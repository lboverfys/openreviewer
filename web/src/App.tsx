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
      <div className="brand-icon-sunburst">
        <span className="brand-core-icon" aria-hidden="true">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="16 18 22 12 16 6" />
            <polyline points="8 6 2 12 8 18" />
            <line x1="14" y1="4" x2="10" y2="20" />
          </svg>
        </span>
        <span className="sunburst-ring" />
      </div>
      <div className="brand-text-block">
        <div className="brand-main-row">
          <strong>OpenReviewer</strong>
          <span className="brand-version-badge">v0.2.0</span>
        </div>
        <small>AI 代码审查与调度中枢</small>
      </div>
    </div>
  );
}

function LoadingScreen() {
  return (
    <main className="loading-screen">
      <div className="loading-card">
        <Brand />
        <div className="loading-track">
          <div className="loading-spark" />
        </div>
        <p className="loading-hint">正在唤醒 OpenReviewer 运行空间…</p>
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
    <main className="warm-login-page">
      {/* Background Animated Blobs */}
      <div className="blob-warm-1" />
      <div className="blob-warm-2" />
      <div className="blob-warm-3" />
      <div className="warm-dot-pattern" />

      {/* Left Showcase Side */}
      <section className="warm-showcase-panel">
        <div className="showcase-topbar">
          <Brand />
          <div className="milestone-pill">
            <span className="live-orange-dot" />
            <span>M2 · 调度中枢已就绪</span>
          </div>
        </div>

        <div className="showcase-content">
          <div className="feature-highlight-chip">
            <span className="sparkle-icon">✨</span>
            <span>下一代智能化代码审查调度控制台</span>
          </div>

          <h1>
            让每一次代码审查
            <br />
            <span className="warm-gradient-title">清晰、可靠、有迹可循</span>
          </h1>

          <p className="showcase-subtitle">
            全流程状态跃迁可视化、Worker 实时心跳感知与幂等任务隔离。
            为自动化 PR 审查、模型推理与协同反馈提供坚实稳定的工程底座。
          </p>

          {/* Interactive Visual Pipeline Flow */}
          <div className="pipeline-interactive-card">
            <div className="pipeline-header">
              <div className="mac-dots">
                <span className="dot d-red" />
                <span className="dot d-yellow" />
                <span className="dot d-green" />
              </div>
              <span className="pipeline-name">Workflow Live Pipeline</span>
              <span className="flow-badge">ACTIVE</span>
            </div>

            <div className="pipeline-flow-steps">
              <div className="flow-step-item is-done">
                <div className="step-badge">✓</div>
                <div className="step-info">
                  <strong>任务生成</strong>
                  <small>Idempotent Key</small>
                </div>
              </div>
              <div className="step-connector active" />
              <div className="flow-step-item is-running">
                <div className="step-badge spin-gear">⚙</div>
                <div className="step-info">
                  <strong>Worker 分发</strong>
                  <small>Dispatching</small>
                </div>
              </div>
              <div className="step-connector active" />
              <div className="flow-step-item is-waiting">
                <div className="step-badge pulse-scale">⏳</div>
                <div className="step-info">
                  <strong>等待 CI 结果</strong>
                  <small>Waiting for CI</small>
                </div>
              </div>
              <div className="step-connector" />
              <div className="flow-step-item is-future">
                <div className="step-badge">🚀</div>
                <div className="step-info">
                  <strong>模型深度审查</strong>
                  <small>AI Analysis</small>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div className="showcase-stats-bar">
          <div className="stat-unit">
            <strong className="stat-number">0 ms</strong>
            <span className="stat-desc">SSE 实时流式延迟</span>
          </div>
          <div className="stat-divider" />
          <div className="stat-unit">
            <strong className="stat-number">100%</strong>
            <span className="stat-desc">幂等重试保护</span>
          </div>
          <div className="stat-divider" />
          <div className="stat-unit">
            <strong className="stat-number">AES-GCM</strong>
            <span className="stat-desc">安全凭据管理</span>
          </div>
        </div>
      </section>

      {/* Right Login Card Side */}
      <section className="warm-login-form-side">
        <div className="warm-auth-wrapper">
          <form className="warm-glass-auth-card" onSubmit={submit} autoComplete="on">
            <div className="auth-card-top">
              <div className="auth-avatar-box">
                <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 2a5 5 0 0 0-5 5v3H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-1V7a5 5 0 0 0-5-5zM9 7a3 3 0 0 1 6 0v3H9V7z"/>
                </svg>
              </div>
              <h2>欢迎回来</h2>
              <p>请登录管理员账号进入控制台</p>
            </div>

            <div className="auth-form-body">
              <label className="warm-input-field">
                <span className="input-title">管理员账号</span>
                <div className="warm-input-box">
                  <svg className="field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
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

              <label className="warm-input-field">
                <span className="input-title">安全密码</span>
                <div className="warm-input-box">
                  <svg className="field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
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

              <label className="warm-checkbox-field">
                <input
                  type="checkbox"
                  checked={rememberCredentials}
                  onChange={(event) => setRememberCredentials(event.target.checked)}
                />
                <span className="checkbox-custom-square" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                </span>
                <div className="checkbox-text-pair">
                  <strong>记住登录状态</strong>
                  <small>交由浏览器原生密码库安全存储</small>
                </div>
              </label>

              {message && (
                <div className="warm-alert-banner" role="alert" aria-live="polite">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                    <circle cx="12" cy="12" r="10" />
                    <line x1="12" y1="8" x2="12" y2="12" />
                    <line x1="12" y1="16" x2="12.01" y2="16" />
                  </svg>
                  <span>{message}</span>
                </div>
              )}

              <button className="warm-primary-btn" disabled={submitting}>
                {submitting ? (
                  <>
                    <span className="warm-btn-spinner" />
                    正在认证中…
                  </>
                ) : (
                  <>
                    <span>进入审查控制台</span>
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                      <line x1="5" y1="12" x2="19" y2="12" />
                      <polyline points="12 5 19 12 12 19" />
                    </svg>
                  </>
                )}
              </button>
            </div>

            <div className="auth-card-subfoot">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              </svg>
              <span>安全环境校验通过 · 零 localStorage 明文留存</span>
            </div>
          </form>
          <p className="warm-page-footnote">OpenReviewer Operations · Powered by NiuMa</p>
        </div>
      </section>
    </main>
  );
}

function StatusBadge({ status }: { status: ExecutionStatus }) {
  return (
    <span className={`warm-status-pill status-${status}`}>
      <span className="pill-dot" />
      {statusLabels[status]}
    </span>
  );
}

function ReviewRow({ review }: { review: ReviewItem }) {
  return (
    <tr className="warm-table-row">
      <td>
        <div className="table-repo-block">
          <div className="repo-avatar-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
            </svg>
          </div>
          <div className="repo-name-stack">
            <strong>{review.repository}</strong>
            <span className="run-id-pill">
              RUN #{review.review_run_id.slice(0, 8).toUpperCase()}
            </span>
          </div>
        </div>
      </td>
      <td>
        <span className="warm-pr-badge">
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
        <span className="warm-sha-chip" title={review.head_sha}>
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
        <div className="warm-attempts-track-block">
          <div className="attempts-num">
            <span>{review.attempt_count}</span> / {review.max_attempts}
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
      const successMessage = `任务 ${result.review_task_id.slice(0, 8)} 已成功调度入队`;
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
    <form className="warm-create-card" onSubmit={submit}>
      <div className="create-card-header">
        <div className="icon-badge-warm">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4">
            <path d="M12 5v14M5 12h14" />
          </svg>
        </div>
        <div>
          <h3>发起审查任务</h3>
          <p>提交 PR 请求至 Worker 调度队列</p>
        </div>
      </div>

      {/* Quick Fill Presets */}
      <div className="preset-quick-row">
        <span className="preset-lead-tag">一键填入:</span>
        <button
          type="button"
          className="preset-btn"
          onClick={() => applyPreset("niuma")}
        >
          ⚡ NiuMa 主库
        </button>
        <button
          type="button"
          className="preset-btn"
          onClick={() => applyPreset("demo")}
        >
          🧪 Demo 样例
        </button>
      </div>

      <div className="form-inputs-group">
        <div className="two-cols-inputs">
          <label className="warm-input-control">
            <span>Installation ID</span>
            <input
              name="installation_id"
              type="number"
              min="1"
              step="1"
              placeholder="例: 10001"
              value={installationId}
              onChange={(event) => setInstallationId(event.target.value)}
              required
            />
          </label>
          <label className="warm-input-control">
            <span>Repository ID</span>
            <input
              name="repository_id"
              type="number"
              min="1"
              step="1"
              placeholder="例: 20001"
              value={repositoryId}
              onChange={(event) => setRepositoryId(event.target.value)}
              required
            />
          </label>
        </div>

        <label className="warm-input-control">
          <span>目标仓库 (Owner/Repository)</span>
          <input
            name="repository"
            value={repository}
            onChange={(event) => setRepository(event.target.value)}
            placeholder="例如: lboverfys/NiuMa"
            pattern={"[A-Za-z0-9_.\\-]+/[A-Za-z0-9_.\\-]+"}
            required
          />
        </label>

        <label className="warm-input-control">
          <span>Pull Request 编号</span>
          <input
            name="pull_request_number"
            type="number"
            min="1"
            step="1"
            placeholder="例如: 42"
            value={pullRequest}
            onChange={(event) => setPullRequest(event.target.value)}
            required
          />
        </label>

        <label className="warm-input-control">
          <span>Head Commit SHA (40位哈希)</span>
          <input
            name="head_sha"
            className="code-font"
            value={headSha}
            onChange={(event) => setHeadSha(event.target.value)}
            minLength={40}
            maxLength={64}
            pattern="[0-9a-fA-F]{40,64}"
            placeholder="例如: a1b2c3d4e5f6..."
            required
          />
        </label>
      </div>

      <button className="warm-submit-btn" disabled={submitting}>
        {submitting ? (
          <>
            <span className="warm-btn-spinner" />
            正在排队提交…
          </>
        ) : (
          <>
            <span>提交审查任务</span>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <path d="M5 12h14M12 5l7 7-7 7" />
            </svg>
          </>
        )}
      </button>

      {message && (
        <div className="warm-feedback-badge" role="status" aria-live="polite">
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
    <div className="warm-dashboard-app">
      {/* Top Navbar */}
      <header className="warm-topbar">
        <div className="topbar-brand-section">
          <Brand />
          <div className="topbar-cluster-tag">
            <span className="tag-sparkle">⚡</span>
            <span>Default Cluster</span>
          </div>
        </div>

        <div className="topbar-right-controls">
          {/* Live Telemetry Pill */}
          <div className={`warm-telemetry-indicator stream-${streamState}`}>
            <span className="telemetry-beacon-glow" />
            <span className="telemetry-label">
              {streamState === "live"
                ? "SSE 实时流在线"
                : streamState === "connecting"
                  ? "正在建立流连接"
                  : "正在重连中"}
            </span>
          </div>

          <div className="warm-topbar-divider" />

          {/* User Badge */}
          <div className="warm-user-capsule">
            <div className="warm-user-avatar">
              {user.username.slice(0, 1).toUpperCase()}
            </div>
            <div className="warm-user-details">
              <span className="user-title">{user.username}</span>
              <span className="user-badge">ADMIN</span>
            </div>
          </div>

          <button className="warm-logout-btn" onClick={logout} title="退出登录">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
              <polyline points="16 17 21 12 16 7"/>
              <line x1="21" y1="12" x2="9" y2="12"/>
            </svg>
            <span>退出</span>
          </button>
        </div>
      </header>

      <main className="warm-dashboard-viewport">
        {/* Hero Header Section */}
        <section className="warm-hero-section">
          <div className="hero-titles-wrap">
            <div className="hero-orange-eyebrow">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <circle cx="12" cy="12" r="10" />
                <polyline points="12 6 12 12 14 14" />
              </svg>
              <span>OPERATIONS DASHBOARD</span>
            </div>
            <h1>审查任务总控大厅</h1>
            <p>实时掌控智能代码审查流程、Worker 心跳探测、任务分发与重试状态</p>
          </div>

          <div className="hero-right-cards">
            <div className="sync-timestamp-box">
              <span className="sync-label">最近同步时间</span>
              <strong className="sync-time-str">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/>
                </svg>
                {formatDate(snapshot?.generated_at ?? null)}
              </strong>
            </div>

            <button
              className="warm-refresh-btn"
              onClick={() => void refresh()}
              title="立即刷新快照"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <polyline points="23 4 23 10 17 10"/>
                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
              </svg>
              <span>刷新快照</span>
            </button>
          </div>
        </section>

        {pageMessage && (
          <div className="warm-page-toast" role="alert">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
            <span>{pageMessage}</span>
          </div>
        )}

        {/* Visual Pipeline Topology Flow Track */}
        <section className="warm-topology-card">
          <div className="topology-step-cell step-queued">
            <div className="step-num-bubble">1</div>
            <div className="step-content">
              <strong>1. 排队中 (Queued)</strong>
              <small>{snapshot?.status_counts.queued ?? 0} 个任务等待处理</small>
            </div>
          </div>
          <div className="topology-line-active" />
          <div className="topology-step-cell step-running">
            <div className="step-num-bubble">2</div>
            <div className="step-content">
              <strong>2. 处理中 (Running)</strong>
              <small>{snapshot?.status_counts.running ?? 0} 个任务正在执行</small>
            </div>
          </div>
          <div className="topology-line-active" />
          <div className="topology-step-cell step-waiting">
            <div className="step-num-bubble">3</div>
            <div className="step-content">
              <strong>3. 等待 CI (Waiting CI)</strong>
              <small>{snapshot?.status_counts.waiting_for_ci ?? 0} 个任务等待 CI</small>
            </div>
          </div>
          <div className="topology-line-subtle" />
          <div className="topology-step-cell step-completed">
            <div className="step-num-bubble">4</div>
            <div className="step-content">
              <strong>4. 完成归档 (Done)</strong>
              <small>{snapshot?.status_counts.completed ?? 0} 个任务已完成</small>
            </div>
          </div>
        </section>

        {/* Telemetry Metric Cards Deck */}
        <section className="warm-metrics-deck" aria-busy={loading}>
          {/* Main Total Card */}
          <div
            className={`metric-interactive-card card-total ${activeFilter === "all" ? "is-selected" : ""}`}
            onClick={() => setActiveFilter("all")}
          >
            <div className="card-top-tag">
              <span className="tag-text">TOTAL REVIEWS</span>
              <div className="icon-pill-warm">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <polygon points="12 2 2 7 12 12 22 7 12 2"/>
                  <polyline points="2 17 12 22 22 17"/>
                  <polyline points="2 12 12 17 22 12"/>
                </svg>
              </div>
            </div>
            <div className="metric-large-number">
              {loading ? "—" : snapshot?.total_reviews ?? 0}
            </div>
            <div className="metric-footer-note">
              <span>全量生命周期任务</span>
              <span className="reset-hint">点击显示全部</span>
            </div>
          </div>

          {/* 5 Status Mini Cards */}
          {statusCards.map(({ status, count }) => (
            <div
              key={status}
              className={`metric-interactive-card status-card-${status} ${activeFilter === status ? "is-selected" : ""}`}
              onClick={() =>
                setActiveFilter(activeFilter === status ? "all" : status)
              }
              title={`点击联动筛选 ${statusLabels[status]} 任务`}
            >
              <div className="card-top-tag">
                <span className="status-orb" />
                <span className="tag-title">{statusLabels[status]}</span>
              </div>
              <div className="metric-status-number">{loading ? "—" : count}</div>
              <div className="status-progress-track">
                <div
                  className="status-progress-fill"
                  style={{
                    width: `${Math.min(100, ((count || 0) / Math.max(1, snapshot?.total_reviews || 1)) * 100)}%`,
                  }}
                />
              </div>
            </div>
          ))}
        </section>

        {/* 2-Column Responsive Main Grid */}
        <div className="warm-main-grid">
          {/* Left Column: Worker Node & Table */}
          <div className="warm-left-col">
            {/* Worker Node Telemetry Deck */}
            <section className={`warm-worker-card ${workerHealthy ? "node-healthy" : "node-offline"}`}>
              <div className="worker-left-block">
                <div className="worker-sunburst-node">
                  <div className="node-orbit-ring" />
                  <div className="node-center-chip">
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

                <div className="worker-title-copy">
                  <div className="worker-status-header">
                    <h3>{workerHealthy ? "Worker 智能节点在线" : "Worker 智能节点离线"}</h3>
                    <span className="worker-state-tag">
                      <span className="live-mini-dot" />
                      {workerHealthy ? "POLLING & READY" : "OFFLINE"}
                    </span>
                  </div>
                  <p className="worker-node-id-str">
                    <span>NODE ID:</span>
                    <code>{worker?.worker_id ?? "未接入任何 Worker 实例"}</code>
                  </p>
                </div>
              </div>

              <div className="worker-right-metrics">
                <div className="worker-metric-item">
                  <span className="wm-label">节点运行状态</span>
                  <strong className="wm-val highlight-orange">
                    {worker?.status ? workerLabels[worker.status] : "未就绪"}
                  </strong>
                </div>
                <div className="worker-metric-item">
                  <span className="wm-label">当前执行任务</span>
                  <strong className="wm-val code-font">
                    {worker?.current_task_id
                      ? `TASK-${worker.current_task_id.slice(0, 8)}`
                      : "IDLE (空闲)"}
                  </strong>
                </div>
                <div className="worker-metric-item">
                  <span className="wm-label">最近心跳回报</span>
                  <strong className="wm-val">
                    {formatDate(worker?.last_seen_at ?? null)}
                  </strong>
                </div>
              </div>
            </section>

            {/* Task Stream Table Deck */}
            <section className="warm-table-card">
              <div className="table-header-toolbar">
                <div className="th-left">
                  <div className="th-title-group">
                    <h3>实时审查流水线</h3>
                    <span className="th-count-pill">{filteredReviews.length} 条</span>
                  </div>

                  {/* Filter Pills */}
                  <div className="th-filter-tabs">
                    <button
                      className={`tab-btn ${activeFilter === "all" ? "is-active" : ""}`}
                      onClick={() => setActiveFilter("all")}
                    >
                      全部
                    </button>
                    <button
                      className={`tab-btn ${activeFilter === "running" ? "is-active" : ""}`}
                      onClick={() => setActiveFilter("running")}
                    >
                      处理中
                    </button>
                    <button
                      className={`tab-btn ${activeFilter === "waiting_for_ci" ? "is-active" : ""}`}
                      onClick={() => setActiveFilter("waiting_for_ci")}
                    >
                      等待 CI
                    </button>
                    <button
                      className={`tab-btn ${activeFilter === "queued" ? "is-active" : ""}`}
                      onClick={() => setActiveFilter("queued")}
                    >
                      排队中
                    </button>
                    <button
                      className={`tab-btn ${activeFilter === "completed" ? "is-active" : ""}`}
                      onClick={() => setActiveFilter("completed")}
                    >
                      已完成
                    </button>
                  </div>
                </div>

                <div className="th-right">
                  {/* Search Box */}
                  <div className="warm-search-input-box">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
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
                        className="clear-search-x"
                        onClick={() => setSearchKeyword("")}
                      >
                        ✕
                      </button>
                    )}
                  </div>
                </div>
              </div>

              <div className="warm-table-scroller">
                <table className="warm-clean-table">
                  <thead>
                    <tr>
                      <th>仓库 / 批次 ID</th>
                      <th>PR 编号</th>
                      <th>Head Commit SHA</th>
                      <th>执行状态</th>
                      <th>重试次数</th>
                      <th>更新时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredReviews.map((review) => (
                      <ReviewRow review={review} key={review.review_run_id} />
                    ))}
                  </tbody>
                </table>

                {!loading && filteredReviews.length === 0 && (
                  <div className="warm-empty-state">
                    <div className="empty-sun-icon">
                      <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                        <circle cx="12" cy="12" r="10" />
                        <path d="M16 16s-1.5-2-4-2-4 2-4 2" />
                        <line x1="9" y1="9" x2="9.01" y2="9" />
                        <line x1="15" y1="9" x2="15.01" y2="9" />
                      </svg>
                    </div>
                    <h4>暂无匹配的审查任务</h4>
                    <p>
                      {searchKeyword || activeFilter !== "all"
                        ? "当前筛选条件下未查询到任务，您可以尝试重置筛选或清除搜索词。"
                        : "调度任务队列当前为空，请在右侧面板提交一条新的审查任务！"}
                    </p>
                    {(searchKeyword || activeFilter !== "all") && (
                      <button
                        className="reset-filter-action-btn"
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

          {/* Right Column: Create Task & Protocols */}
          <aside className="warm-right-col">
            <CreateReviewForm
              onCreated={(message) => {
                setPageMessage(message);
                void refresh();
              }}
              onUnauthorized={() => onSignedOut("登录状态已失效，请重新登录")}
            />

            {/* Architecture Protocol Card */}
            <div className="warm-protocol-card">
              <div className="protocol-card-head">
                <div className="protocol-icon-bubble">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
                  </svg>
                </div>
                <div>
                  <h4>M2 里程碑系统契约</h4>
                  <span className="protocol-badge">SAFETY & RELIABILITY</span>
                </div>
              </div>

              <div className="protocol-points-list">
                <div className="protocol-point-row">
                  <span className="protocol-dot orange" />
                  <div>
                    <strong>Worker 容灾重试</strong>
                    <p>超时未响应自动触发重试，保障调度不丢单</p>
                  </div>
                </div>
                <div className="protocol-point-row">
                  <span className="protocol-dot caramel" />
                  <div>
                    <strong>CI 门禁安全区</strong>
                    <p>任务在 <code>waiting_for_ci</code> 阶段安全等待 GitHub 状态</p>
                  </div>
                </div>
                <div className="protocol-point-row">
                  <span className="protocol-dot green" />
                  <div>
                    <strong>M3 大模型接入</strong>
                    <p>支持深度代码语义审查与 GitHub Comment 自动回写</p>
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
