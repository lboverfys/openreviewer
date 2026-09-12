import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api } from "./api";
import EvaluationImportPanel from "./EvaluationImportPanel";
import EvaluationSourcePicker from "./EvaluationSourcePicker";
import Pagination from "./Pagination";
import type { AuthUser, EvaluationCaseDetail, EvaluationDataset, EvaluationDecision, EvaluationFinding, EvaluationObservationDetail, EvaluationReference, EvaluationVariant } from "./types";
import { useCursorPage } from "./useCursorPage";
import { shortSha } from "./utils";

export const assessmentLabels: Record<string, string> = {pending:"待复核", partial:"复核中", disputed:"存在分歧", complete:"双人复核完成"};
const categories: Record<EvaluationReference["category"], string> = {authorization:"权限", security:"安全", database:"数据库", business_contract:"业务契约", architecture:"架构", test_gap:"测试缺口", reliability:"可靠性"};
const verdicts: Record<EvaluationDecision["verdict"], string> = {valid:"有效问题", false_positive:"误报", duplicate:"重复问题", out_of_scope:"超出范围", known_issue:"已知问题"};

export default function EvaluationCasePanel({ dataset, caseId, user, canEdit, onError, onChanged }: {
  dataset: EvaluationDataset; caseId: string; user: AuthUser; canEdit: boolean;
  onError: (error: unknown) => void; onChanged: () => void;
}) {
  const [sample, setSample] = useState<EvaluationCaseDetail | null>(null);
  const [variant, setVariant] = useState<EvaluationVariant>("baseline");
  const [editingReference, setEditingReference] = useState(false);
  const [importing, setImporting] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const data = await api.evaluationCase(caseId, signal);
      if (data.dataset_id !== dataset.id) throw new Error("样本不属于当前评测集");
      if (!signal?.aborted) setSample(data);
    }
    catch (error) {if (!signal?.aborted) onError(error);}
  }, [caseId, dataset.id, onError]);
  useEffect(() => {const controller = new AbortController(); void load(controller.signal); return () => controller.abort();}, [load]);
  const updated = useCallback(() => {void load(); onChanged();}, [load, onChanged]);
  async function reviewReference(agrees: boolean) {
    if (!sample) return;
    setBusy(true);
    try {setSample(await api.reviewEvaluationReference(caseId, sample.revision, agrees, note)); setNote(""); onChanged();}
    catch (error) {onError(error);} finally {setBusy(false);}
  }
  if (!sample) return <section className="evaluation-card" role="status">正在加载样本…</section>;
  const editable = canEdit && !dataset.archived_at;
  return <section className="evaluation-case">
    <div className="evaluation-card">
      <div className="evaluation-toolbar"><div><h2>PR #{sample.pull_request_number} · {sample.title}</h2><p className="evaluation-hint">{sample.repository} · <code>{shortSha(sample.head_sha)}</code> · {sample.split === "validation" ? "验收集" : "调参集"}</p></div>
        <button type="button" disabled={busy} onClick={() => void load()}>刷新样本</button></div>
      <a href={"https://github.com/" + sample.repository + "/pull/" + sample.pull_request_number + "/files/" + sample.head_sha} target="_blank" rel="noreferrer">在 GitHub 核对该提交</a>
      <div className="evaluation-reference">
        <div className="evaluation-toolbar"><h3>参考缺陷</h3><span>{sample.reference_status === "confirmed" ? "两位成员已确认" : sample.reference_status === "disputed" ? "参考标签存在分歧" : "待双人确认"}</span>
          {editable && <button type="button" onClick={() => setEditingReference(value => !value)}>{editingReference ? "关闭参考编辑" : "编辑参考标签"}</button>}</div>
        {sample.reference_defects == null ? <p className="evaluation-empty">尚未标注参考缺陷，已知缺陷找回率暂不计算。</p>
          : sample.reference_defects.length === 0 ? <p>当前标签为“没有已知缺陷”，仍需双人确认。</p>
          : <ul>{sample.reference_defects.map(item => <li key={item.key}><strong>{item.title}</strong> · {categories[item.category]} {item.file && <code>{item.file}{item.start_line ? ":" + item.start_line : ""}</code>}</li>)}</ul>}
        <div className="evaluation-reviewers">{sample.reference_reviews.map(item => <p key={item.reviewer}>{item.reviewer}：{item.agrees ? "认可参考标签" : "不认可参考标签"}{item.note ? " · " + item.note : ""}</p>)}</div>
        {editable && sample.reference_defects != null && <div className="evaluation-actions">
          <label>参考复核说明<input maxLength={1000} value={note} onChange={event => setNote(event.target.value)} /></label>
          <button type="button" disabled={busy} onClick={() => void reviewReference(true)}>认可参考标签</button>
          <button type="button" disabled={busy} onClick={() => void reviewReference(false)}>参考标签有问题</button>
        </div>}
      </div>
      {editingReference && editable && <ReferenceEditor key={caseId} sample={sample} onSaved={data => {setSample(data); setEditingReference(false); onChanged();}} onError={onError} />}
    </div>
    <nav className="evaluation-tabs" aria-label="观察分组">
      {(["baseline","candidate"] as const).map(group => <button type="button" key={group} aria-pressed={variant === group} onClick={() => {setVariant(group); setImporting(false);}}>
        {group === "baseline" ? "基线" : "候选"} · {sample[group] ? assessmentLabels[sample[group].assessment_status] : "未收录"}
      </button>)}
    </nav>
    {sample[variant] ? <ObservationPanel key={caseId + ":" + variant} sample={sample} variant={variant} user={user} editable={editable} onError={onError} onChanged={updated} />
      : <section className="evaluation-card"><p>该组尚未收录。请选择同一 PR、同一提交的另一条已完成运行。</p>{editable && <button type="button" onClick={() => setImporting(true)}>收录该组运行</button>}</section>}
    {importing && editable && <EvaluationImportPanel key={variant} dataset={dataset} sample={sample}
      onSaved={() => {setImporting(false); updated();}} onCancel={() => setImporting(false)} onError={onError} />}
  </section>;
}

