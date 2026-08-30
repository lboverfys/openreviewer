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
    <main className="warm-login-page">
      <div className="blob-warm-1" />
      <div className="blob-warm-2" />
      <div className="warm-dot-pattern" />

      {/* Left Showcase Side */}
      <section className="warm-showcase-panel">
        <div className="showcase-topbar">
          <Brand />
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

      {/* Right Login Form Side */}
      <section className="warm-login-form-side">
        <div className="warm-auth-wrapper">
          <form className="warm-glass-auth-card" onSubmit={submit} autoComplete="on">
            <div className="auth-card-top">
              <div className="auth-avatar-box">
                <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 2a5 5 0 0 0-5 5v3H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-1V7a5 5 0 0 0-5-5zM9 7a3 3 0 0 1 6 0v3H9V7z"/>
                </svg>
              </div>
              <h2>欢迎登录</h2>
              <p>请使用管理员凭据进入审查调度控制台</p>
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

              <button type="submit" className="warm-primary-btn" disabled={submitting}>
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
