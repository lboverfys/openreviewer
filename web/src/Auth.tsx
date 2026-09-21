import { Input } from "./components/ui/input";
import { Button } from "./components/ui/button";
import { Notice } from "./Feedback";
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
    <header className="simple-login-brand"><Brand /></header>
    <section className="simple-login-content">
      <h1>登录代码审查平台</h1>
      <p>使用团队账号查看审查、核对问题和跟进修复。</p>
      <form className="auth-card" onSubmit={submit} autoComplete="on">
        <label className="simple-login-field">账号<Input name="username" value={username}
          onChange={event => setUsername(event.target.value)} autoComplete="username" maxLength={100}
          placeholder="输入你的账号" required autoFocus disabled={submitting} /></label>
        <label className="simple-login-field">密码<Input name="password" type="password" value={password}
          onChange={event => setPassword(event.target.value)} autoComplete="current-password"
          maxLength={512} required disabled={submitting} /></label>
        <label className="simple-login-remember"><input type="checkbox" checked={rememberCredentials}
          onChange={event => setRememberCredentials(event.target.checked)} />在这台设备保存登录信息</label>
        <small>公共电脑请勿保存登录信息。</small>
        {message && <Notice kind="success" onDismiss={() => setMessage("")}>{message}</Notice>}
        <Button variant="default" type="submit" className="auth-submit-btn" disabled={submitting}>{submitting ? "正在登录…" : "登录平台"}</Button>
      </form>
      <p className="simple-login-note">账号由管理员创建。登录后可在右上角打开使用手册。</p>
    </section>
  </main>;
}
