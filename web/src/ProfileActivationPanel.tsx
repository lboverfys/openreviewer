import { DetailDialog } from "./Feedback";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "./api";
import { platformApi } from "./platform-api";
import type { ProfileQuality } from "./types";
import Pagination from "./Pagination";
import { useCursorPage } from "./useCursorPage";
import { WorkspaceBadge, WorkspaceSection } from "./Workspace";

export default function ProfileActivationPanel({ id, repository, revision, onDone, onCancel, onError }: {
  id: string; repository: string; revision: number; onDone: () => Promise<void>;
  onCancel: () => void; onError: (error: unknown) => void;
}) {
  const loadDatasets = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => api.evaluationDatasets(false, cursor, signal, force), []);
  const datasets = useCursorPage({ cacheKey: "profile-quality-datasets", load: loadDatasets, onError });
  const [dataset, setDataset] = useState("");
  const [quality, setQuality] = useState<ProfileQuality | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setQuality(null);
    void platformApi.profileQuality(id, dataset || undefined, controller.signal).then(setQuality).catch(error => { if (!controller.signal.aborted) onError(error); });
    return () => controller.abort();
  }, [id, dataset, revision, onError, refresh]);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); if (!quality) return; setBusy(true);
    try {
      await platformApi.activateProfile(id, revision, { evaluation_dataset_id: dataset || null, evidence_token: quality.evidence_token, reason });
      await onDone();
    } catch (error) { onError(error); } finally { setBusy(false); }
  };
  const percent = (value: number | null) => value == null ? "待人工复核" : `${(value * 100).toFixed(1)}%`;
  const cost = (value: number | null) => value == null ? "未知" : `$${value.toFixed(6)}`;
  return <section className="workspace-surface ws-editor" aria-label="方案启用质量确认">
    <div className="ws-editor-heading"><div><h2>确认启用审查方案</h2><p>{repository} · 新任务使用此方案，已有任务保持原版本。</p></div><button type="button" disabled={busy} onClick={() => setRefresh(value => value + 1)}>重新读取质量依据</button></div>
    <WorkspaceSection title="验收依据" description="比较同一提交下的基线与候选结果。">
      <label>验收评测集<select disabled={busy} value={dataset} onChange={event => setDataset(event.target.value)}><option value="">暂无可用评测，记录理由后人工启用</option>{datasets.data?.items.filter(item => item.repository.toLowerCase() === repository.toLowerCase()).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <Pagination label="评测集选择分页" page={datasets.page} count={datasets.data?.items.length ?? 0} hasNext={Boolean(datasets.data?.next_cursor)} busy={datasets.loading} onPrevious={datasets.previous} onNext={datasets.next} />
    </WorkspaceSection>
    {quality ? <>
      <div className="profile-quality"><WorkspaceBadge tone={quality.status === "reviewed" ? "success" : quality.status === "regression" ? "danger" : "warning"}>{({ unverified: "未验证", regression: "较基线退步", reviewed: "已复核" })[quality.status]}</WorkspaceBadge><div><strong role="status">质量状态：{({ unverified: "未验证", regression: "较基线退步", reviewed: "已完成样本复核" })[quality.status]}</strong>{quality.reasons.slice(0, 1).map(message => <p key={message}>{message}</p>)}{quality.reasons.length > 1 && <DetailDialog className="profile-version"><summary>查看全部质量提示</summary>{quality.reasons.slice(1).map(message => <p key={message}>{message}</p>)}</DetailDialog>}</div></div>
      {quality.report && <div className="team-table-wrap"><table><thead><tr><th>指标</th><th>当前方案</th><th>候选方案</th></tr></thead><tbody>
        <tr><td>有效问题比例</td><td>{percent(quality.report.baseline.precision)}</td><td>{percent(quality.report.candidate.precision)}</td></tr>
        <tr><td>已知缺陷找回率</td><td>{percent(quality.report.baseline.recall)}</td><td>{percent(quality.report.candidate.recall)}</td></tr>
        <tr><td>平均估算费用</td><td>{cost(quality.report.baseline.mean_estimated_cost_usd)}</td><td>{cost(quality.report.candidate.mean_estimated_cost_usd)}</td></tr>
      </tbody></table></div>}
      {quality.report && <p className="ws-hint">完整参考配对 {quality.report.reference_pairs} / {quality.report.case_count} · <a href={"#evaluations/" + quality.report.dataset_id}>查看完整评测及置信区间 →</a></p>}
    </> : <div className="profile-quality" role="status">正在读取质量依据…</div>}
    <form onSubmit={event => void submit(event)}><fieldset disabled={busy || !quality}>
      <WorkspaceSection title="人工确认" description="未验证或出现退步时，请说明本次启用的依据。"><label>人工启用理由<textarea required={quality?.status !== "reviewed"} value={reason} maxLength={1000} rows={4} onChange={event => setReason(event.target.value)} placeholder="例如：先在指定仓库试用，完成人工复核后再扩大范围" /></label></WorkspaceSection>
      <div className="ws-form-actions"><button className="ws-primary" type="submit">确认启用方案</button><button type="button" onClick={onCancel}>取消</button><span className="ws-hint">启用时再次核对评测与策略版本</span></div>
    </fieldset></form>
  </section>;
}
