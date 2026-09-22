import { Input } from "./components/ui/input";
import { Button } from "./components/ui/button";
import { FormEvent, useEffect, useState } from "react";
import { ArrowRight, FileCode2, GitPullRequest, ShieldCheck, AlertCircle, LockKeyhole } from "lucide-react";

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
        </div>
        <small>代码审查平台</small>
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
        <p className="loading-hint">正在加载页面…</p>
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

  return <main className="auth-page simple-login">
    <div className="login-layout">
      <section className="login-intro" aria-label="平台简介">
        <Brand />
        <div className="login-intro-copy"><span className="login-eyebrow">团队代码审查工作台</span>
          <h2>从代码变更，<br />到有据可循的判断。</h2>
          <p>把 AI 审查、代码证据与团队处理放在一起，让每个问题都有清晰的来路和下一步。</p>
        </div>
        <ol className="login-capabilities">
          <li><GitPullRequest aria-hidden="true" /><div><strong>围绕提交开展审查</strong><span>固定代码版本，保留完整审查记录。</span></div></li>
          <li><FileCode2 aria-hidden="true" /><div><strong>结合证据核对问题</strong><span>关联代码上下文，追溯每一条发现。</span></div></li>
          <li><ShieldCheck aria-hidden="true" /><div><strong>由团队决定下一步</strong><span>人工确认、批准，再发布和跟进修复。</span></div></li>
        </ol>
        <div className="login-intro-footer"><span className="login-status-dot" />OpenReviewer · 团队工作空间</div>
      </section>
      <section className="simple-login-content" aria-labelledby="login-title">
        <div className="login-mobile-brand"><Brand /></div>
        <div className="login-heading"><span className="login-welcome">欢迎回来</span><h1 id="login-title">登录代码审查平台</h1>
          <p>使用团队账号，继续你的审查工作。</p></div>
        <form className="auth-card" onSubmit={submit} autoComplete="on">
        <label className="simple-login-field">账号<Input className="h-11" name="username" value={username}
          onChange={event => setUsername(event.target.value)} autoComplete="username" maxLength={100}
          placeholder="输入你的账号" required autoFocus disabled={submitting} /></label>
        <label className="simple-login-field">密码<Input className="h-11" name="password" type="password" value={password}
          onChange={event => setPassword(event.target.value)} autoComplete="current-password"
          maxLength={512} required disabled={submitting} /></label>
        <div className="login-remember-group"><label className="simple-login-remember"><input type="checkbox" checked={rememberCredentials} disabled={submitting}
          onChange={event => setRememberCredentials(event.target.checked)} />在这台设备保存登录信息</label>
          <small>公共电脑请勿保存登录信息。</small></div>
        {message && <div className="login-message" role="alert"><AlertCircle aria-hidden="true" /><span>{message}</span></div>}
        <Button variant="default" type="submit" className="auth-submit-btn h-11 justify-between" disabled={submitting}>{submitting ? "正在登录…" : "登录平台"}<ArrowRight aria-hidden="true" /></Button>
        </form>
        <p className="simple-login-note"><LockKeyhole aria-hidden="true" /><span>账号由管理员创建。<br />登录后可在右上角查看使用手册。</span></p>
      </section>
    </div>
  </main>;
}
