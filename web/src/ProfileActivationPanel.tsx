import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "./api";
import { platformApi } from "./platform-api";
import type { ProfileQuality } from "./types";
import Pagination from "./Pagination";
import { useCursorPage } from "./useCursorPage";

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
  return <section className="team-card" aria-label="方案启用质量确认"><h3>查看质量后启用方案</h3>
    <p>仅比较同一 PR、同一提交且分别绑定两个方案的验收结果。达到样本要求也不代表普遍准确率。</p>
    <label>验收评测集<select disabled={busy} value={dataset} onChange={event => setDataset(event.target.value)}><option value="">暂无可用评测，记录理由后人工启用</option>{datasets.data?.items.filter(item => item.repository.toLowerCase() === repository.toLowerCase()).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
    <Pagination label="评测集选择分页" page={datasets.page} count={datasets.data?.items.length ?? 0} hasNext={Boolean(datasets.data?.next_cursor)} busy={datasets.loading} onPrevious={datasets.previous} onNext={datasets.next} />
    {quality ? <><p role="status">质量状态：{({ unverified: "未验证", regression: "较基线退步", reviewed: "已完成样本复核" })[quality.status]}</p>
      {quality.reasons.map(message => <p key={message} className="team-hint">{message}</p>)}
      {quality.report && <div className="team-table-wrap"><table><thead><tr><th>指标</th><th>当前方案</th><th>候选方案</th></tr></thead><tbody>
        <tr><td>有效问题比例</td><td>{percent(quality.report.baseline.precision)}</td><td>{percent(quality.report.candidate.precision)}</td></tr>
        <tr><td>已知缺陷找回率</td><td>{percent(quality.report.baseline.recall)}</td><td>{percent(quality.report.candidate.recall)}</td></tr>
        <tr><td>平均估算费用</td><td>{cost(quality.report.baseline.mean_estimated_cost_usd)}</td><td>{cost(quality.report.candidate.mean_estimated_cost_usd)}</td></tr>
      </tbody></table><p>完整参考配对 {quality.report.reference_pairs} / {quality.report.case_count} · <a href="#evaluations">查看完整评测及置信区间</a></p></div>}
    </> : <p role="status">正在读取质量依据…</p>}
    <form onSubmit={event => void submit(event)}><fieldset disabled={busy || !quality}>
      <label>人工启用理由<textarea required={quality?.status !== "reviewed"} value={reason} maxLength={1000} rows={3} onChange={event => setReason(event.target.value)} placeholder="例如：先在受控仓库试用，完成复核后再扩大使用范围" /></label>
      <button type="submit">确认启用方案</button><button type="button" onClick={() => setRefresh(value => value + 1)}>重新读取质量依据</button><button type="button" onClick={onCancel}>取消</button>
    </fieldset></form>
  </section>;
}
