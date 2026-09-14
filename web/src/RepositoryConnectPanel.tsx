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
  return <section className="team-card team-editor">
    <h2>接入仓库</h2><p>继续使用现有 GitHub App {installations?.app_name ?? ""}。无需重新创建 App、密钥或回调地址。</p>
    <ol><li>选择现有 App 的授权账号。</li><li>选择仓库；列表没有目标仓库时，为同一个 App 补充授权，再返回刷新。</li><li>检查访问与平台接入条件，通过后启用审查。</li></ol>
    <label>GitHub 账号<select value={installation} onChange={event => {setInstallation(Number(event.target.value)); setPage(1); setRepository("");}}>{installations?.items.map(item => <option key={item.id} value={item.id}>{item.account} · {item.selection === "all" ? "全部仓库已授权" : "部分仓库已授权"}</option>)}</select></label>
    {(appPage > 1 || installations?.has_more) && <div><button disabled={appPage === 1} onClick={() => setAppPage(value => value - 1)}>上一页账号</button><button disabled={!installations?.has_more} onClick={() => setAppPage(value => value + 1)}>下一页账号</button></div>}
    <label>已授权的仓库<select value={repository} onChange={event => setRepository(event.target.value)}><option value="">请选择仓库</option>{repositories?.items.filter(item => !existing || item.repository.toLowerCase() === existing.repository.toLowerCase()).map(item => <option key={item.id} value={item.repository}>{item.repository}</option>)}</select></label>
    <div><button disabled={page === 1 || busy} onClick={() => setPage(value => value - 1)}>上一页仓库</button><button disabled={!repositories?.has_more || busy} onClick={() => setPage(value => value + 1)}>下一页仓库</button></div>
    {repositories?.selection === "all" ? <p>该账号的所有仓库已经授权，直接选择即可。</p> : <p>如果目标仓库不在列表中，请在 GitHub 给现有 App 增加该仓库的权限。</p>}
    {repositories?.selection !== "all" && (repositories?.manage_url || installations?.authorize_url) && <a target="_blank" rel="noreferrer" href={repositories?.manage_url ?? installations?.authorize_url}>到 GitHub 补充仓库授权</a>}
    <button disabled={busy} onClick={() => setRefresh(value => value + 1)}>已返回平台，刷新授权列表</button>
    <p>访问检查通过后，平台仅允许这个已选择的仓库接入。新 PR 会自动触发审查，具体范围按项目设置执行。</p>
    <div className="ws-form-actions"><button className="team-primary" disabled={busy || !installation || !repository || !repositories?.items.some(item => item.repository === repository)} onClick={() => {
      setBusy(true);
      void api.saveTeamRepository({expected_revision: existing?.revision ?? 0, repository, installation_id: installation, policy: {approval_timeout_hours: 24, budget_warning_percent: 80, incremental_review: false, target_branches: [], ...existing?.policy, enabled: true}}, existing?.id)
        .then(onSaved).catch(onError).finally(() => setBusy(false));
    }}>{busy ? "正在检查…" : "检查并启用审查"}</button><button disabled={busy} onClick={onCancel}>取消</button></div>
  </section>;
}
