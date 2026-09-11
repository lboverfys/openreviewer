import { FormEvent, useEffect, useState } from "react";

import { api, ApiError } from "./api";
import { loadSavedCredentials, saveCredentials } from "./credentials";
import type { AuthUser } from "./types";

export function Brand() {
  return (
    <div className="brand-logo-unit" aria-label="OpenReviewer">
      <div className="brand-sunburst-badge">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="16 18 22 12 16 6" />
          <polyline points="8 6 2 12 8 18" />
          <line x1="14" y1="4" x2="10" y2="20" />
        </svg>
      </div>
      <div className="brand-name-group">
        <div className="brand-header-line">
          <strong>OpenReviewer</strong>
          <span className="brand-v-pill">v0.2.0</span>
        </div>
        <small>AI 代码审查与调度中枢</small>
      </div>
    </div>
  );
}
export function LoadingScreen() {
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

const showcaseFeatures = [
  {
    icon: "📡",
    title: "实时状态订阅",
    text: "任务进度秒级推送，排队、审查、汇总全程可见。",
  },
  {
    icon: "🛡️",
    title: "加密凭据管理",
    text: "密钥加密落盘、掩码展示，会话由服务端校验。",
  },
  {
    icon: "🔁",
    title: "租约与自动重试",
    text: "失败节点可单独重跑，不重复消耗已成功请求。",
  },
];

const showcaseFlow = [
  { badge: "01", title: "任务生成", text: "绑定提交版本" },
  { badge: "02", title: "Worker 分发", text: "后台队列执行" },
  { badge: "03", title: "等待 CI", text: "检查完成后继续" },
  { badge: "04", title: "模型深度审查", text: "问题与代码证据" },
];

export function Login({ initialMessage, onAuthenticated }: LoginProps) {
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
    <main className="auth-page">
      <div className="auth-blob auth-blob-indigo" aria-hidden="true" />
      <div className="auth-blob auth-blob-cyan" aria-hidden="true" />
      <div className="auth-grid-pattern" aria-hidden="true" />

      <header className="auth-topbar">
        <Brand />
        <span className="auth-topbar-note">
          <span className="auth-topbar-dot" />
          审查调度服务运行中
        </span>
      </header>

      <section className="auth-hero">
        <span className="auth-eyebrow-chip">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
            <path d="M12 3l1.9 5.8L20 10l-6.1 1.2L12 17l-1.9-5.8L4 10l6.1-1.2L12 3z" />
          </svg>
          OpenReviewer · 智能审查工作台
        </span>
        <h1>
          让每一次代码审查
          <br />
          <em className="auth-gradient-text">清晰、可靠、有迹可循</em>
        </h1>
        <p>
          从拉取请求到问题确认，在同一个工作台查看审查进度、
          跨文件代码证据和处理结果。
        </p>
      </section>

      <section className="auth-card-stage">
        <form className="auth-card" onSubmit={submit} autoComplete="on">
          <div className="auth-card-top">
            <div className="auth-avatar-box">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 2a5 5 0 0 0-5 5v3H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-1V7a5 5 0 0 0-5-5zM9 7a3 3 0 0 1 6 0v3H9V7z"/>
              </svg>
            </div>
            <h2>欢迎登录</h2>
            <p>请使用管理员凭据进入审查调度控制台</p>
          </div>

          <div className="auth-form-body">
            <label className="auth-field">
              <span className="auth-field-title">管理员账号</span>
              <div className="auth-input-box">
                <svg className="auth-field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
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

            <label className="auth-field">
              <span className="auth-field-title">安全密码</span>
              <div className="auth-input-box">
                <svg className="auth-field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
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

            <label className="auth-checkbox-field">
              <input
                type="checkbox"
                checked={rememberCredentials}
                onChange={(event) => setRememberCredentials(event.target.checked)}
              />
              <span className="auth-checkbox-square" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </span>
              <span className="auth-checkbox-copy">
                <strong>记住登录状态</strong>
                <small>交由浏览器原生密码库安全存储</small>
              </span>
            </label>

            {message && (
              <div className="auth-alert" role="alert" aria-live="polite">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                  <circle cx="12" cy="12" r="10" />
                  <line x1="12" y1="8" x2="12" y2="12" />
                  <line x1="12" y1="16" x2="12.01" y2="16" />
                </svg>
                <span>{message}</span>
              </div>
            )}

            <button type="submit" className="auth-submit-btn" disabled={submitting}>
              {submitting ? (
                <>
                  <span className="btn-spinner" />
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
            <span>会话由服务端验证 · 密码不写入 localStorage</span>
          </div>
        </form>
      </section>

      <section className="auth-feature-grid" aria-label="平台特性">
        {showcaseFeatures.map((feature) => (
          <div className="auth-feature-card" key={feature.title}>
            <span className="auth-feature-icon" aria-hidden="true">{feature.icon}</span>
            <div>
              <strong>{feature.title}</strong>
              <p>{feature.text}</p>
            </div>
          </div>
        ))}
      </section>

      <section className="auth-flow-strip" aria-label="审查流程示意">
        {showcaseFlow.map((step, index) => (
          <div className="auth-flow-item" key={step.badge}>
            <span className="auth-flow-badge">{step.badge}</span>
            <div>
              <strong>{step.title}</strong>
              <small>{step.text}</small>
            </div>
            {index < showcaseFlow.length - 1 && <span className="auth-flow-arrow" aria-hidden="true">→</span>}
          </div>
        ))}
      </section>

      <footer className="auth-footnote">OpenReviewer · 让审查结果有据可查</footer>
    </main>
  );
}
