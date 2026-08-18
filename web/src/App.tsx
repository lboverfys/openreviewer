import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import { api, ApiError } from "./api";
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
      <span className="brand-mark" aria-hidden="true">
        <span>&lt;</span>
        <i />
        <span>/&gt;</span>
      </span>
      <span>
        <strong>OpenReviewer</strong>
        <small>审查运行控制台</small>
      </span>
    </div>
  );
}

function LoadingScreen() {
  return (
    <main className="loading-screen">
      <Brand />
      <div className="loader" aria-label="正在确认登录状态">
        <span />
        <span />
        <span />
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

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setMessage("");
    try {
      const user = await api.login(username, password);
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
      <div className="ambient ambient-one" />
      <div className="ambient ambient-two" />
      <section className="login-story">
        <Brand />
        <div className="story-copy">
          <p className="eyebrow">REVIEW OPERATIONS / M2</p>
          <h1>
            让每一次代码审查
            <br />
            <em>都有迹可循</em>
          </h1>
          <p>
            查看任务流转、Worker 心跳与重试状态。当前版本会在 CI 边界前停下，
            不会把尚未执行的模型审查标记为完成。
          </p>
        </div>
        <div className="story-status">
          <span className="pulse-dot" />
          <span>系统入口已加密</span>
          <span className="story-divider" />
          <span>单 Worker 模式</span>
        </div>
      </section>

      <section className="login-panel">
        <form className="login-card" onSubmit={submit}>
          <div className="login-heading">
            <p className="eyebrow">AUTHORIZED ACCESS</p>
            <h2>欢迎回来</h2>
            <p>请使用管理员账号进入审查控制台</p>
          </div>

          <label className="field">
            <span>账号</span>
            <span className="input-shell">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M20 21a8 8 0 0 0-16 0M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z" />
              </svg>
              <input
                name="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="username"
                maxLength={100}
                required
                autoFocus
              />
            </span>
          </label>

          <label className="field">
            <span>密码</span>
            <span className="input-shell">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M7 10V8a5 5 0 0 1 10 0v2m-11 0h12a2 2 0 0 1 2 2v8H4v-8a2 2 0 0 1 2-2Z" />
              </svg>
              <input
                name="password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                maxLength={512}
                required
              />
            </span>
          </label>

          <div className="form-message" role="alert" aria-live="polite">
            {message}
          </div>

          <button className="primary-button login-button" disabled={submitting}>
            {submitting ? "正在验证…" : "进入控制台"}
            {!submitting && <span aria-hidden="true">→</span>}
          </button>

          <p className="security-note">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="m12 3 8 4v5c0 5-3.4 8-8 9-4.6-1-8-4-8-9V7l8-4Z" />
              <path d="m9 12 2 2 4-4" />
            </svg>
            密码仅用于服务端 Argon2id 校验，不会保存在浏览器中
          </p>
        </form>
        <p className="login-footer">OpenReviewer · Internal review infrastructure</p>
      </section>
    </main>
  );
}

function StatusBadge({ status }: { status: ExecutionStatus }) {
  return <span className={`status-badge status-${status}`}>{statusLabels[status]}</span>;
}

function ReviewRow({ review }: { review: ReviewItem }) {
  return (
    <tr>
      <td>
        <div className="repo-cell">
          <strong>{review.repository}</strong>
          <span>运行 {review.review_run_id.slice(0, 8)}</span>
        </div>
      </td>
      <td>
        <span className="pr-number">#{review.pull_request_number}</span>
      </td>
      <td>
        <code>{shortSha(review.head_sha)}</code>
      </td>
      <td>
        <StatusBadge status={review.execution_status} />
      </td>
      <td>
        <span className="attempts">
          {review.attempt_count}/{review.max_attempts}
        </span>
      </td>
      <td>{formatDate(review.updated_at)}</td>
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
      const successMessage = `任务 ${result.review_task_id.slice(0, 8)} 已进入队列`;
      setMessage(successMessage);
      onCreated(successMessage);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        onUnauthorized();
        return;
      }
      const friendly =
        error instanceof ApiError && error.status === 422
          ? "输入内容不符合任务契约，请检查 ID、仓库名和完整 SHA"
          : errorMessage(error);
      setMessage(friendly);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="create-form" onSubmit={submit}>
      <div className="section-heading compact">
        <div>
          <p className="eyebrow">NEW REVIEW</p>
          <h2>创建审查任务</h2>
        </div>
        <span className="queue-icon" aria-hidden="true">＋</span>
      </div>

      <div className="form-grid two-columns">
        <label className="field small">
          <span>Installation ID</span>
          <input
            name="installation_id"
            type="number"
            min="1"
            step="1"
            value={installationId}
            onChange={(event) => setInstallationId(event.target.value)}
            required
          />
        </label>
        <label className="field small">
          <span>Repository ID</span>
          <input
            name="repository_id"
            type="number"
            min="1"
            step="1"
            value={repositoryId}
            onChange={(event) => setRepositoryId(event.target.value)}
            required
          />
        </label>
      </div>

      <label className="field small">
        <span>仓库</span>
        <input
          name="repository"
          value={repository}
          onChange={(event) => setRepository(event.target.value)}
          placeholder="owner/repository"
          pattern={"[A-Za-z0-9_.\\-]+/[A-Za-z0-9_.\\-]+"}
          required
        />
      </label>

      <label className="field small">
        <span>Pull Request 编号</span>
        <input
          name="pull_request_number"
          type="number"
          min="1"
          step="1"
          value={pullRequest}
          onChange={(event) => setPullRequest(event.target.value)}
          required
        />
      </label>

      <label className="field small">
        <span>Head SHA</span>
        <input
          name="head_sha"
          className="mono-input"
          value={headSha}
          onChange={(event) => setHeadSha(event.target.value)}
          minLength={40}
          maxLength={64}
          pattern="[0-9a-fA-F]{40,64}"
          placeholder="40 位提交哈希"
          required
        />
      </label>

      <button className="primary-button create-button" disabled={submitting}>
        {submitting ? "正在提交…" : "提交到任务队列"}
        {!submitting && <span aria-hidden="true">↗</span>}
      </button>
      <div className="inline-message" role="status" aria-live="polite">
        {message}
      </div>
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

  const refresh = useCallback(async () => {
    try {
      setSnapshot(await api.dashboard());
      setPageMessage("");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setPageMessage("暂时无法读取仪表盘，系统会继续自动重试");
    } finally {
      setLoading(false);
    }
  }, [onSignedOut]);

  useEffect(() => {
    void refresh();
    const source = new EventSource("/api/v1/reviews/stream");
    source.onopen = () => setStreamState("live");
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
    <div className="app-shell">
      <header className="topbar">
        <Brand />
        <div className="topbar-actions">
          <div className={`live-indicator live-${streamState}`}>
            <span />
            {streamState === "live"
              ? "实时连接"
              : streamState === "connecting"
                ? "正在连接"
                : "正在重连"}
          </div>
          <div className="user-chip">
            <span>{user.username.slice(0, 1).toUpperCase()}</span>
            <div>
              <strong>{user.username}</strong>
              <small>管理员</small>
            </div>
          </div>
          <button className="ghost-button" onClick={logout} aria-label="退出登录">
            退出
          </button>
        </div>
      </header>

      <main className="dashboard-page">
        <section className="page-intro">
          <div>
            <p className="eyebrow">OPERATIONS OVERVIEW</p>
            <h1>审查任务总览</h1>
            <p>观察任务从排队、领取到等待 CI 的真实状态。</p>
          </div>
          <div className="last-sync">
            <span>最近同步</span>
            <strong>{formatDate(snapshot?.generated_at ?? null)}</strong>
          </div>
        </section>

        {pageMessage && <div className="page-alert" role="alert">{pageMessage}</div>}

        <section className="dashboard-grid">
          <div className="main-column">
            <div className="metrics-grid" aria-busy={loading}>
              <article className="metric-card total-card">
                <span className="metric-label">全部任务</span>
                <strong>{snapshot?.total_reviews ?? "—"}</strong>
                <small>累计提交的审查运行</small>
                <i aria-hidden="true">Σ</i>
              </article>
              {statusCards.map(({ status, count }) => (
                <article className={`metric-card metric-${status}`} key={status}>
                  <span className="metric-label">{statusLabels[status]}</span>
                  <strong>{loading ? "—" : count}</strong>
                  <span className="metric-line" />
                </article>
              ))}
            </div>

            <section className="panel worker-panel">
              <div className="worker-main">
                <div className={`worker-orb ${workerHealthy ? "healthy" : "offline"}`}>
                  <span>&lt;/&gt;</span>
                </div>
                <div>
                  <p className="eyebrow">WORKER STATUS</p>
                  <h2>
                    {workerHealthy ? "Worker 在线" : "Worker 离线"}
                    <span className={workerHealthy ? "online-dot" : "offline-dot"} />
                  </h2>
                  <p>
                    {worker?.worker_id ?? "尚未收到任何 Worker 心跳"}
                  </p>
                </div>
              </div>
              <div className="worker-facts">
                <div>
                  <span>当前状态</span>
                  <strong>
                    {worker?.status ? workerLabels[worker.status] : "未连接"}
                  </strong>
                </div>
                <div>
                  <span>当前任务</span>
                  <strong>{worker?.current_task_id?.slice(0, 8) ?? "—"}</strong>
                </div>
                <div>
                  <span>最后心跳</span>
                  <strong>{formatDate(worker?.last_seen_at ?? null)}</strong>
                </div>
              </div>
            </section>

            <section className="panel reviews-panel">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">RECENT RUNS</p>
                  <h2>最近审查任务</h2>
                </div>
                <button className="text-button" onClick={() => void refresh()}>
                  立即刷新 <span aria-hidden="true">↻</span>
                </button>
              </div>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>仓库 / 运行</th>
                      <th>PR</th>
                      <th>Head SHA</th>
                      <th>状态</th>
                      <th>尝试</th>
                      <th>更新时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {snapshot?.recent_reviews.map((review) => (
                      <ReviewRow review={review} key={review.review_run_id} />
                    ))}
                  </tbody>
                </table>
                {!loading && !snapshot?.recent_reviews.length && (
                  <div className="empty-state">
                    <span aria-hidden="true">{`{ }`}</span>
                    <strong>还没有审查任务</strong>
                    <p>从右侧表单创建第一条任务，它会实时出现在这里。</p>
                  </div>
                )}
              </div>
            </section>
          </div>

          <aside className="side-column">
            <section className="panel create-panel">
              <CreateReviewForm
                onCreated={(message) => {
                  setPageMessage(message);
                  void refresh();
                }}
                onUnauthorized={() => onSignedOut("登录状态已失效，请重新登录")}
              />
            </section>
            <section className="boundary-card">
              <span className="boundary-icon" aria-hidden="true">i</span>
              <div>
                <strong>M2 能力边界</strong>
                <p>
                  Worker 会领取并恢复任务，目前在 <code>waiting_for_ci</code>
                  停下。GitHub、模型与审查结果将在后续里程碑接入。
                </p>
              </div>
            </section>
          </aside>
        </section>
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
