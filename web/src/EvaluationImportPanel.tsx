import { useRef, useState, type FormEvent } from "react";
import { api } from "./api";
import EvaluationSourcePicker from "./EvaluationSourcePicker";
import type { EvaluationCaseDetail, EvaluationDataset, EvaluationImport, EvaluationSplit, EvaluationVariant } from "./types";
import { WorkspaceBack, WorkspaceBadge, WorkspaceSection } from "./Workspace";

export default function EvaluationImportPanel({ dataset, sample, initialRunId, onSaved, onCancel, onError }: {
  dataset?: EvaluationDataset; sample?: EvaluationCaseDetail; initialRunId?: string;
  onSaved: (datasetId: string) => void; onCancel: () => void; onError: (error: unknown) => void;
}) {
  const [name, setName] = useState("");
  const [variant, setVariant] = useState<EvaluationVariant>(sample?.baseline ? "candidate" : "baseline");
  const [split, setSplit] = useState<EvaluationSplit>(sample?.split ?? "tuning");
  const [kind, setKind] = useState<EvaluationImport["kind"]>(sample?.kind ?? "normal");
  const [selected, setSelected] = useState<string[]>(initialRunId ? [initialRunId] : []);
  const [busy, setBusy] = useState(false);
  const requestKey = useRef(crypto.randomUUID());
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!selected.length) { onError(new Error("请至少选择一条审查运行")); return; }
    setBusy(true);
    try {
      const body = {review_run_ids:selected, variant, split, kind};
      if (dataset) {
        await api.importEvaluationObservations(dataset.id, body);
        onSaved(dataset.id);
      } else {
        const created = await api.createEvaluationDataset({...body, name:name.trim()}, requestKey.current);
        onSaved(created.id);
      }
    } catch (error) { onError(error); } finally { setBusy(false); }
  }
  const opposite = sample?.[variant === "baseline" ? "candidate" : "baseline"]?.source_run_id;
  return <><WorkspaceBack onClick={() => { if (!busy) onCancel(); }}>{dataset ? "返回当前评测" : "返回评测列表"}</WorkspaceBack><section className="evaluation-card evaluation-editor" aria-label="收录评测样本">
    <div className="ws-editor-heading"><div><h2>{dataset ? "收录到 " + dataset.name : "创建评测集并收录样本"}</h2><p>先设置评测分组，再选择要收录的审查运行。</p></div><WorkspaceBadge tone="accent">已选 {selected.length} 条</WorkspaceBadge></div>
    <form onSubmit={submit}>
      <fieldset disabled={busy}>
        <WorkspaceSection title="评测设置" description="基线与候选用于对照，调参与验收样本分别保留。"><div className="evaluation-form-grid">
          {!dataset && <label>评测集名称<input required maxLength={120} value={name} placeholder="例如：权限与数据库审查评测" onChange={event => setName(event.target.value)} /></label>}
          <label>分组<select value={variant} onChange={event => setVariant(event.target.value as EvaluationVariant)}>
            <option value="baseline">基线</option><option value="candidate">候选</option></select></label>
          <label>样本划分<select disabled={Boolean(sample)} value={split} onChange={event => setSplit(event.target.value as EvaluationSplit)}>
            <option value="tuning">调参集</option><option value="validation">验收集</option></select><small>同一 PR 的划分固定，防止调参数据混入验收。</small></label>
          <label>样本类型<select disabled={Boolean(sample)} value={kind} onChange={event => setKind(event.target.value as EvaluationImport["kind"])}>
            <option value="normal">正常变更</option><option value="known_defect">已知缺陷</option><option value="cross_file">跨文件问题</option></select></label>
        </div></WorkspaceSection>
        {initialRunId && <p className="evaluation-hint">已预选来自任务详情的运行，可在下方调整。</p>}
        <div className="evaluation-source-step"><EvaluationSourcePicker datasetId={dataset?.id} caseId={sample?.id} selected={selected} onSelected={setSelected}
          onError={onError} disabled={busy} excludeRunId={opposite} single={Boolean(sample)} /></div>
        <div className="ws-form-actions is-sticky"><button className="evaluation-primary" type="submit" disabled={!selected.length}>{busy ? "正在收录…" : "保存并收录"}</button>
          <button type="button" onClick={onCancel}>取消</button></div>
      </fieldset>
    </form>
  </section></>;
}
