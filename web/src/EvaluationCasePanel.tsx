import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { NativeSelect } from "./components/ui/native-select";
import { DetailDialog } from "./Feedback";
import { failureReasonLabels } from "./evaluation-labels";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api } from "./api";
import EvaluationImportPanel from "./EvaluationImportPanel";
import EvaluationOverviewPanel from "./EvaluationOverviewPanel";
import EvaluationOutputPanel from "./EvaluationOutputPanel";
import EvaluationSourcePicker from "./EvaluationSourcePicker";
import Pagination from "./Pagination";
import type { AuthUser, EvaluationCaseDetail, EvaluationDataset, EvaluationDecision, EvaluationFinding, EvaluationObservationDetail, EvaluationReference, EvaluationVariant } from "./types";
import { useCursorPage } from "./useCursorPage";
import { shortSha } from "./utils";
import { WorkspaceBadge, WorkspaceEmpty } from "./Workspace";

export const assessmentLabels: Record<string, string> = {pending:"待复核", partial:"复核中", disputed:"需重新核对", complete:"已完成核对"};
const categories: Record<EvaluationReference["category"], string> = {authorization:"权限", security:"安全", database:"数据库", business_contract:"业务契约", architecture:"架构", test_gap:"测试缺口", reliability:"可靠性"};
const verdicts: Record<EvaluationDecision["verdict"], string> = {valid:"有效问题", false_positive:"误报", duplicate:"重复问题", out_of_scope:"超出范围", known_issue:"已知问题", uncertain:"暂不确定"};

