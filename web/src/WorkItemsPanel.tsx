import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "./api";
import Pagination from "./Pagination";
import { platformApi } from "./platform-api";
import type { PlatformPanelProps } from "./PlatformPage";
import { hasPermission } from "./rbac";
import type { AuthUser, WorkItem, WorkItemUpdate } from "./types";
import { useCursorPage } from "./useCursorPage";
import { formatDate } from "./utils";

const statuses: Record<WorkItem["status"], string> = { open: "待处理", in_progress: "处理中", resolved: "人工确认修复", wont_fix: "暂不修复" };
const toInputDate = (value: string | null | undefined) => value ? new Date(new Date(value).getTime() - new Date(value).getTimezoneOffset() * 60_000).toISOString().slice(0, 16) : "";

export default function WorkItemsPanel({ user, findingId, onError }: PlatformPanelProps & { user: AuthUser; findingId?: string }) {
  const [mode, setMode] = useState<"issues" | "approvals">("issues");
  const [mine, setMine] = useState(true);
  const [status, setStatus] = useState("");
  const [overdue, setOverdue] = useState(false);
  const [source, setSource] = useState(findingId ?? "");
  const [editing, setEditing] = useState<WorkItem | null>(null);
  const [learning, setLearning] = useState<WorkItem | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const editable = hasPermission(user, "findings:adjudicate");
  const load = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.workItems(mine, status, overdue, cursor, signal, force), [mine, status, overdue]);
  const loadApprovals = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.approvals(mine, overdue, cursor, signal, force), [mine, overdue]);
  const page = useCursorPage({ cacheKey: `work:${mine}:${status}:${overdue}`, load, onError, enabled: mode === "issues" });
  const approvals = useCursorPage({ cacheKey: `approvals:${mine}:${overdue}`, load: loadApprovals, onError, enabled: mode === "approvals" });
  const create = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setMessage("");
    try { const item = await platformApi.createWork({ finding_id: source, assignee: user.username }); setEditing(item); setSource(""); setMessage("工作项已创建，可以安排处理时间。"); await page.refresh(); }
    catch (error) { onError(error); } finally { setBusy(false); }
  };
  const save = async (item: WorkItem, body: WorkItemUpdate) => {
    setBusy(true); setMessage("");
    try { await platformApi.updateWork(item.id, body); setEditing(null); setMessage("处理结果已保存并记录操作人。"); await page.refresh(); }
    catch (error) { onError(error); } finally { setBusy(false); }
  };
  return <>
    <section className="team-card"><div className="team-toolbar"><h2>团队待办</h2><label>类型<select value={mode} onChange={event => { setMode(event.target.value as typeof mode); setEditing(null); }}><option value="issues">问题处理</option>{hasPermission(user, "reviews:approve") && <option value="approvals">等待审批</option>}</select></label><label><input type="checkbox" checked={mine} onChange={event => setMine(event.target.checked)} />只看我的待办</label><label><input type="checkbox" checked={overdue} onChange={event => setOverdue(event.target.checked)} />已超期</label><button disabled={busy} onClick={() => void (mode === "issues" ? page : approvals).refresh()}>刷新</button></div>
      {message && <p className="team-success" role="status">{message}</p>}
      {mode === "issues" && <>
        <p className="team-hint">处理状态由成员明确确认。审查报告中的问题是否有效，以及问题是否完成修复，分别记录。</p>
        <label>处理状态<select value={status} onChange={event => setStatus(event.target.value)}><option value="">全部状态</option>{Object.entries(statuses).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <div className="team-table-wrap"><table><thead><tr><th>问题</th><th>负责人</th><th>截止时间</th><th>状态</th><th>操作</th></tr></thead><tbody>{page.data?.items.map(item => <tr key={item.id}><td>{item.title}<small>{item.repository} · PR #{item.pull_request_number}</small></td><td>{item.assignee ?? "未分配"}</td><td>{item.due_at ? formatDate(item.due_at) : "未设置"}</td><td>{statuses[item.status]}</td><td><a href={`#review/${encodeURIComponent(item.source_run_id)}`}>来源</a>{editable && <button disabled={busy} onClick={() => setEditing(item)}>处理</button>}</td></tr>)}</tbody></table></div>
        {!page.loading && !page.data?.items.length && <p className="platform-empty">当前筛选下没有工作项。可从审查问题卡片加入待办。</p>}
        <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={page.loading} onPrevious={page.previous} onNext={page.next} />
        {editable && source && <form className="platform-inline-form" onSubmit={event => void create(event)}><p>将刚选择的审查问题加入我的待办，再安排负责人和截止时间。</p><button disabled={busy} type="submit">加入我的待办</button></form>}
      </>}
      {mode === "approvals" && <>
        <p className="team-hint">未指定负责人的审查由有权限的成员处理；旧任务缺少截止时间时不推算超期。</p>
        <div className="team-table-wrap"><table><thead><tr><th>审查任务</th><th>负责人</th><th>进入审批</th><th>截止时间</th><th>操作</th></tr></thead><tbody>{approvals.data?.items.map(item => <tr key={item.id}><td>{item.repository} · PR #{item.pull_request_number}</td><td>{item.assignee ?? "按角色处理"}</td><td>{item.requested_at ? formatDate(item.requested_at) : "历史未记录"}</td><td>{item.due_at ? formatDate(item.due_at) : "未设置"}</td><td><a href={`#review/${encodeURIComponent(item.id)}`}>查看并审批</a></td></tr>)}</tbody></table></div>
        {!approvals.loading && !approvals.data?.items.length && <p className="platform-empty">当前没有待审批任务。</p>}
        <Pagination page={approvals.page} count={approvals.data?.items.length ?? 0} hasNext={Boolean(approvals.data?.next_cursor)} busy={approvals.loading} onPrevious={approvals.previous} onNext={approvals.next} />
      </>}
    </section>
    {editing && <WorkEditor key={`${editing.id}:${editing.revision}`} item={editing} busy={busy} onCancel={() => setEditing(null)} onSave={body => void save(editing, body)} />}
    {editing && hasPermission(user, "knowledge:manage") && (editing.status === "resolved" || editing.status === "wont_fix") && <button onClick={() => setLearning(editing)}>将处理结论整理为知识草稿</button>}
    {learning && <LearningEditor key={`${learning.id}:${learning.revision}`} item={learning} onError={onError} onClose={() => setLearning(null)} />}
  </>;
}

function LearningEditor({ item, onError, onClose }: PlatformPanelProps & { item: WorkItem; onClose: () => void }) {
  const [lesson, setLesson] = useState(item.note);
  const [revision, setRevision] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  useEffect(() => { const controller = new AbortController(); void api.knowledgeDocuments(false, controller.signal).then(value => setRevision(value.revision)).catch(error => { if (!controller.signal.aborted) onError(error); }); return () => controller.abort(); }, [onError]);
  return <section className="team-card"><h2>整理仓库经验</h2><p className="team-hint">来源为当前工作项的人工处理结论。保存后处于未启用状态，仅适用于 {item.repository}；审核启用后，固定审查方案的仓库还需保存新方案。</p>
    {saved ? <p role="status">知识草稿已保存。<a href="#knowledge">前往知识库审核</a></p> : <form onSubmit={async event => { event.preventDefault(); if (revision == null) return; setBusy(true); try { await platformApi.proposeKnowledge(item.id, { expected_work_revision: item.revision, expected_library_revision: revision, lesson }); setSaved(true); } catch (error) { onError(error); } finally { setBusy(false); } }}><label>经验与适用条件<textarea required rows={5} maxLength={4000} value={lesson} onChange={event => setLesson(event.target.value)} /></label><button disabled={busy || revision == null} type="submit">保存为未启用知识草稿</button></form>}
    <button onClick={onClose}>关闭草稿编辑</button>
  </section>;
}

function WorkEditor({ item, busy, onSave, onCancel }: { item: WorkItem; busy: boolean; onSave: (body: WorkItemUpdate) => void; onCancel: () => void }) {
  const [status, setStatus] = useState(item.status);
  const [assignee, setAssignee] = useState(item.assignee ?? "");
  const [due, setDue] = useState(toInputDate(item.due_at));
  const [note, setNote] = useState(item.note);
  const [fixPr, setFixPr] = useState(item.fix_pull_request_number?.toString() ?? "");
  return <section className="team-card"><h2>处理 · {item.title}</h2><form onSubmit={event => { event.preventDefault(); onSave({ expected_revision: item.revision, status, assignee: assignee.trim() || null, due_at: due ? new Date(due).toISOString() : null, note, fix_pull_request_number: fixPr ? Number(fixPr) : null }); }}><fieldset disabled={busy}><div className="team-form-grid"><label>处理状态<select aria-label="修改处理状态" value={status} onChange={event => setStatus(event.target.value as typeof status)}>{Object.entries(statuses).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</select></label><label>负责人用户名<input value={assignee} maxLength={100} onChange={event => setAssignee(event.target.value)} /></label><label>截止时间<input type="datetime-local" value={due} onChange={event => setDue(event.target.value)} /></label><label>关联修复 PR 编号<input type="number" min={1} value={fixPr} onChange={event => setFixPr(event.target.value)} /><small>属于同一仓库，由人工核对修复内容。</small></label></div><label>处理说明<textarea value={note} required={status === "resolved" || status === "wont_fix"} maxLength={2000} rows={4} onChange={event => setNote(event.target.value)} /></label><div className="team-form-actions"><button type="submit">保存处理结果</button><button type="button" onClick={onCancel}>取消</button></div></fieldset></form></section>;
}
