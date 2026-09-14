import { DetailDialog, Notice } from "./Feedback";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "./api";
import Pagination from "./Pagination";
import { platformApi } from "./platform-api";
import type { PlatformPanelProps } from "./PlatformPage";
import { useCursorPage } from "./useCursorPage";
import { formatDate } from "./utils";
import ProfileActivationPanel from "./ProfileActivationPanel";
import { WorkspaceBack, WorkspaceBadge, WorkspaceEmpty, WorkspaceSection } from "./Workspace";

const agentLabels: Record<string, string> = { security: "安全", convention: "规范", logic: "逻辑", summary: "汇总" };

export default function ReviewProfilesPanel({ onError }: PlatformPanelProps) {
  const repositories = useCursorPage({ cacheKey: "team-repositories", load: api.teamRepositories, onError });
  const [repositoryId, setRepositoryId] = useState("");
  const selected = repositories.data?.items.find(item => item.id === repositoryId);
  const repository = selected?.repository ?? "";
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [aiRevision, setAiRevision] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [message, setMessage] = useState("");
  const [pendingProfile, setPendingProfile] = useState<string | null>(null);
  const load = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.profiles(repository, cursor, signal, force), [repository]);
  const profiles = useCursorPage({ cacheKey: `profiles:${repository}`, load, onError, enabled: Boolean(selected) });
  useEffect(() => { if (repositories.data?.items.length && !repositories.data.items.some(item => item.id === repositoryId)) setRepositoryId(repositories.data.items[0].id); }, [repositories.data, repositoryId]);
  useEffect(() => { if (!creating) return; const controller = new AbortController(); void api.aiSettings(controller.signal).then(value => setAiRevision(value.revision)).catch(error => { if (!controller.signal.aborted) onError(error); }); return () => controller.abort(); }, [creating, onError]);
  const refreshAi = () => void api.aiSettings(undefined, true).then(value => setAiRevision(value.revision)).catch(onError);
  const create = async (event: FormEvent) => {
    event.preventDefault(); if (!selected || aiRevision == null) return; setBusy(true); setMessage("");
    try { await platformApi.createProfile({ name, note, repository, expected_ai_revision: aiRevision }); setName(""); setNote(""); setCreating(false); setMessage("已保存不可变方案，可在下方启用。"); await profiles.refresh(); }
    catch (error) { onError(error); } finally { setBusy(false); }
  };
  if (pendingProfile && selected) return <><WorkspaceBack onClick={() => setPendingProfile(null)}>返回方案历史</WorkspaceBack>
    <ProfileActivationPanel key={`${pendingProfile}:${repository}`} id={pendingProfile} repository={repository} revision={selected.revision} onError={onError} onCancel={() => setPendingProfile(null)} onDone={async () => { setPendingProfile(null); setMessage("仓库已切换方案，新任务将使用此版本。"); await repositories.refresh(); }} />
  </>;
  if (creating) return <><WorkspaceBack onClick={() => { if (!busy) setCreating(false); }}>返回方案历史</WorkspaceBack><section className="team-card ws-editor">
    <div className="ws-editor-heading"><div><h2>保存新的审查方案</h2><p>{repository || "请先选择仓库"}</p></div><WorkspaceBadge tone="accent">配置快照</WorkspaceBadge></div>
    <form onSubmit={event => void create(event)}><fieldset disabled={busy || !selected}>
      <WorkspaceSection title="方案信息" description="保存当前模型、Prompt、知识和检索配置，后续可恢复此版本。"><label>方案名称<input required value={name} maxLength={120} onChange={event => setName(event.target.value)} placeholder="例如：Java 服务审查 · 第一版" /></label><label>变更说明<textarea value={note} rows={3} maxLength={1000} onChange={event => setNote(event.target.value)} placeholder="说明本次调整的目的" /></label></WorkspaceSection>
      <WorkspaceSection title="配置来源" description="确认当前配置后保存，启用时再查看质量依据。"><div className="ws-note">当前 AI 配置版本：{aiRevision ?? "读取中"}</div><div className="ws-actions"><a className="ws-button-link" href="#settings">AI 配置</a><a className="ws-button-link" href="#knowledge">知识库</a><a className="ws-button-link" href="#retrieval">检索设置</a><button type="button" onClick={refreshAi}>刷新配置版本</button></div></WorkspaceSection>
      <div className="ws-form-actions"><button className="ws-primary" type="submit" disabled={aiRevision == null}>保存当前配置为方案</button><button type="button" onClick={() => setCreating(false)}>取消</button></div>
    </fieldset></form>
  </section></>;
  return <>
    {message && <Notice kind="success" onDismiss={() => setMessage("")}>{message}</Notice>}
    <section className="team-card"><div className="team-toolbar"><div><h2>审查方案</h2><p>为仓库保存、启用或恢复一套审查配置。</p></div><div className="ws-actions"><button disabled={busy || repositories.loading || profiles.loading} onClick={() => { void repositories.refresh(); if (selected) void profiles.refresh(); }}>刷新</button><button className="ws-primary" disabled={!selected} onClick={() => { setCreating(true); setMessage(""); }}>新建审查方案</button></div></div>
      <div className="profile-repository-picker ws-filterbar"><label>选择仓库<select value={repositoryId} onChange={event => { setRepositoryId(event.target.value); setMessage(""); }}><option value="">请选择仓库</option>{repositories.data?.items.map(item => <option key={item.id} value={item.id}>{item.repository}</option>)}</select></label>{selected && <WorkspaceBadge tone={selected.policy.review_profile_id ? "accent" : "neutral"}>{selected.policy.review_profile_id ? "已绑定审查方案" : "使用当前配置"}</WorkspaceBadge>}
        <Pagination label="仓库选择分页" page={repositories.page} count={repositories.data?.items.length ?? 0} hasNext={Boolean(repositories.data?.next_cursor)} busy={repositories.loading} onPrevious={repositories.previous} onNext={repositories.next} />
      </div>
    </section>
    <section className="team-card"><div className="team-toolbar"><h2>方案历史{repository ? ` · ${repository}` : ""}</h2><span className="ws-hint">已创建任务保持原方案</span></div>
      <div className="profile-history">{selected && profiles.data?.items.map(item => <article className="platform-profile" key={item.id}><div><div className="profile-heading"><h3>{item.name}</h3>{selected.policy.review_profile_id === item.id && <WorkspaceBadge tone="accent">当前启用</WorkspaceBadge>}</div>{item.note && <p>{item.note}</p>}
        <div className="profile-models">{Object.entries(item.models).map(([key, value]) => <span key={key}><b>{agentLabels[key] ?? key}</b>{value}</span>)}</div>
        <div className="profile-metadata"><span>{formatDate(item.created_at)}</span><span>{item.created_by}</span><span>知识 {Object.keys(item.knowledge_versions).length} 份</span></div>
        <DetailDialog className="profile-version"><summary>版本信息</summary><p>Prompt {item.prompt_version} · 版本 {item.fingerprint.slice(0, 12)}</p></DetailDialog>
      </div><button className="ws-link-button" disabled={busy || selected.policy.review_profile_id === item.id} onClick={() => { setPendingProfile(item.id); setMessage(""); }}>启用 / 恢复此方案</button></article>)}</div>
      {!repositories.loading && !selected && <WorkspaceEmpty title="先选择一个仓库" description="尚未添加仓库时，可先前往团队管理配置。" />}
      {selected && !profiles.loading && !profiles.data?.items.length && <WorkspaceEmpty title="尚未保存审查方案" description="保存当前配置，建立可追溯、可恢复的方案版本。" />}
      {selected && <Pagination page={profiles.page} count={profiles.data?.items.length ?? 0} hasNext={Boolean(profiles.data?.next_cursor)} busy={profiles.loading} onPrevious={profiles.previous} onNext={profiles.next} />}
    </section>
  </>;
}
