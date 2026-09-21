import { Button } from "./components/ui/button";
import { NativeSelect } from "./components/ui/native-select";
import { useEffect, useState } from "react";
import { api } from "./api";
import type { TeamRepository } from "./types";

export type AppInstallations = {app_name: string; authorize_url: string; has_more: boolean; items: {id: number; account: string; selection: string; manage_url: string}[]};
export type AuthorizedRepositories = {manage_url: string; selection: string; has_more: boolean; items: {id: number; repository: string}[]};

export default function RepositoryConnectPanel({existing, onSaved, onCancel, onError}: {
  existing?: TeamRepository; onSaved: () => void; onCancel: () => void; onError: (error: unknown) => void;
}) {
  const [installations, setInstallations] = useState<AppInstallations | null>(null);
  const [repositories, setRepositories] = useState<AuthorizedRepositories | null>(null);
  const [installation, setInstallation] = useState(existing?.connection_installation_id ?? 0);
  const [repository, setRepository] = useState(existing?.repository ?? "");
  const [appPage, setAppPage] = useState(1);
  const [page, setPage] = useState(1);
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void api.githubInstallations(appPage, controller.signal).then(result => {
      if (!controller.signal.aborted) {setInstallations(result); setInstallation(current => result.items.some(item => item.id === current) ? current : result.items[0]?.id ?? 0);}
    }).catch(error => {if (!controller.signal.aborted) onError(error);});
    return () => controller.abort();
  }, [appPage, refresh, onError]);
  useEffect(() => {
    if (!installation) return;
    const controller = new AbortController(); setRepositories(null);
    void api.githubAuthorizedRepositories(installation, page, controller.signal).then(result => {
      if (!controller.signal.aborted) setRepositories(result);
    }).catch(error => {if (!controller.signal.aborted) onError(error);});
    return () => controller.abort();
  }, [installation, page, refresh, onError]);
  return (
    <section className="team-card team-editor repo-connect-card" aria-label="接入仓库面板">
      <div className="repo-connect-header">
        <div className="repo-connect-header-info">
          <div className="repo-connect-title-row">
            <h2>接入仓库</h2>
            {installations?.app_name && (
              <span className="repo-app-badge" title="当前连接的 GitHub App">
                <svg className="repo-badge-icon" width="13" height="13" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.53 1.032 1.53 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z"/>
                </svg>
                <span>{installations.app_name}</span>
              </span>
            )}
          </div>
          <p className="repo-connect-subtitle">
            继续使用现有 GitHub App {installations?.app_name ?? ""}。无需重新创建 App、密钥或回调地址。
          </p>
        </div>
      </div>

      <ol className="repo-connect-steps" aria-label="接入流程步骤">
        <li className={`repo-step-item ${installation ? "is-active" : ""}`}>
          <span className="repo-step-num">1</span>
          <div className="repo-step-text">
            <strong>选择现有 App 的授权账号</strong>
            <small>确认 GitHub App 授权归属的账号或组织</small>
          </div>
        </li>
        <li className={`repo-step-item ${installation && !repository ? "is-active" : ""}`}>
          <span className="repo-step-num">2</span>
          <div className="repo-step-text">
            <strong>选择仓库与权限</strong>
            <small>选择仓库；列表没有目标仓库时，为同一个 App 补充授权，再返回刷新。</small>
          </div>
        </li>
        <li className={`repo-step-item ${repository ? "is-active" : ""}`}>
          <span className="repo-step-num">3</span>
          <div className="repo-step-text">
            <strong>检查并启用审查</strong>
            <small>检查访问与平台接入条件，通过后启用审查。</small>
          </div>
        </li>
      </ol>

      <div className="repo-fields-container">
        <div className="repo-field-card">
          <div className="repo-field-top">
            <label htmlFor="repo-account-select" className="repo-field-label">
              <span className="repo-field-label-text">GitHub 账号</span>
              <span className="repo-field-tip">选择 GitHub App 已安装的个人或组织账号</span>
            </label>
            {(appPage > 1 || installations?.has_more) && (
              <div className="repo-page-nav" aria-label="账号翻页">
                <Button variant="outline"
                  type="button"
                  className="repo-page-btn"
                  disabled={appPage === 1}
                  onClick={() => setAppPage(value => value - 1)}
                >
                  上一页账号
                </Button>
                <span className="repo-page-indicator">第 {appPage} 页</span>
                <Button variant="outline"
                  type="button"
                  className="repo-page-btn"
                  disabled={!installations?.has_more}
                  onClick={() => setAppPage(value => value + 1)}
                >
                  下一页账号
                </Button>
              </div>
            )}
          </div>
          <div className="repo-select-wrapper">
            <NativeSelect
              id="repo-account-select"
              aria-label="GitHub 账号"
              value={installation}
              onChange={event => {
                setInstallation(Number(event.target.value));
                setPage(1);
                setRepository("");
              }}
            >
              {installations?.items.map(item => (
                <option key={item.id} value={item.id}>
                  {item.account} · {item.selection === "all" ? "全部仓库已授权" : "部分仓库已授权"}
                </option>
              ))}
            </NativeSelect>
          </div>
        </div>

        <div className="repo-field-card">
          <div className="repo-field-top">
            <label htmlFor="repo-repository-select" className="repo-field-label">
              <span className="repo-field-label-text">已授权的仓库</span>
              <span className="repo-field-tip">从当前已授权代码库中指定接入对象</span>
            </label>
            {(page > 1 || repositories?.has_more) && (
              <div className="repo-page-nav" aria-label="仓库翻页">
                <Button variant="outline"
                  type="button"
                  className="repo-page-btn"
                  disabled={page === 1 || busy}
                  onClick={() => setPage(value => value - 1)}
                >
                  上一页仓库
                </Button>
                <span className="repo-page-indicator">第 {page} 页</span>
                <Button variant="outline"
                  type="button"
                  className="repo-page-btn"
                  disabled={!repositories?.has_more || busy}
                  onClick={() => setPage(value => value + 1)}
                >
                  下一页仓库
                </Button>
              </div>
            )}
          </div>
          <div className="repo-select-wrapper">
            <NativeSelect
              id="repo-repository-select"
              aria-label="已授权的仓库"
              value={repository}
              onChange={event => setRepository(event.target.value)}
            >
              <option value="">请选择仓库</option>
              {repositories?.items
                .filter(item => !existing || item.repository.toLowerCase() === existing.repository.toLowerCase())
                .map(item => (
                  <option key={item.id} value={item.repository}>
                    {item.repository}
                  </option>
                ))}
            </NativeSelect>
          </div>
        </div>
      </div>

      {repositories?.selection === "all" ? (
        <div className="repo-status-callout is-success">
          <div className="repo-status-icon">✓</div>
          <div className="repo-status-content">
            <p className="repo-status-text">该账号的所有仓库已经授权，直接选择即可。</p>
            <Button variant="outline"
              type="button"
              className="repo-callout-btn"
              disabled={busy}
              onClick={() => setRefresh(value => value + 1)}
            >
              已返回平台，刷新授权列表
            </Button>
          </div>
        </div>
      ) : (
        <div className="repo-status-callout is-warning">
          <div className="repo-status-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10"/>
              <line x1="12" y1="8" x2="12" y2="12"/>
              <line x1="12" y1="16" x2="12.01" y2="16"/>
            </svg>
          </div>
          <div className="repo-status-content">
            <p className="repo-status-text">如果目标仓库不在列表中，请在 GitHub 给现有 App 增加该仓库的权限。</p>
            <div className="repo-status-actions">
              {(repositories?.manage_url || installations?.authorize_url) && (
                <a
                  className="repo-callout-link"
                  target="_blank"
                  rel="noreferrer"
                  href={repositories?.manage_url ?? installations?.authorize_url}
                >
                  到 GitHub 补充仓库授权
                </a>
              )}
              <Button variant="outline"
                type="button"
                className="repo-callout-btn"
                disabled={busy}
                onClick={() => setRefresh(value => value + 1)}
              >
                已返回平台，刷新授权列表
              </Button>
            </div>
          </div>
        </div>
      )}

      <div className="repo-security-note">
        <div className="repo-security-icon">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
            <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
          </svg>
        </div>
        <p>访问检查通过后，平台仅允许这个已选择的仓库接入。新 PR 会自动触发审查，具体范围按项目设置执行。</p>
      </div>

      <div className="ws-form-actions repo-actions">
        <Button variant="default"
          type="button"
          className="team-primary repo-primary-btn"
          disabled={busy || !installation || !repository || !repositories?.items.some(item => item.repository === repository)}
          onClick={() => {
            setBusy(true);
            void api.saveTeamRepository({
              expected_revision: existing?.revision ?? 0,
              repository,
              installation_id: installation,
              policy: {
                approval_timeout_hours: 24,
                budget_warning_percent: 80,
                incremental_review: false,
                target_branches: [],
                ...existing?.policy,
                enabled: true
              }
            }, existing?.id)
              .then(onSaved)
              .catch(onError)
              .finally(() => setBusy(false));
          }}
        >
          {busy ? "正在检查…" : "检查并启用审查"}
        </Button>
        <Button variant="outline" type="button" className="repo-cancel-btn" disabled={busy} onClick={onCancel}>
          取消
        </Button>
        <span className="ws-hint repo-action-hint">检查通过后将允许该仓库接入并按项目设置触发审查</span>
      </div>
    </section>
  );
}

