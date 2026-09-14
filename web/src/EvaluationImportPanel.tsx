import { DetailDialog } from "./Feedback";
import { useRef, useState, type FormEvent } from "react";
import { api } from "./api";
import EvaluationSourcePicker from "./EvaluationSourcePicker";
import type { EvaluationCaseDetail, EvaluationDataset, EvaluationImport, EvaluationSplit, EvaluationVariant } from "./types";
import { WorkspaceBadge, WorkspaceSection } from "./Workspace";

export default function EvaluationImportPanel({ dataset, sample, initialRunId, onSaved, onCancel, onError }: {
  dataset?: EvaluationDataset; sample?: EvaluationCaseDetail; initialRunId?: string;
  onSaved: (datasetId: string) => void; onCancel: () => void; onError: (error: unknown) => void;
}) {
  const [name, setName] = useState("");
  const [variant] = useState<EvaluationVariant>(sample?.baseline ? "candidate" : "baseline");
  const [split, setSplit] = useState<EvaluationSplit>(sample?.split ?? "validation");
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
        const created = await api.createEvaluationDataset({...body, name:name.trim() || "审查核对 · " + new Date().toLocaleDateString("zh-CN")}, requestKey.current);
        onSaved(created.id);
      }
    } catch (error) { onError(error); } finally { setBusy(false); }
  }
  const opposite = sample?.[variant === "baseline" ? "candidate" : "baseline"]?.source_run_id;
  return <section className="evaluation-card evaluation-editor" aria-label="选择要核对的审查">
    <div className="ws-editor-heading"><div><h2>{dataset ? "添加到 " + dataset.name : "开始一次效果评测"}</h2><p>选择一份已完成的审查，下一步直接判断问题。</p></div><div className="ws-actions"><WorkspaceBadge tone="accent">已选 {selected.length} 条</WorkspaceBadge><button type="button" disabled={busy} onClick={onCancel}>取消添加</button></div></div>
    <form onSubmit={submit}>
      <fieldset disabled={busy}>
        <WorkspaceSection title="选择审查结果" description="一份结果即可开始核对；第二份结果仅用于可选比较。"><div className="evaluation-form-grid">
          {!dataset && <label>评测名称（可不填）<input maxLength={120} value={name} placeholder="例如：NiuMa 审查质量核对" onChange={event => setName(event.target.value)} /></label>}

        </div></WorkspaceSection>
        <DetailDialog className="ws-disclosure"><summary>样本分类（可选）</summary><div className="ws-disclosure-body evaluation-form-grid">
          <label>样本用途<select disabled={Boolean(sample)} value={split} onChange={event => setSplit(event.target.value as EvaluationSplit)}>
            <option value="validation">正式验收</option><option value="tuning">调试配置</option></select><small>默认用于验收。调试过配置的样本应单独保留，避免评测结果失真。</small></label>
          <label>变更类型<select disabled={Boolean(sample)} value={kind} onChange={event => setKind(event.target.value as EvaluationImport["kind"])}>
            <option value="normal">普通变更</option><option value="known_defect">包含已知缺陷</option><option value="cross_file">涉及跨文件问题</option></select></label>
        </div></DetailDialog>
        {initialRunId && <p className="evaluation-hint">已预选来自任务详情的运行，可在下方调整。</p>}
        <div className="evaluation-source-step"><EvaluationSourcePicker datasetId={dataset?.id} caseId={sample?.id} selected={selected} onSelected={setSelected}
          onError={onError} disabled={busy} excludeRunId={opposite} single /></div>
        <div className="ws-form-actions is-sticky"><button className="evaluation-primary" type="submit" disabled={!selected.length}>{busy ? "正在准备…" : sample ? "添加这份审查进行比较" : "开始核对问题"}</button>
          <button type="button" onClick={onCancel}>取消</button></div>
      </fieldset>
    </form>
  </section>;
}