function ReferenceEditor({ sample, onSaved, onError }: {
  sample: EvaluationCaseDetail; onSaved: (sample: EvaluationCaseDetail) => void; onError: (error: unknown) => void;
}) {
  const [mode, setMode] = useState(sample.reference_defects == null ? "unknown" : sample.reference_defects.length ? "defects" : "clean");
  const [references, setReferences] = useState<EvaluationReference[]>(sample.reference_defects ?? []);
  const [revision, setRevision] = useState(sample.revision);
  const [kind, setKind] = useState(sample.kind);
  const [reset, setReset] = useState(false);
  const [busy, setBusy] = useState(false);
  function fresh() {
    setReferences(sample.reference_defects ?? []); setRevision(sample.revision); setKind(sample.kind);
    setMode(sample.reference_defects == null ? "unknown" : sample.reference_defects.length ? "defects" : "clean");
    setReset(false);
  }
  function edit(index: number, values: Partial<EvaluationReference>) {
    setReferences(items => items.map((item, number) => number === index ? {...item, ...values} : item));
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (mode === "defects" && references.length === 0) {onError(new Error("请添加至少一条参考缺陷")); return;}
    setBusy(true);
    try {onSaved(await api.updateEvaluationReference(sample.id, {expected_revision:revision, kind, reset_reviews:reset,
      reference_defects:mode === "unknown" ? null : mode === "clean" ? [] : references}));}
    catch (error) {onError(error);} finally {setBusy(false);}
  }
  return <form className="evaluation-editor" onSubmit={save} aria-label="参考标签编辑">
    <fieldset disabled={busy}>
      <div className="evaluation-form-grid"><label>参考标签类型<select value={mode} onChange={event => setMode(event.target.value)}>
        <option value="unknown">尚未标注</option><option value="clean">没有已知缺陷</option><option value="defects">列出已知缺陷</option></select></label>
        <label>样本类型<select value={kind} onChange={event => setKind(event.target.value as typeof kind)}>
          <option value="normal">正常变更</option><option value="known_defect">已知缺陷</option><option value="cross_file">跨文件问题</option></select></label></div>
      {mode === "defects" && <>
        {references.map((item, index) => <div key={item.key} className="evaluation-reference-row">
          <label>缺陷说明 {index + 1}<input required maxLength={300} value={item.title} onChange={event => edit(index,{title:event.target.value})} /></label>
          <label>风险类型<select value={item.category} onChange={event => edit(index,{category:event.target.value as EvaluationReference["category"]})}>{Object.entries(categories).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label>文件路径<input maxLength={1024} value={item.file ?? ""} onChange={event => edit(index,{file:event.target.value || null})} /></label>
          <label>起始行<input type="number" min={1} value={item.start_line ?? ""} onChange={event => edit(index,{start_line:event.target.value ? Number(event.target.value) : null})} /></label>
          <button type="button" onClick={() => setReferences(items => items.filter((_, number) => number !== index))}>移除缺陷 {index + 1}</button>
        </div>)}
        <button type="button" disabled={references.length >= 30} onClick={() => setReferences(items => [...items,{key:crypto.randomUUID(),title:"",category:"business_contract",file:null,start_line:null}])}>添加参考缺陷</button>
      </>}
      <p className="evaluation-hint">参考标签需要独立人工确认。修改后将重新开始本 PR 的参考确认与两组复核。</p>
      <label className="evaluation-check"><input type="checkbox" checked={reset} onChange={event => setReset(event.target.checked)} />确认清空该样本已有复核记录</label>
      <div className="evaluation-actions"><button type="submit" className="evaluation-primary">{busy ? "正在保存…" : "保存参考标签"}</button>
        <button type="button" onClick={fresh}>载入当前标签</button></div>
    </fieldset>
  </form>;
}

function ObservationPanel({ sample, variant, user, editable, onError, onChanged }: {
  sample: EvaluationCaseDetail; variant: EvaluationVariant; user: AuthUser; editable: boolean;
  onError: (error: unknown) => void; onChanged: () => void;
}) {
  const slot = sample[variant]!;
  const [detail, setDetail] = useState<EvaluationObservationDetail | null>(null);
  const [tab, setTab] = useState<"findings"|"changes"|"versions">("findings");
  const [busy, setBusy] = useState(false);
  const [replacing, setReplacing] = useState(false);
  const [replacement, setReplacement] = useState<string[]>([]);
  const [reset, setReset] = useState(false);
  const handledRevision = useRef(0);
  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const data=await api.evaluationObservation(sample.id,variant,signal);
      if (!signal?.aborted) {setDetail(data); handledRevision.current=data.observation.revision;}
    }
    catch (error) {if (!signal?.aborted) onError(error);}
  }, [sample.id,variant,onError]);
  const snapshot = detail?.observation.snapshot_sha256 ?? slot.snapshot_sha256;
  const loadFindings = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) =>
    api.evaluationFindings(sample.id, variant, snapshot, cursor, signal, force), [sample.id,variant,snapshot]);
  const loadChanges = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) =>
    api.evaluationChanges(sample.id, variant, snapshot, cursor, signal, force), [sample.id,variant,snapshot]);
  const findings = useCursorPage({cacheKey:"evaluation-findings:" + sample.id + ":" + variant + ":" + snapshot, load:loadFindings, onError, enabled:tab === "findings"});
  const changes = useCursorPage({cacheKey:"evaluation-changes:" + sample.id + ":" + variant + ":" + snapshot, load:loadChanges, onError, enabled:tab === "changes"});
  useEffect(() => {
    if (handledRevision.current === slot.revision) return;
    const controller=new AbortController();
    const refreshFindings=handledRevision.current !== 0;
    void load(controller.signal).then(() => {
      if (refreshFindings && !controller.signal.aborted) void findings.refresh(true,controller.signal);
    });
    return () => controller.abort();
  }, [load,slot.revision,findings.refresh]);
  async function action(operation: () => Promise<EvaluationObservationDetail>) {
    setBusy(true);
    try {const result=await operation(); setDetail(result); handledRevision.current=result.observation.revision; await findings.refresh(); onChanged();}
    catch (error) {onError(error);} finally {setBusy(false);}
  }
  async function replace() {
    if (!detail || replacement.length !== 1) {onError(new Error("请选择一条替换运行")); return;}
    setBusy(true);
    try {setDetail(await api.replaceEvaluationObservation(sample.id,variant,replacement[0],detail.observation.revision,reset));
      setReplacing(false); setReplacement([]); setReset(false); onChanged();}
    catch(error){onError(error);} finally{setBusy(false);}
  }
  const page = tab === "changes" ? changes : findings;
  return <section className="evaluation-card" aria-label="观察结果复核">
    <div className="evaluation-toolbar"><div><h3>{variant === "baseline" ? "基线" : "候选"}观察</h3><p>{assessmentLabels[detail?.observation.assessment_status ?? slot.assessment_status]}</p></div>
      <button type="button" disabled={busy} onClick={() => {void load(); void page.refresh();}}>刷新观察</button>
      {editable && <button type="button" disabled={busy} onClick={() => setReplacing(value => !value)}>更换来源运行</button>}
    </div>
    {detail && <>
      <p className="evaluation-hint">来源 <a href={"#review/" + detail.observation.source_run_id}>{detail.observation.source_run_id.slice(0,8)}</a> · {detail.observation.model_label} · {detail.observation.finding_count} 条问题</p>
      <div className="evaluation-reviewers">{detail.ballots.length ? detail.ballots.map(item => <span key={item.reviewer}>{item.reviewer}：{item.decision_count}/{detail.observation.finding_count} 条 · {item.submitted_at ? "已提交" : "草稿"}</span>) : <span>尚无复核。需要两位不同成员分别提交。</span>}</div>
      <nav className="evaluation-tabs" aria-label="复核内容">{([["findings","问题复核"],["changes","变更代码"],["versions","模型与版本"]] as const).map(([key,label]) => <button key={key} type="button" aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}</nav>
      {tab === "findings" && <>
        {findings.data?.items.map(item => <FindingEditor key={item.finding.id} item={item} user={user} references={sample.reference_defects ?? []} disabled={busy || !editable}
          onSave={decision => void action(() => api.saveEvaluationFindingReview(sample.id,variant,item.finding.id,detail.observation.revision,decision))} />)}
        {findings.data?.items.length === 0 && !findings.loading && <p className="evaluation-empty">本次审查没有输出问题。请核对参考缺陷与变更代码后提交复核，遗漏问题会体现在已知缺陷找回率中。</p>}
      </>}
      {tab === "changes" && <>
        <p className="evaluation-hint">这里保存收录时的 PR 审查变更。未改动的关联文件可到 GitHub 的同一提交核对。</p>
        {changes.data?.items.map(item => <article className="evaluation-code" key={item.file}><h4>{item.file}</h4><pre>{item.patch}</pre></article>)}
        {changes.data?.items.length === 0 && !changes.loading && <p className="evaluation-empty">该快照没有可展示的审查变更。</p>}
      </>}
      {tab !== "versions" && <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={busy || page.loading} onPrevious={page.previous} onNext={page.next} label={tab === "changes" ? "变更代码分页" : "评测问题分页"} />}
      {tab === "versions" && <div className="evaluation-provenance">
        {detail.source.limitations.map(item => <p className="evaluation-notice" key={item}>{item}</p>)}
        {detail.source.models.map((model,index) => <article key={model.agent + ":" + index}><h4>{model.agent} · {model.provider} / {model.model}</h4>
          <p>Prompt：{model.prompt_version} · 程序版本：{model.application_revision ? shortSha(model.application_revision) : "历史未记录"}</p>
          <p>知识引用：{Object.entries(model.knowledge_versions ?? {}).map(([name,version]) => name + "@" + version).join("、") || (model.context_recorded ? "本次没有引用" : "历史未记录")}</p></article>)}
        <details><summary>仓库规则与检索索引</summary>
          {detail.source.rule_versions.map(item => <p key={item.source}><code>{item.source}</code> · {item.sha256}</p>)}
          {detail.source.retrieval.map((item,index) => <p key={item.index_id + ":" + index}>{item.agent} · {item.strategy || "策略未记录"} · 索引 {item.index_id.slice(0,12)} · 提交 {shortSha(item.index_head_sha)} · {item.embedding_model}</p>)}
        </details><small>快照校验值：{detail.observation.snapshot_sha256}</small>
      </div>}
      {editable && <div className="evaluation-actions"><button type="button" className="evaluation-primary" disabled={busy} onClick={() => void action(() => api.submitEvaluationReview(sample.id,variant,detail.observation.revision))}>{busy ? "正在保存…" : "提交本次复核"}</button>
        <span className="evaluation-hint">保存每条结论后提交；再次编辑会将自己的复核改回草稿。</span></div>}
      {replacing && editable && <div className="evaluation-editor"><p>更换运行会清空该组已有复核，参考标签保留。</p>
        <EvaluationSourcePicker datasetId={sample.dataset_id} caseId={sample.id} single selected={replacement} onSelected={setReplacement} onError={onError} disabled={busy}
          excludeRunId={sample[variant === "baseline" ? "candidate" : "baseline"]?.source_run_id} />
        <label className="evaluation-check"><input type="checkbox" checked={reset} onChange={event => setReset(event.target.checked)} />确认重置该组已有复核</label>
        <button type="button" disabled={busy} onClick={() => void replace()}>保存替换</button></div>}
    </>}
    {!detail && <p role="status">正在加载观察结果…</p>}
  </section>;
}

