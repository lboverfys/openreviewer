import { useCallback, useEffect, useState, type FormEvent } from "react";
import { platformApi } from "./platform-api";
import type { StaticReport } from "./types";
import { useCursorPage } from "./useCursorPage";
import Pagination from "./Pagination";
import { WorkspaceBadge, WorkspaceEmpty } from "./Workspace";

export default function StaticAnalysisPanel({ runId, headSha, editable, onError }: {
  runId: string; headSha: string; editable: boolean; onError: (error: unknown) => void;
}) {
  const [open, setOpen] = useState(false);
  const [report, setReport] = useState<StaticReport | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [head, setHead] = useState("");
  const [base, setBase] = useState("");
  const [baseSha, setBaseSha] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.staticFindings(runId, cursor, signal, force), [runId]);
  const findings = useCursorPage({ cacheKey: `static:${runId}`, load, onError, enabled: open && Boolean(report) });
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController(); setLoaded(false);
    void platformApi.staticReport(runId, controller.signal).then(value => { setReport(value); setLoaded(true); }).catch(error => { if (!controller.signal.aborted) onError(error); });
    return () => controller.abort();
  }, [open, runId, onError]);
  const readFile = async (file: File | undefined, set: (value: string) => void) => {
    set(""); if (!file) return;
    try { if (file.size > 1_000_000) throw new Error("单份 SARIF 最大 1 MB"); set(await file.text()); } catch (error) { onError(error); }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try { setReport(await platformApi.importStaticReport(runId, { head_sha: headSha, base_sha: base ? baseSha : null, head_sarif: head, base_sarif: base || null })); setHead(""); setBase(""); await findings.refresh(); }
    catch (error) { onError(error); } finally { setBusy(false); }
  };
  return <details className="workspace-surface static-analysis" onToggle={event => setOpen(event.currentTarget.open)}><summary><strong>扫描工具报告（可选）</strong><span className="ws-hint">有 Semgrep 报告时再导入</span></summary>
    {open && <div className="static-analysis-content"><p className="ws-hint">核对静态线索与 AI 判断，同一位置不代表同一个缺陷。</p>
      {!loaded && <WorkspaceEmpty loading title="正在读取报告…" />}
      {report && <><div className="ws-toolbar"><div className="profile-heading"><strong>{report.tool} {report.tool_version}</strong><WorkspaceBadge tone="accent">基线未出现 {report.new_count}</WorkspaceBadge><WorkspaceBadge>已有 {report.existing_count}</WorkspaceBadge><WorkspaceBadge tone="warning">待判断 {report.unknown_count}</WorkspaceBadge></div></div>
        {findings.data?.items.map(item => <article className="static-finding" key={item.id}><div className="ws-toolbar"><h4>{item.rule_id}</h4><WorkspaceBadge tone={item.baseline_state === "new" ? "accent" : "neutral"}>{({ new: "基线未出现", existing: "基线已出现", unknown: "新增状态待判断" })[item.baseline_state]}</WorkspaceBadge></div><p>{item.message}</p><div className="profile-metadata"><code>{item.file}:{item.start_line}–{item.end_line}</code><span>同位置 AI 问题 {item.overlapping_ai_count} 条</span></div></article>)}
        {loaded && !findings.loading && findings.data?.items.length === 0 && <WorkspaceEmpty title="此报告没有静态线索" />}
        <Pagination page={findings.page} count={findings.data?.items.length ?? 0} hasNext={Boolean(findings.data?.next_cursor)} busy={findings.loading} onPrevious={findings.previous} onNext={findings.next} />
        <details className="profile-version"><summary>来源与版本</summary><p>导入人 {report.imported_by} · 报告指纹 <code>{report.report_hash.slice(0, 12)}</code></p><p>报告提交 <code>{report.head_sha}</code></p></details>
      </>}
      {loaded && !report && editable && <form onSubmit={event => void submit(event)}><fieldset disabled={busy}>
        <div className="static-upload-grid"><label className="static-file-field">当前提交报告<small>Semgrep SARIF · 最大 1 MB</small><input aria-label="当前提交报告（Semgrep SARIF，最大 1 MB）" type="file" accept=".sarif,.json" required onChange={event => void readFile(event.target.files?.[0], setHead)} /></label>
          <label className="static-file-field">基线提交报告<small>可选 · 同一扫描器版本</small><input aria-label="基线提交报告（可选，同一扫描器版本）" type="file" accept=".sarif,.json" onChange={event => void readFile(event.target.files?.[0], setBase)} /></label></div>
        {base && <div className="ws-filterbar"><label>基线 SHA<input value={baseSha} required pattern="[0-9a-f]{40,64}" maxLength={64} onChange={event => setBaseSha(event.target.value)} /></label></div>}
        <div className="ws-form-actions"><button className="ws-primary" type="submit" disabled={!head}>导入静态报告</button><span className="ws-hint">当前提交 <code>{headSha.slice(0, 12)}</code></span></div>
        <p className="ws-hint">没有基线或稳定标识时保留“待判断”。导入不会自动发布评论，保存后不可覆盖。</p>
      </fieldset></form>}
      {loaded && !report && !editable && <WorkspaceEmpty title="尚未导入静态报告。" />}
    </div>}
  </details>;
}
