import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "./api";
import Pagination from "./Pagination";
import { platformApi } from "./platform-api";
import type { PlatformPanelProps } from "./PlatformPage";
import { useCursorPage } from "./useCursorPage";
import { formatDate } from "./utils";
import ProfileActivationPanel from "./ProfileActivationPanel";

export default function ReviewProfilesPanel({ onError }: PlatformPanelProps) {
  const repositories = useCursorPage({ cacheKey: "team-repositories", load: api.teamRepositories, onError });
  const [repositoryId, setRepositoryId] = useState("");
  const selected = repositories.data?.items.find(item => item.id === repositoryId);
  const repository = selected?.repository ?? "";
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [aiRevision, setAiRevision] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [pendingProfile, setPendingProfile] = useState<string | null>(null);
  const load = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.profiles(repository, cursor, signal, force), [repository]);
  const profiles = useCursorPage({ cacheKey: `profiles:${repository}`, load, onError });
  useEffect(() => { const controller = new AbortController(); void api.aiSettings(controller.signal).then(value => setAiRevision(value.revision)).catch(error => { if (!controller.signal.aborted) onError(error); }); return () => controller.abort(); }, [onError]);
  const create = async (event: FormEvent) => {
    event.preventDefault(); if (!selected || aiRevision == null) return; setBusy(true); setMessage("");
    try { await platformApi.createProfile({ name, note, repository, expected_ai_revision: aiRevision }); setName(""); setNote(""); setMessage("已保存不可变方案，可在下方启用。"); await profiles.refresh(); }
    catch (error) { onError(error); } finally { setBusy(false); }
  };
  const activate = async (id: string) => {
    if (!selected) return; setMessage(""); setPendingProfile(id);
  };
  return <>
    {pendingProfile && selected && <ProfileActivationPanel key={`${pendingProfile}:${repository}`} id={pendingProfile} repository={repository} revision={selected.revision} onError={onError} onCancel={() => setPendingProfile(null)} onDone={async () => { setPendingProfile(null); setMessage("仓库已切换方案，新任务将使用此版本。"); await repositories.refresh(); }} />}
    <section className="team-card"><h2>保存与启用审查方案</h2><p className="team-hint">方案同时保存模型配置、Prompt、知识内容和检索参数。选择历史方案即可恢复配置；已创建的任务保持原方案。</p>
      <label>选择仓库<select value={repositoryId} onChange={event => setRepositoryId(event.target.value)}><option value="">请选择仓库</option>{repositories.data?.items.map(item => <option key={item.id} value={item.id}>{item.repository} · 策略 v{item.revision}</option>)}</select></label>
      <Pagination label="仓库选择分页" page={repositories.page} count={repositories.data?.items.length ?? 0} hasNext={Boolean(repositories.data?.next_cursor)} busy={repositories.loading} onPrevious={repositories.previous} onNext={repositories.next} />
      <form onSubmit={event => void create(event)}><fieldset disabled={busy || !selected}><div className="team-form-grid"><label>方案名称<input required value={name} maxLength={120} onChange={event => setName(event.target.value)} placeholder="例如：Java 服务审查 · 第一版" /></label><label>变更说明<input value={note} maxLength={1000} onChange={event => setNote(event.target.value)} /></label></div><p>当前 AI 配置版本：{aiRevision ?? "读取中"} · <a href="#settings">调整 AI 配置</a> · <a href="#knowledge">维护知识</a> · <a href="#retrieval">调整检索</a></p><button type="submit" disabled={aiRevision == null}>保存当前配置为方案</button></fieldset></form>
      <button disabled={busy} onClick={() => { void repositories.refresh(); void profiles.refresh(); void api.aiSettings(undefined, true).then(value => setAiRevision(value.revision)).catch(onError); }}>刷新配置版本</button>
      {message && <p role="status" className="team-success">{message}</p>}
    </section>
    <section className="team-card"><h2>方案历史{repository ? ` · ${repository}` : ""}</h2>
      {profiles.data?.items.map(item => <article className="platform-profile" key={item.id}><div><h3>{item.name} {selected?.policy.review_profile_id === item.id && <small>当前启用</small>}</h3><p>{item.repository} · {formatDate(item.created_at)} · {item.created_by}</p><p>{item.note}</p><p>模型：{Object.entries(item.models).map(([key, value]) => `${key}: ${value}`).join("；")}</p><p>知识文档 {Object.keys(item.knowledge_versions).length} 份 · Prompt {item.prompt_version} · 版本 {item.fingerprint.slice(0, 12)}</p></div><button disabled={busy || !selected || selected.repository !== item.repository || selected.policy.review_profile_id === item.id} onClick={() => void activate(item.id)}>启用 / 恢复此方案</button></article>)}
      {!profiles.loading && !profiles.data?.items.length && <p className="platform-empty">尚未保存审查方案。现有仓库继续使用原有配置。</p>}
      <Pagination page={profiles.page} count={profiles.data?.items.length ?? 0} hasNext={Boolean(profiles.data?.next_cursor)} busy={profiles.loading} onPrevious={profiles.previous} onNext={profiles.next} />
    </section>
  </>;
}