function FindingEditor({ item, user, references, disabled, onSave }: {
  item: EvaluationFinding; user: AuthUser; references: EvaluationReference[]; disabled: boolean;
  onSave: (decision: EvaluationDecision) => void;
}) {
  const mine = item.reviews.find(review => review.reviewer === user.username)?.decision;
  const [verdict,setVerdict]=useState<EvaluationDecision["verdict"]|"">(mine?.verdict ?? "");
  const [location,setLocation]=useState(mine?.location_correct == null ? "" : String(mine.location_correct));
  const [reference,setReference]=useState(mine?.reference_key ?? "");
  const [note,setNote]=useState(mine?.note ?? "");
  useEffect(() => {
    setVerdict(mine?.verdict ?? ""); setLocation(mine?.location_correct == null ? "" : String(mine.location_correct));
    setReference(mine?.reference_key ?? ""); setNote(mine?.note ?? "");
  }, [mine?.verdict,mine?.location_correct,mine?.reference_key,mine?.note]);
  const submit=(event: FormEvent) => {event.preventDefault(); if (!verdict) return; onSave({verdict,
    location_correct:location === "" ? null : location === "true", reference_key:verdict === "valid" && reference ? reference : null, note});};
  return <article className="evaluation-finding">
    <div className="evaluation-toolbar"><h4>{item.finding.title}</h4><span>{item.finding.severity} · {categories[item.finding.category]}</span></div>
    <p><code>{item.finding.file ?? "未定位"}{item.finding.start_line ? ":" + item.finding.start_line : ""}</code></p>
    <details open><summary>证据与影响</summary><pre>{item.finding.evidence}</pre><p>{item.finding.impact}</p><p>{item.finding.suggestion}</p></details>
    <div className="evaluation-reviewers">{item.reviews.map(review => <span key={review.reviewer}>{review.reviewer}：{verdicts[review.decision.verdict]}{review.submitted_at ? "（已提交）" : "（草稿）"}{review.decision.note ? " · " + review.decision.note : ""}</span>)}</div>
    <form onSubmit={submit}><fieldset disabled={disabled}><div className="evaluation-form-grid">
      <label>我的结论<select required value={verdict} onChange={event => setVerdict(event.target.value as typeof verdict)}><option value="">请选择结论</option>{Object.entries(verdicts).map(([key,label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <label>定位是否正确<select value={location} onChange={event => setLocation(event.target.value)}><option value="">未判断</option><option value="true">正确</option><option value="false">错误</option></select></label>
      <label>对应参考缺陷<select value={reference} disabled={verdict !== "valid"} onChange={event => setReference(event.target.value)}><option value="">没有对应 / 新发现的问题</option>{references.map(item => <option key={item.key} value={item.key}>{item.title}</option>)}</select></label>
      <label>复核说明<input maxLength={1000} value={note} onChange={event => setNote(event.target.value)} /></label>
    </div><button type="submit">保存我的结论</button></fieldset></form>
  </article>;
}