export default function EvaluationCasePanel({ dataset, caseId, user, canEdit, onError, onChanged }: {
  dataset: EvaluationDataset; caseId: string; user: AuthUser; canEdit: boolean;
  onError: (error: unknown) => void; onChanged: () => void;
}) {
  const [sample, setSample] = useState<EvaluationCaseDetail | null>(null);
  const [variant, setVariant] = useState<EvaluationVariant>("baseline");
  const [showResults, setShowResults] = useState(false);
  const [compareResults, setCompareResults] = useState(false);
  const [editingReference, setEditingReference] = useState(false);
  const [importing, setImporting] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const data = await api.evaluationCase(caseId, signal);
      if (data.dataset_id !== dataset.id) throw new Error("样本不属于当前评测集");
      if (!signal?.aborted) {setSample(data); if (!data.baseline && data.candidate) setVariant("candidate");}
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
  if (!sample) return <section className="evaluation-card"><WorkspaceEmpty loading title="正在加载样本…" /></section>;
  const editable = canEdit && !dataset.archived_at;
  if (editingReference && editable) return <section className="evaluation-card ws-editor"><div className="ws-editor-heading"><div><h2>编辑参考标签</h2><p>PR #{sample.pull_request_number} · {sample.title}</p></div><div className="ws-actions"><WorkspaceBadge>版本 {sample.revision}</WorkspaceBadge><Button variant="outline" type="button" onClick={() => setEditingReference(false)}>取消编辑</Button></div></div><ReferenceEditor key={caseId} sample={sample} onSaved={data => {setSample(data); setEditingReference(false); onChanged();}} onError={onError} /></section>;
  if (importing && editable) return <EvaluationImportPanel key={variant} dataset={dataset} sample={sample} onSaved={() => {setImporting(false); setVariant(sample.baseline ? "candidate" : "baseline"); setShowResults(false); updated();}} onCancel={() => setImporting(false)} onError={onError} />;
  return <section className="evaluation-case">
    <div className="evaluation-card">
      <p className="evaluation-hint">{dataset.review_mode === "dual" ? "双人独立验收：先参与的两位成员分别核对全部问题并提交。首次提交前隐藏对方判断；修改后需要重新提交。" : "单人日常核对：以最近保存的判断为准。"}</p>
      <div className="evaluation-toolbar"><div><h2>PR #{sample.pull_request_number} · {sample.title}</h2><p className="evaluation-hint">{sample.repository} · <code>{shortSha(sample.head_sha)}</code> · {sample.split === "validation" ? "验收集" : "调参集"}</p></div>
        <Button variant="outline" type="button" disabled={busy} onClick={() => void load()}>刷新样本</Button></div>
      <a href={"https://github.com/" + sample.repository + "/pull/" + sample.pull_request_number + "/files/" + sample.head_sha} target="_blank" rel="noreferrer">在 GitHub 核对该提交</a>
      <DetailDialog className="evaluation-reference"><summary>核对已知漏报（可选）</summary>
        <div className="evaluation-toolbar"><h3>参考缺陷</h3><WorkspaceBadge tone={sample.reference_status === "confirmed" ? "success" : sample.reference_status === "disputed" ? "danger" : "warning"}>{sample.reference_status === "confirmed" ? "已由核对人确认" : sample.reference_status === "disputed" ? "参考标签存在分歧" : "尚未确认"}</WorkspaceBadge>
          {editable && <Button variant="outline" type="button" onClick={() => setEditingReference(value => !value)}>{editingReference ? "关闭参考编辑" : "编辑参考标签"}</Button>}</div>
        {sample.reference_defects == null ? <WorkspaceEmpty title="尚未标注参考缺陷" description="补充人工参考标签后，才能计算已知缺陷找回率。" />
          : sample.reference_defects.length === 0 ? <p>当前标签为“没有已知缺陷”，可由你确认。</p>
          : <ul>{sample.reference_defects.map(item => <li key={item.key}><strong>{item.title}</strong> · {categories[item.category]} {item.file && <code>{item.file}{item.start_line ? ":" + item.start_line : ""}</code>}</li>)}</ul>}
        <div className="evaluation-reviewers">{sample.reference_reviews.map(item => <p key={item.reviewer}>{item.reviewer}：{item.agrees ? "认可参考标签" : "不认可参考标签"}{item.note ? " · " + item.note : ""}</p>)}</div>
        {editable && sample.reference_defects != null && <div className="evaluation-actions">
          <label>参考复核说明<Input maxLength={1000} value={note} onChange={event => setNote(event.target.value)} /></label>
          <Button variant="outline" type="button" disabled={busy} onClick={() => void reviewReference(true)}>认可参考标签</Button>
          <Button variant="outline" type="button" disabled={busy} onClick={() => void reviewReference(false)}>参考标签有问题</Button>
        </div>}
      </DetailDialog>
    </div>
    <div className="evaluation-card evaluation-workflow">
      <nav className="evaluation-tabs" aria-label="核对步骤">
        <Button variant="outline" type="button" aria-pressed={!showResults} onClick={() => setShowResults(false)}>1 · 核对问题</Button>
        <Button variant="outline" type="button" aria-pressed={showResults} onClick={() => setShowResults(true)}>2 · 查看统计</Button>
      </nav>
      {sample.baseline && sample.candidate ? <div className="evaluation-comparison-options" role="group" aria-label="选择要核对的审查">
        {(["baseline", "candidate"] as const).map(group => <Button variant="outline" type="button" key={group} aria-pressed={variant === group} onClick={() => {setVariant(group); setShowResults(false);}}>
          <strong>{group === "baseline" ? "第一份审查" : "第二份审查"} · {sample[group]!.source_run_id.slice(0, 8)}</strong>
          <span>提交 {shortSha(sample.head_sha)} · {sample[group]!.model_label}</span><small>{assessmentLabels[sample[group]!.assessment_status]}</small>
        </Button>)}
      </div> : editable && <Button variant="outline" type="button" className="ws-button-link" onClick={() => setImporting(true)}>比较另一份审查（可选）</Button>}
    </div>
    {showResults ? <>
      <EvaluationOverviewPanel datasetId={dataset.id} caseId={sample.id} variant={variant} version={sample.revision} onError={onError}/>
      {sample.baseline && sample.candidate && <>
        <Button variant="outline" type="button" onClick={() => setCompareResults(value => !value)}>{compareResults ? "收起对比" : "查看两份统计对比"}</Button>
        {compareResults && <DetailDialog open={compareResults} hideTrigger onToggle={event => setCompareResults(event.currentTarget.open)}><summary>两份审查统计对比 · 同一提交 {shortSha(sample.head_sha)}</summary>
          <h3>第一份 · {sample.baseline.model_label} · {sample.baseline.source_run_id.slice(0, 8)}</h3><EvaluationOverviewPanel datasetId={dataset.id} caseId={sample.id} variant="baseline" version={sample.revision} onError={onError}/>
          <h3>第二份 · {sample.candidate.model_label} · {sample.candidate.source_run_id.slice(0, 8)}</h3><EvaluationOverviewPanel datasetId={dataset.id} caseId={sample.id} variant="candidate" version={sample.revision} onError={onError}/>
        </DetailDialog>}
      </>}
      <Button variant="outline" type="button" onClick={() => setShowResults(false)}>返回继续核对</Button>
    </> : sample[variant] && <ObservationPanel key={caseId + ":" + variant} sample={sample} variant={variant} reviewMode={dataset.review_mode} user={user} editable={editable} onError={onError} onChanged={updated} onComplete={() => setShowResults(true)} />}

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
  return <form className="evaluation-reference-editor" onSubmit={save} aria-label="参考标签编辑">
    <fieldset disabled={busy}>
      <div className="evaluation-form-grid"><label>参考标签类型<NativeSelect value={mode} onChange={event => setMode(event.target.value)}>
        <option value="unknown">尚未标注</option><option value="clean">没有已知缺陷</option><option value="defects">列出已知缺陷</option></NativeSelect></label>
        <label>样本类型<NativeSelect value={kind} onChange={event => setKind(event.target.value as typeof kind)}>
          <option value="normal">正常变更</option><option value="known_defect">已知缺陷</option><option value="cross_file">跨文件问题</option></NativeSelect></label></div>
      {mode === "defects" && <>
        {references.map((item, index) => <div key={item.key} className="evaluation-reference-row">
          <label>缺陷说明 {index + 1}<Input required maxLength={300} value={item.title} onChange={event => edit(index,{title:event.target.value})} /></label>
          <label>风险类型<NativeSelect value={item.category} onChange={event => edit(index,{category:event.target.value as EvaluationReference["category"]})}>{Object.entries(categories).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</NativeSelect></label>
          <label>文件路径<Input maxLength={1024} value={item.file ?? ""} onChange={event => edit(index,{file:event.target.value || null})} /></label>
          <label>起始行<Input type="number" min={1} value={item.start_line ?? ""} onChange={event => edit(index,{start_line:event.target.value ? Number(event.target.value) : null})} /></label>
          <Button variant="outline" type="button" onClick={() => setReferences(items => items.filter((_, number) => number !== index))}>移除缺陷 {index + 1}</Button>
        </div>)}
        <Button variant="outline" type="button" disabled={references.length >= 30} onClick={() => setReferences(items => [...items,{key:crypto.randomUUID(),title:"",category:"business_contract",file:null,start_line:null}])}>添加参考缺陷</Button>
      </>}
      <p className="evaluation-hint">参考缺陷由你核对确认。修改会重置这条记录的已有判断，请确认后保存。</p>
      <label className="evaluation-check"><input type="checkbox" checked={reset} onChange={event => setReset(event.target.checked)} />确认清空该样本已有复核记录</label>
      <div className="evaluation-actions"><Button variant="default" type="submit" className="evaluation-primary">{busy ? "正在保存…" : "保存参考标签"}</Button>
        <Button variant="outline" type="button" onClick={fresh}>载入当前标签</Button></div>
    </fieldset>
  </form>;
}

function ObservationPanel({ sample, variant, reviewMode, user, editable, onError, onChanged, onComplete }: {
  sample: EvaluationCaseDetail; variant: EvaluationVariant; reviewMode: EvaluationDataset["review_mode"]; user: AuthUser; editable: boolean;
  onError: (error: unknown) => void; onChanged: () => void; onComplete: () => void;
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
  async function action(operation: () => Promise<EvaluationObservationDetail>, complete = false) {
    setBusy(true);
    try {const result=await operation(); setDetail(result); handledRevision.current=result.observation.revision; await findings.refresh(); onChanged(); if (complete) onComplete();}
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
  return <section className="evaluation-card" aria-label="审查问题核对">
    <div className="evaluation-toolbar"><div><h3>逐条判断问题</h3><p>{assessmentLabels[detail?.observation.assessment_status ?? slot.assessment_status]}</p></div><div className="ws-actions">
      <Button variant="outline" type="button" disabled={busy} onClick={() => {void load(); void page.refresh();}}>刷新问题</Button>
      {editable && <Button variant="outline" type="button" disabled={busy} onClick={() => setReplacing(value => !value)}>{replacing ? "取消更换" : "更换这份审查"}</Button>}
    </div></div>
    {detail && !replacing && <>
      <div className="evaluation-observation-meta"><a href={"#review/" + detail.observation.source_run_id}>查看来源审查 →</a><span>{detail.observation.model_label}</span><span>配置版本 {detail.source.configuration_revision ?? "历史未记录"}</span><WorkspaceBadge>{detail.observation.finding_count} 条问题</WorkspaceBadge></div>
      <p className="evaluation-hint">这里展示来源运行当时的模型与结果，修改当前模型配置不会改写这份快照；复核也不会自动再发起模型调用。</p>
      <div className="evaluation-reviewers">{detail.ballots.length ? detail.ballots.map(item => <span key={item.reviewer}>{item.reviewer}：{item.decision_count}/{detail.observation.finding_count} 条 · {item.submitted_at ? "已提交" : "草稿"}</span>) : <span>{reviewMode === "dual" ? "请独立核对全部问题后提交；双人验收需要两位不同成员的提交。" : "由当前账号核对即可完成，无需第二个账号。"}</span>}</div>
      <nav className="evaluation-tabs" aria-label="复核内容">{([["findings","核对问题"],["changes","变更代码"],["versions","模型与版本"]] as const).map(([key,label]) => <Button variant="outline" key={key} type="button" aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</Button>)}</nav>
      {tab === "findings" && <>
        {findings.data?.items.map(item => <FindingEditor key={item.finding.id} item={item} user={user} references={sample.reference_defects ?? []} disabled={busy || !editable}
          onSave={decision => void action(() => api.saveEvaluationFindingReview(sample.id,variant,item.finding.id,detail.observation.revision,decision))} />)}
        {findings.data?.items.length === 0 && !findings.loading && <WorkspaceEmpty title="本次审查没有输出问题" description={reviewMode === "dual" ? "两位成员仍需分别提交独立复核；未提供可靠的已知缺陷清单时，不计算找回率。" : "你可以直接完成核对；未提供可靠的已知缺陷清单时，不计算找回率。"} />}
      </>}
      {tab === "changes" && <>
        <p className="evaluation-hint">这里保存收录时的 PR 审查变更。未改动的关联文件可到 GitHub 的同一提交核对。</p>
        {changes.data?.items.map(item => <DetailDialog className="evaluation-code" key={item.file}><summary>{item.file}</summary><pre>{item.patch}</pre></DetailDialog>)}
        {changes.data?.items.length === 0 && !changes.loading && <WorkspaceEmpty title="该快照没有可展示的审查变更" />}
      </>}
      {tab !== "versions" && <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={busy || page.loading} onPrevious={page.previous} onNext={page.next} label={tab === "changes" ? "变更代码分页" : "评测问题分页"} />}
      {tab === "versions" && <div className="evaluation-provenance">
        {detail.source.model_output_evidence?.capture_requested ? <>
          <p>收录时的调用输出：{detail.source.model_output_evidence.captured_count} / {detail.source.model_output_evidence.request_count} 份完整，{detail.source.model_output_evidence.incomplete_count} 份缺失或不完整。
            {detail.source.model_output_evidence.expires_at && new Date(detail.source.model_output_evidence.expires_at).getTime() <= Date.now() ? " 在线留存已到期，请核对受控归档。" : " 完整性以各调用当前状态和归档校验为准。"}</p>
          <EvaluationOutputPanel runId={detail.source.review_run_id} onError={onError} />
        </> : <p>本次未启用调用输出留存，或历史未记录；结构化审查快照不能代替供应商原始输出。</p>}
        <p>提交 {shortSha(sample.head_sha)} · 配置版本 {detail.source.configuration_revision ?? "历史未记录"} · 审查范围版本 <code>{detail.source.plan_fingerprint}</code></p>
        {detail.source.limitations.map(item => <p className="evaluation-notice" key={item}>{item}</p>)}
        {detail.source.models.map((model,index) => <article key={model.agent + ":" + index}><h4>{model.agent} · {model.provider} / {model.model}</h4>
          <p>Prompt 协议：{model.prompt_protocol_version ?? model.prompt_version} · 内容 {model.prompt_content_sha256?.slice(0,12) ?? "历史未记录"} · 程序版本：{model.application_revision ? shortSha(model.application_revision) : "历史未记录"}</p>
          <p>知识引用：{Object.entries(model.knowledge_versions ?? {}).map(([name,version]) => name + "@" + version).join("、") || (model.context_recorded ? "本次没有引用" : "历史未记录")}</p></article>)}
        <DetailDialog><summary>仓库规则与检索索引</summary>
          {detail.source.rule_versions.map(item => <p key={item.source}><code>{item.source}</code> · {item.sha256}</p>)}
          {detail.source.retrieval.map((item,index) => <p key={item.index_id + ":" + index}>{item.agent} · {item.strategy || "策略未记录"} · 索引 {item.index_id.slice(0,12)} · 提交 {shortSha(item.index_head_sha)} · {item.embedding_model}</p>)}
        </DetailDialog><small>快照校验值：{detail.observation.snapshot_sha256}</small>
      </div>}
      {editable && <div className="evaluation-actions"><Button variant="default" type="button" className="evaluation-primary" disabled={busy} onClick={() => void action(() => api.submitEvaluationReview(sample.id,variant,detail.observation.revision), true)}>{busy ? "正在保存…" : reviewMode === "dual" ? "提交我的独立复核并查看统计" : "完成核对并查看统计"}</Button>
        <span className="evaluation-hint">{reviewMode === "dual" ? "提交前必须核对全部问题；无法确认时可选择暂不确定，它们不会算作有效问题。" : "可以保留暂不确定或未核对的问题；它们不会算作有效问题。"}</span></div>}
    </>}
      {detail && replacing && editable && <div className="evaluation-replace"><p className="ws-note">更换运行会清空该组已有复核，参考标签保留。</p>
        <EvaluationSourcePicker datasetId={sample.dataset_id} caseId={sample.id} single selected={replacement} onSelected={setReplacement} onError={onError} disabled={busy}
          excludeRunId={sample[variant === "baseline" ? "candidate" : "baseline"]?.source_run_id} />
        <label className="evaluation-check"><input type="checkbox" checked={reset} onChange={event => setReset(event.target.checked)} />确认重置该组已有复核</label>
        <div className="ws-form-actions"><Button variant="default" className="ws-primary" type="button" disabled={busy || replacement.length !== 1} onClick={() => void replace()}>保存替换</Button><Button variant="outline" type="button" disabled={busy} onClick={() => setReplacing(false)}>取消</Button></div></div>}
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
  const [failureReason, setFailureReason] = useState<keyof typeof failureReasonLabels | "">(mine?.failure_reason ?? "");
  useEffect(() => {
    setVerdict(mine?.verdict ?? ""); setLocation(mine?.location_correct == null ? "" : String(mine.location_correct));
    setReference(mine?.reference_key ?? ""); setNote(mine?.note ?? ""); setFailureReason(mine?.failure_reason ?? "");
  }, [mine?.verdict,mine?.location_correct,mine?.reference_key,mine?.note,mine?.failure_reason]);
  const submit=(event: FormEvent) => {event.preventDefault(); if (!verdict) return; onSave({verdict,
    location_correct:location === "" ? null : location === "true", reference_key:verdict === "valid" && reference ? reference : null, note, failure_reason: failureReason || null});};
  return <article className="evaluation-finding">
    <div className="evaluation-toolbar"><h4>{item.finding.title}</h4><WorkspaceBadge tone={item.finding.severity === "critical" || item.finding.severity === "high" ? "danger" : "warning"}>{({ critical: "严重", high: "高风险", medium: "中风险", low: "低风险" } as Record<string,string>)[item.finding.severity] ?? item.finding.severity} · {categories[item.finding.category]}</WorkspaceBadge></div>
    <p><code>{item.finding.file ?? "未定位"}{item.finding.start_line ? ":" + item.finding.start_line : ""}</code></p>
    <DetailDialog><summary>证据与影响</summary><pre>{item.finding.evidence}</pre><p>{item.finding.impact}</p><p>{item.finding.suggestion}</p></DetailDialog>
    <div className="evaluation-reviewers">{item.reviews.map(review => <span key={review.reviewer}>{review.reviewer}：{verdicts[review.decision.verdict]}{review.submitted_at ? "（已提交）" : "（草稿）"}{review.decision.note ? " · " + review.decision.note : ""}</span>)}</div>
    <form onSubmit={submit}><fieldset disabled={disabled}>
      <div className="evaluation-verdict-actions" role="group" aria-label="判断这条问题">
        {(["valid", "false_positive", "uncertain"] as const).map(value => <Button variant="outline" type="button" key={value} className={"verdict-" + value} aria-pressed={mine?.verdict === value}
          onClick={() => {setVerdict(value); onSave({verdict:value, location_correct:location === "" ? null : location === "true", reference_key:value === "valid" && reference ? reference : null, note, failure_reason: failureReason || null});}}>{verdicts[value]}</Button>)}
      </div>
      <p className="evaluation-hint">点击即保存。当前判断：{mine ? verdicts[mine.verdict] : "尚未核对"}</p>
      <DetailDialog><summary>补充说明、位置与已知缺陷（可选）</summary><div className="evaluation-form-grid">
        <label>我的结论<NativeSelect value={verdict} onChange={event => setVerdict(event.target.value as typeof verdict)}><option value="">请选择结论</option>{Object.entries(verdicts).map(([key,label]) => <option key={key} value={key}>{label}</option>)}</NativeSelect></label>
        <label>定位是否正确<NativeSelect value={location} onChange={event => setLocation(event.target.value)}><option value="">未判断</option><option value="true">正确</option><option value="false">错误</option></NativeSelect></label>
        {references.length > 0 && <label>对应已知缺陷<NativeSelect value={reference} disabled={verdict !== "valid"} onChange={event => setReference(event.target.value)}><option value="">没有对应 / 新发现的问题</option>{references.map(item => <option key={item.key} value={item.key}>{item.title}</option>)}</NativeSelect></label>}
        <label>核对说明<Input maxLength={1000} value={note} onChange={event => setNote(event.target.value)} /></label>
        <label>失败归因（可选）<NativeSelect value={failureReason} onChange={event => setFailureReason(event.target.value as typeof failureReason)}><option value="">尚未分析 / 不适用</option>{Object.entries(failureReasonLabels).map(([key,label]) => <option key={key} value={key}>{label}</option>)}</NativeSelect></label>
      </div><Button variant="outline" type="submit" disabled={!verdict}>保存补充判断</Button></DetailDialog>
    </fieldset></form>
  </article>;
}
