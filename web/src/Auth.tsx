import { Input } from "./components/ui/input";
import { Button } from "./components/ui/button";
import { FormEvent, useEffect, useState } from "react";
import { ArrowRight, GitPullRequest, Check, AlertCircle, LockKeyhole } from "lucide-react";

import { api, ApiError } from "./api";
import { loadSavedCredentials, saveCredentials } from "./credentials";
import type { AuthUser } from "./types";
import BrandMark from "./BrandMark";

export function Brand() {
  return (
    <div className="brand-logo-unit" aria-label="OpenReviewer">
      <div className="brand-sunburst-badge">
        <BrandMark />
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
        <div className="login-intro-copy"><span className="login-eyebrow"><span />代码有上下文，判断有依据</span>
          <h2>每一次合并，<br />都<span>有据可循。</span></h2>
          <p>把 AI 审查、代码证据与团队处理放在一起，让每个问题都有清晰的来路和下一步。</p>
        </div>
        <div className="login-review-art" role="img" aria-label="审查流程示意：代码变更经过上下文核验和人工确认，再发布审查结果。">
          <div className="review-art-caption" aria-hidden="true"><span>从变更到判断</span><span>REVIEW / 01</span></div>
          <div className="review-art-code" aria-hidden="true">
            <div className="review-art-file"><GitPullRequest /><span>pull request</span><span className="review-art-dots">···</span></div>
            <div className="review-art-line"><span>01</span><code><i>const</i> review = &#123;</code></div>
            <div className="review-art-line is-added"><span>02</span><code>+ &nbsp;context: <em>"connected"</em>,</code></div>
            <div className="review-art-line is-added"><span>03</span><code>+ &nbsp;evidence: <em>"traceable"</em></code></div>
            <div className="review-art-line"><span>04</span><code>&#125;</code></div>
          </div>
          <div className="review-art-verdict" aria-hidden="true"><span className="review-art-check"><Check /></span><div><strong>让结论，经得起核对。</strong><span>代码证据 → 人工确认 → 发布结果</span></div><span className="review-art-arrow">↗</span></div>
        </div>
        <ol className="login-capabilities">
          <li><span className="login-capability-number">01</span><div><strong>固定版本</strong><span>保留审查记录</span></div></li>
          <li><span className="login-capability-number">02</span><div><strong>追溯证据</strong><span>关联代码上下文</span></div></li>
          <li><span className="login-capability-number">03</span><div><strong>团队确认</strong><span>批准后再发布</span></div></li>
        </ol>
        <div className="login-intro-footer"><span>为认真对待代码的团队而建</span><span>OpenReviewer</span></div>
      </section>
      <section className="simple-login-content" aria-labelledby="login-title">
        <div className="login-mobile-brand"><Brand /></div>
        <div className="login-heading"><span className="login-welcome">YOUR NEXT REVIEW STARTS HERE</span><h1 id="login-title">欢迎回到工作台</h1>
          <p>使用团队账号，继续你的审查工作。</p></div>
        <form className="auth-card" onSubmit={submit} autoComplete="on" aria-busy={submitting}>
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
        <p className="simple-login-note"><LockKeyhole aria-hidden="true" /><span>仅限已获授权的团队成员。账号由管理员创建。<br />登录后可在右上角查看使用手册。</span></p>
      </section>
    </div>
  </main>;
}
