import { Button } from "./components/ui/button";
import { NativeSelect } from "./components/ui/native-select";
import { Table, TableHeader, TableRow, TableHead, TableBody, TableCell } from "./components/ui/table";
import { Textarea } from "./components/ui/textarea";
import { Input } from "./components/ui/input";
import { Notice } from "./Feedback";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "./api";
import Pagination from "./Pagination";
import { platformApi } from "./platform-api";
import type { PlatformPanelProps } from "./PlatformPage";
import { hasPermission } from "./rbac";
import type { AuthUser, WorkItem, WorkItemUpdate } from "./types";
import { useCursorPage } from "./useCursorPage";
import { formatDate } from "./utils";
import { WorkspaceBack, WorkspaceBadge, WorkspaceEmpty, WorkspaceSection } from "./Workspace";

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
  if (learning) return <LearningEditor key={`${learning.id}:${learning.revision}`} item={learning} onError={onError} onClose={() => setLearning(null)} />;
  if (editing) return <>
    <WorkspaceBack onClick={() => { if (!busy) setEditing(null); }}>返回待办列表</WorkspaceBack>
    {message && <Notice kind="success" onDismiss={() => setMessage("")}>{message}</Notice>}
    <WorkEditor key={`${editing.id}:${editing.revision}`} item={editing} busy={busy} onCancel={() => setEditing(null)} onSave={body => void save(editing, body)}
      onLearn={hasPermission(user, "knowledge:manage") && (editing.status === "resolved" || editing.status === "wont_fix") ? () => { setLearning(editing); setEditing(null); } : undefined} />
  </>;
  const active = mode === "issues" ? page : approvals;
  return <>
    {message && <Notice kind="success" onDismiss={() => setMessage("")}>{message}</Notice>}
    {editable && source && mode === "issues" && <form className="platform-inline-form" onSubmit={event => void create(event)}><div><strong>来自审查结果的问题</strong><p>加入待办后，可安排负责人和截止时间。</p></div><Button variant="default" className="ws-primary" disabled={busy} type="submit">加入我的待办</Button></form>}
    <section className="team-card">
      <div className="team-toolbar"><div><h2>团队待办</h2><p>跟进问题处理与待审批审查。</p></div><Button variant="outline" disabled={busy || active.loading} onClick={() => void active.refresh()}>刷新</Button></div>
      <div className="ws-filterbar">
        <label>类型<NativeSelect value={mode} onChange={event => setMode(event.target.value as typeof mode)}><option value="issues">问题处理</option>{hasPermission(user, "reviews:approve") && <option value="approvals">等待审批</option>}</NativeSelect></label>
        {mode === "issues" && <label>处理状态<NativeSelect value={status} onChange={event => setStatus(event.target.value)}><option value="">全部状态</option>{Object.entries(statuses).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</NativeSelect></label>}
        <label className="ws-check"><input type="checkbox" checked={mine} onChange={event => setMine(event.target.checked)} />只看我的待办</label>
        <label className="ws-check"><input type="checkbox" checked={overdue} onChange={event => setOverdue(event.target.checked)} />已超期</label>
      </div>
      {mode === "issues" ? <div className="team-table-wrap"><Table><TableHeader><TableRow><TableHead>问题 / 来源</TableHead><TableHead>负责人</TableHead><TableHead>截止时间</TableHead><TableHead>处理状态</TableHead><TableHead>操作</TableHead></TableRow></TableHeader><TableBody>{page.data?.items.map(item => <TableRow key={item.id}>
        <TableCell><strong>{item.title}</strong><small>{item.repository} · PR #{item.pull_request_number}</small></TableCell><TableCell>{item.assignee ?? "未分配"}</TableCell><TableCell>{item.due_at ? formatDate(item.due_at) : "未设置"}</TableCell>
        <TableCell><WorkspaceBadge tone={item.status === "resolved" ? "success" : item.status === "in_progress" ? "accent" : "neutral"}>{statuses[item.status]}</WorkspaceBadge></TableCell>
        <TableCell><div className="ws-cell-actions"><a href={`#review/${encodeURIComponent(item.source_run_id)}`}>来源</a>{editable && <Button variant="outline" className="ws-link-button" disabled={busy} onClick={() => setEditing(item)}>处理</Button>}</div></TableCell>
      </TableRow>)}</TableBody></Table></div> : <div className="team-table-wrap"><Table><TableHeader><TableRow><TableHead>审查任务</TableHead><TableHead>负责人</TableHead><TableHead>进入审批</TableHead><TableHead>截止时间</TableHead><TableHead>操作</TableHead></TableRow></TableHeader><TableBody>{approvals.data?.items.map(item => <TableRow key={item.id}>
        <TableCell><strong>{item.repository}</strong><small>PR #{item.pull_request_number}</small></TableCell><TableCell>{item.assignee ?? "按角色处理"}</TableCell><TableCell>{item.requested_at ? formatDate(item.requested_at) : "历史未记录"}</TableCell><TableCell>{item.due_at ? formatDate(item.due_at) : "未设置"}</TableCell><TableCell><a className="ws-button-link" href={`#review/${encodeURIComponent(item.id)}`}>查看并审批 →</a></TableCell>
      </TableRow>)}</TableBody></Table></div>}
      {!active.loading && !active.data?.items.length && <WorkspaceEmpty title={mode === "issues" ? "当前没有待处理问题" : "当前没有待审批任务"} description={mode === "issues" ? "可调整筛选条件，或从审查问题卡片加入待办。" : "需要人工批准的审查会集中显示在这里。"} />}
      <Pagination page={active.page} count={active.data?.items.length ?? 0} hasNext={Boolean(active.data?.next_cursor)} busy={active.loading} onPrevious={active.previous} onNext={active.next} />
    </section>
    <p className="ws-hint">问题是否有效与是否完成修复分别记录；处理状态由成员确认。</p>
  </>;
}

