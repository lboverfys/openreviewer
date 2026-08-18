import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

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
  /**
   * 渲染全站复用的品牌标识和控制台副标题。
   *
   * 组件没有输入参数，也不持有状态；它只输出静态可访问标记。图形装饰通过
   * `aria-hidden` 隐藏，真正的品牌名称保留为文本，便于屏幕阅读器和测试定位。
   */
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
  /**
   * 在首次查询会话期间展示稳定尺寸的加载画面。
   *
   * 根组件在 `api.me()` 返回前只渲染这里，避免登录页和控制台先后闪烁。该组件
   * 没有网络请求或定时器，加载动画完全由 CSS 驱动，尺寸稳定后再交给下一阶段页面。
   */
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
  /**
   * 管理员登录表单。
   *
   * 密码只在组件状态中短暂存在；用户勾选“记住账号密码”后，提交成功的凭据会
   * 交给浏览器 PasswordCredential 密码库保存，应用本身不写入 localStorage，也不
   * 保存会话 Token。组件重新挂载时会向浏览器密码库请求自动填充。
   *
   * 参数：
   * - `initialMessage`：从会话检查或注销流程传来的首次提示，可选。
   * - `onAuthenticated`：登录成功后由父组件提供的状态切换回调。
   *
   * 组件状态只控制输入、提交中、记住开关和错误提示；真正的身份验证、限流和
   * 会话 Cookie 设置由后端完成。浏览器不支持凭据管理 API 时，用户仍可手动登录，
   * 只是无法由本应用主动触发自动填充。
   */
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState(initialMessage ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [rememberCredentials, setRememberCredentials] = useState(true);

  useEffect(() => {
    /**
     * 首次显示登录页时读取浏览器密码库。
     *
     * 使用函数式状态更新是为了不覆盖用户在异步读取期间已经开始输入的内容；
     * 凭据读取失败只代表浏览器策略不允许，不影响普通登录流程。
     */
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
    /**
     * 处理登录表单提交并把后端结果转换为页面状态。
     *
     * 参数：
     * - `event`：浏览器表单提交事件；调用 `preventDefault` 防止整页刷新。
     *
     * 流程：先锁定按钮并清空旧提示，再调用 API；成功后按开关异步把凭据交给浏览器
     * 密码库，不等待这个可选动作完成，然后清除 React 中的密码并通知父组件；失败
     * 时按 401/429/其他错误显示不同文案。密码在成功和失败分支都会清空，避免继续
     * 留在页面状态中。
     */
    event.preventDefault();
    setSubmitting(true);
    setMessage("");
    try {
      // 后端契约会去掉账号两端空白；前端先统一格式，避免浏览器密码库保存出带空格的账号。
      const normalizedUsername = username.trim();
      const user = await api.login(normalizedUsername, password);
      if (rememberCredentials) {
        // 保存动作只触碰浏览器密码库；失败不会回滚已经成功的服务端登录。
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
        <form className="login-card" onSubmit={submit} autoComplete="on">
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

          <label className="remember-field">
            <input
              type="checkbox"
              checked={rememberCredentials}
              onChange={(event) => setRememberCredentials(event.target.checked)}
            />
            <span className="remember-box" aria-hidden="true" />
            <span className="remember-copy">
              <strong>记住账号密码</strong>
              <small>由浏览器密码库安全保存</small>
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
            凭据交由浏览器密码库管理，应用不会写入本地存储
          </p>
        </form>
        <p className="login-footer">OpenReviewer · Internal review infrastructure</p>
      </section>
    </main>
  );
}

function StatusBadge({ status }: { status: ExecutionStatus }) {
  /**
   * 将机器状态值映射为带颜色语义的可读徽标。
   *
   * 参数：
   * - `status`：后端返回的受限 `ExecutionStatus` 枚举值。
   *
   * CSS 类名保留机器值，文本从 `statusLabels` 读取；如果状态枚举扩展，TypeScript
   * 会提示同步更新标签和样式。组件不修改状态，也不触发网络请求。
   */
  return <span className={`status-badge status-${status}`}>{statusLabels[status]}</span>;
}

function ReviewRow({ review }: { review: ReviewItem }) {
  /**
   * 把单条任务读模型渲染成 Dashboard 表格行。
   *
   * 参数：
   * - `review`：服务端 Dashboard 快照中的一条任务，包含仓库、PR、SHA、状态、
   *   尝试次数和更新时间。
   *
   * 仅缩短 SHA 和运行 ID 供视觉展示，原始数据没有被修改；状态徽标委托给
   * `StatusBadge`，日期委托给 `formatDate`，保证表格各行使用同一套格式规则。
   */
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
  /**
   * 手工创建审查任务的表单。
   *
   * 表单只收集后端任务契约要求的五个字段，并为每次点击生成新的幂等键。
   * 成功后清空容易填错的 PR 编号和 SHA，同时通知父组件刷新 Dashboard。
   *
   * 参数：
   * - `onCreated`：任务被 API 接受后传回成功提示，父组件用它更新页面消息并刷新。
   * - `onUnauthorized`：API 返回 401 时通知父组件清除当前会话。
   *
   * 文本输入先保存在本地状态，提交时才转换为数字并交给后端 Pydantic 契约做最终
   * 校验；前端约束用于尽早提示，不能替代服务器校验。
   */
  const [installationId, setInstallationId] = useState("");
  const [repositoryId, setRepositoryId] = useState("");
  const [repository, setRepository] = useState("lboverfys/NiuMa");
  const [pullRequest, setPullRequest] = useState("");
  const [headSha, setHeadSha] = useState("");
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    /**
     * 将表单字符串转换为任务请求并提交。
     *
     * 参数：
     * - `event`：表单提交事件，阻止浏览器默认跳转。
     *
     * 每次提交使用 `manual:<UUID>` 作为新的幂等键，因此用户明确再次点击会创建
     * 新运行；网络重试应复用同一个键才不会重复。成功只代表任务进入队列，不代表
     * Worker 已完成审查；401 交给父组件退出，422 显示契约提示，其他错误保留安全
     * 的统一消息。无论结果如何都会恢复按钮可用状态。
     */
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
  /**
   * 认证后的运行控制台。
   *
   * 首次进入先请求一次快照，再打开 SSE 长连接接收后续更新；连接断开时依靠
   * 浏览器 EventSource 自动重连，并在界面上显示当前连接状态。所有 401 都
   * 交给父组件切回登录页，避免继续展示可能已经过期的数据。
   *
   * 参数：
   * - `user`：根组件已经验证过的管理员信息，用于顶部身份展示。
   * - `onSignedOut`：会话失效或用户注销时切回登录阶段的回调。
   *
   * 数据来源有两条：`refresh` 负责首屏/手工完整快照，SSE 负责后续增量式快照。
   * SSE 断开时保留最后一份快照但显示重连状态；只有新的 401 才清空登录阶段，避免
   * 短暂网络抖动把用户强制登出。
   */
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [streamState, setStreamState] = useState<StreamState>("connecting");
  const [pageMessage, setPageMessage] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    /**
     * 手工或首屏读取最新 Dashboard 快照。
     *
     * 成功时替换快照并清除旧提示；401 说明 Cookie 已失效，交给父组件切回登录；
     * 其他错误保留旧快照并显示“稍后重试”，避免暂时的数据库故障把页面清空。
     * `finally` 会解除加载状态，因此按钮和空状态不会永久停留在 loading。
     */
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
    // 首次请求负责填充页面，SSE 负责后续实时更新；清理函数关闭长连接。
    void refresh();
    const source = new EventSource("/api/v1/reviews/stream");
    source.onopen = () => {
      // 浏览器完成连接或自动重连后，先把顶部状态恢复为实时连接。
      setStreamState("live");
    };
    source.addEventListener("dashboard", (event) => {
      try {
        // 服务端发送的是完整快照，因此直接替换而不是合并旧字段。
        setSnapshot(JSON.parse((event as MessageEvent<string>).data));
        setStreamState("live");
        setLoading(false);
      } catch {
        // 单条事件 JSON 损坏时保留旧快照，等待 EventSource 下一次重连/事件。
        setStreamState("reconnecting");
      }
    });
    source.addEventListener("unavailable", () => {
      // 后端暂时读不到数据库时连接仍在，页面只显示重连状态而不覆盖快照。
      setStreamState("reconnecting");
    });
    source.onerror = () => {
      // EventSource 会自行重试；这里仅同步可见状态，不额外创建定时器。
      setStreamState("reconnecting");
    };
    return () => source.close();
  }, [refresh]);

  const statusCards = useMemo(
    // 让所有后端状态都拥有固定卡片位置，缺失计数按 0 显示，避免布局跳动。
    () =>
      statusOrder.map((status) => ({
        status,
        count: snapshot?.status_counts[status] ?? 0,
      })),
    [snapshot],
  );

  async function logout() {
    /**
     * 注销当前浏览器会话并切回登录界面。
     *
     * 先尽力调用后端删除 Cookie；即使网络失败也执行父组件回调，防止用户继续
     * 操作可能已经失效的页面。后端 Token 没有写入前端，因此不需要额外清理缓存。
     */
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
  /**
   * 应用根组件，负责在“检查会话 / 未登录 / 已登录”三个阶段之间切换。
   * 初始阶段不直接显示登录表单，避免已经登录的用户先看到错误页面闪烁。
   *
   * 会话检查只在组件挂载时执行一次；清理函数通过 `active` 标志忽略组件卸载后
   * 才到达的异步结果，避免 React 警告或旧请求覆盖新页面。登录成功和注销都只
   * 修改这里的 `SessionState`，具体表单与 Dashboard 逻辑由子组件负责。
   */
  const [session, setSession] = useState<SessionState>({ phase: "checking" });

  useEffect(() => {
    let active = true;
    api
      .me()
      .then((user) => {
        // 组件仍挂载且会话有效时才进入控制台。
        if (active) setSession({ phase: "authenticated", user });
      })
      .catch((error) => {
        // 401 是正常的未登录分支，其他状态显示服务暂不可用提示。
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
      // 阻止卸载后异步响应再次写入状态。
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
