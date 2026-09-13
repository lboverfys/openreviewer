import { useCallback, useEffect, useState, type FormEvent } from "react";
import { platformApi } from "./platform-api";
import type { StaticReport } from "./types";
import { useCursorPage } from "./useCursorPage";
import Pagination from "./Pagination";

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
  return <details className="team-card" onToggle={event => setOpen(event.currentTarget.open)}><summary>静态检查佐证</summary>
    <p>导入同提交的 Semgrep SARIF 报告。静态线索与 AI 判断、人工确认分别保留；同一位置并不代表同一个缺陷，导入不会自动发布评论。</p>
    {!loaded && open && <p role="status">正在读取报告…</p>}
    {report && <><p>{report.tool} {report.tool_version} · 新增 {report.new_count} · 已有 {report.existing_count} · 待判断 {report.unknown_count}</p><p>导入人 {report.imported_by} · 报告指纹 {report.report_hash.slice(0, 12)}</p>
      {findings.data?.items.map(item => <article className="platform-profile" key={item.id}><h4>{item.rule_id}</h4><p>{item.message}</p><p>{item.file}:{item.start_line}–{item.end_line} · {({ new: "基线未出现", existing: "基线已出现", unknown: "新增状态待判断" })[item.baseline_state]} · 同位置 AI 问题 {item.overlapping_ai_count} 条</p></article>)}
      <Pagination page={findings.page} count={findings.data?.items.length ?? 0} hasNext={Boolean(findings.data?.next_cursor)} busy={findings.loading} onPrevious={findings.previous} onNext={findings.next} />
    </>}
    {loaded && !report && editable && <form onSubmit={event => void submit(event)}><fieldset disabled={busy}>
      <label>当前提交报告（Semgrep SARIF，最大 1 MB）<input type="file" accept=".sarif,.json" required onChange={event => void readFile(event.target.files?.[0], setHead)} /></label>
      <label>基线提交报告（可选，同一扫描器版本）<input type="file" accept=".sarif,.json" onChange={event => void readFile(event.target.files?.[0], setBase)} /></label>
      {base && <label>基线 SHA<input value={baseSha} required pattern="[0-9a-f]{40,64}" maxLength={64} onChange={event => setBaseSha(event.target.value)} /></label>}
      <p>没有基线或稳定问题标识时显示“待判断”；报告保存后不可覆盖。</p><button type="submit" disabled={!head}>导入静态报告</button>
    </fieldset></form>}
    {loaded && !report && !editable && <p>尚未导入静态报告。</p>}
  </details>;
}