function LearningEditor({ item, onError, onClose }: PlatformPanelProps & { item: WorkItem; onClose: () => void }) {
  const [lesson, setLesson] = useState(item.note);
  const [revision, setRevision] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  useEffect(() => { const controller = new AbortController(); void api.knowledgeDocuments(false, controller.signal).then(value => setRevision(value.revision)).catch(error => { if (!controller.signal.aborted) onError(error); }); return () => controller.abort(); }, [onError]);
  return <><WorkspaceBack onClick={() => { if (!busy) onClose(); }}>返回待办列表</WorkspaceBack><section className="team-card ws-editor">
    <div className="ws-editor-heading"><div><h2>整理仓库经验</h2><p>{item.repository} · {item.title}</p></div><WorkspaceBadge tone={saved ? "success" : "neutral"}>{saved ? "草稿已保存" : "待审核草稿"}</WorkspaceBadge></div>
    {saved ? <div className="ws-empty" role="status"><strong>知识草稿已保存。</strong><p>审核启用后生效。</p><a className="ws-button-link" href="#knowledge">前往知识库审核 →</a></div> : <form onSubmit={async event => { event.preventDefault(); if (revision == null) return; setBusy(true); try { await platformApi.proposeKnowledge(item.id, { expected_work_revision: item.revision, expected_library_revision: revision, lesson }); setSaved(true); } catch (error) { onError(error); } finally { setBusy(false); } }}><fieldset disabled={busy}>
      <WorkspaceSection title="经验内容" description="从人工处理结论中提取可复用规则，说明适用范围。"><label>经验与适用条件<Textarea required rows={7} maxLength={4000} value={lesson} onChange={event => setLesson(event.target.value)} /></label><p className="ws-hint">仅适用于当前仓库。使用固定方案时，审核后还需保存新方案。</p></WorkspaceSection>
      <div className="ws-form-actions"><Button variant="default" className="ws-primary" disabled={revision == null} type="submit">保存为未启用知识草稿</Button><Button variant="outline" type="button" onClick={onClose}>关闭草稿编辑</Button></div>
    </fieldset></form>}
  </section></>;
}

function WorkEditor({ item, busy, onSave, onCancel, onLearn }: { item: WorkItem; busy: boolean; onSave: (body: WorkItemUpdate) => void; onCancel: () => void; onLearn?: () => void }) {
  const [status, setStatus] = useState(item.status);
  const [assignee, setAssignee] = useState(item.assignee ?? "");
  const [due, setDue] = useState(toInputDate(item.due_at));
  const [note, setNote] = useState(item.note);
  const [fixPr, setFixPr] = useState(item.fix_pull_request_number?.toString() ?? "");
  return <section className="team-card ws-editor"><div className="ws-editor-heading"><div><h2>处理 · {item.title}</h2><p>{item.repository} · PR #{item.pull_request_number}</p></div><WorkspaceBadge>版本 {item.revision}</WorkspaceBadge></div>
    <form onSubmit={event => { event.preventDefault(); onSave({ expected_revision: item.revision, status, assignee: assignee.trim() || null, due_at: due ? new Date(due).toISOString() : null, note, fix_pull_request_number: fixPr ? Number(fixPr) : null }); }}><fieldset disabled={busy}>
      <WorkspaceSection title="处理安排" description="确认状态、负责人和计划完成时间。"><div className="team-form-grid">
        <label>处理状态<NativeSelect aria-label="修改处理状态" value={status} onChange={event => setStatus(event.target.value as typeof status)}>{Object.entries(statuses).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</NativeSelect></label>
        <label>负责人用户名<Input value={assignee} maxLength={100} onChange={event => setAssignee(event.target.value)} /></label>
        <label>截止时间<Input type="datetime-local" value={due} onChange={event => setDue(event.target.value)} /></label>
        <label>关联修复 PR 编号<Input type="number" min={1} value={fixPr} onChange={event => setFixPr(event.target.value)} /><small>同一仓库的修复 PR，由人工核对。</small></label>
      </div></WorkspaceSection>
      <WorkspaceSection title="处理结论" description="确认修复或暂不修复时，需要说明依据。"><label>处理说明<Textarea value={note} required={status === "resolved" || status === "wont_fix"} maxLength={2000} rows={5} onChange={event => setNote(event.target.value)} /></label></WorkspaceSection>
      <div className="ws-form-actions is-sticky"><Button variant="default" className="ws-primary" type="submit">保存处理结果</Button><Button variant="outline" type="button" onClick={onCancel}>取消</Button>{onLearn && <Button variant="outline" type="button" onClick={onLearn}>将处理结论整理为知识草稿</Button>}</div>
    </fieldset></form>
  </section>;
}
