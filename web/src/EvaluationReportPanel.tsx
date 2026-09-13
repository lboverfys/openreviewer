import { useEffect, useState } from "react";
import { api } from "./api";
import { platformApi } from "./platform-api";
import { formatDuration } from "./review-details";
import type { EvaluationReport, EvaluationSplit } from "./types";
import { WorkspaceBadge } from "./Workspace";

export function evaluationPercent(value: number | null | undefined) {
  return value == null ? "—" : (value * 100).toFixed(1) + "%";
}
const money = (value: number | null | undefined) => value == null ? "未知" : "$" + value.toFixed(6);

export default function EvaluationReportPanel({ datasetId, onError }: { datasetId: string; onError: (error: unknown) => void }) {
  const [split, setSplit] = useState<EvaluationSplit>("validation");
  const [report, setReport] = useState<EvaluationReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setReport(null);
    api.evaluationReport(datasetId, split, controller.signal).then(data => {
      if (!controller.signal.aborted) setReport(data);
    }).catch(error => {if (!controller.signal.aborted) onError(error);})
      .finally(() => {if (!controller.signal.aborted) setLoading(false);});
    return () => controller.abort();
  }, [datasetId, split, refresh, onError]);
  function exportReport() {
    if (!report) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type:"application/json"}));
    const link = document.createElement("a");
    link.href = url; link.download = "evaluation-" + datasetId + "-" + split + ".json";
    link.click(); URL.revokeObjectURL(url);
  }
  async function exportEvidence() {
    try {
      const evidence = await platformApi.projectEvidence(datasetId);
      const url = URL.createObjectURL(new Blob([JSON.stringify(evidence, null, 2)], { type: "application/json" }));
      const link = document.createElement("a"); link.href = url;
      link.download = `openreviewer-evidence-${datasetId}.json`; link.click(); URL.revokeObjectURL(url);
    } catch (error) { onError(error); }
  }
  return <section className="workspace-surface evaluation-card evaluation-report-card" aria-label="配对对比报告">
    <div className="evaluation-toolbar"><div><h2>配对对比报告</h2><p>比较同一提交的质量、耗时与估算成本。</p></div><div className="ws-actions">
      <label>报告样本集<select value={split} onChange={event => setSplit(event.target.value as EvaluationSplit)}>
        <option value="validation">验收集</option><option value="tuning">调参集</option></select></label>
      <button type="button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>重新计算</button>
      <button type="button" disabled={!report} onClick={exportReport}>导出报告 JSON</button>
      <button className="ws-primary" type="button" disabled={!report || split !== "validation"} onClick={() => void exportEvidence()}>导出面试证据</button>
    </div></div>
    {loading && <p role="status">正在聚合评测结果…</p>}
    {report && <>
      <div className="evaluation-stats">
        <div><strong>{report.case_count}</strong><span>PR 样本</span></div>
        <div><strong>{report.performance_pairs}</strong><span>同提交配对</span></div>
        <div><strong>{report.quality_pairs}</strong><span>双方复核完成</span></div>
        <div><strong>{report.reference_pairs}</strong><span>参考标签已确认</span></div>
      </div>
      <div className="ws-toolbar"><WorkspaceBadge tone={report.quality_pairs ? "accent" : "warning"}>{report.quality_pairs ? "已有复核样本" : "待人工复核"}</WorkspaceBadge><span className="evaluation-hint">待复核 {report.pending_pairs} 对 · 分歧 {report.disputed_pairs} 对</span></div>
      <details className="ws-disclosure evaluation-report-notes" open={!report.quality_pairs}><summary>数据完整性与统计口径</summary><div className="ws-disclosure-body">{report.notices.map(notice => <p key={notice} className="evaluation-notice">{notice}</p>)}<p className="evaluation-hint">缺基线 {report.missing_baseline} · 缺候选 {report.missing_candidate} · 待完成复核 {report.pending_pairs} 对 · 有分歧 {report.disputed_pairs} 对</p></div></details>
      <div className="evaluation-table-wrap"><table>
        <thead><tr><th>指标</th><th>基线</th><th>候选</th><th>统计范围</th></tr></thead>
        <tbody>
          <tr><td>有效问题比例</td><td>{evaluationPercent(report.baseline.precision)}</td><td>{evaluationPercent(report.candidate.precision)}</td><td>{report.quality_pairs} 对，双方复核一致</td></tr>
          <tr><td>有效问题 / 已复核问题</td><td>{report.baseline.valid_count} / {report.baseline.finding_count}</td><td>{report.candidate.valid_count} / {report.candidate.finding_count}</td><td>按问题计数</td></tr>
          <tr><td>已知缺陷找回率</td><td>{evaluationPercent(report.baseline.recall)}</td><td>{evaluationPercent(report.candidate.recall)}</td><td>{report.reference_pairs} 对，参考标签双人确认</td></tr>
          <tr><td>找回 / 已知缺陷</td><td>{report.baseline.reference_true_positive_count} / {report.baseline.reference_expected_count}</td><td>{report.candidate.reference_true_positive_count} / {report.candidate.reference_expected_count}</td><td>同一缺陷多次匹配只计一次</td></tr>
          <tr><td>定位准确率</td><td>{evaluationPercent(report.baseline.location_accuracy)}</td><td>{evaluationPercent(report.candidate.location_accuracy)}</td><td>两人均确认定位的已复核问题</td></tr>
          <tr><td>重复问题比例</td><td>{evaluationPercent(report.baseline.duplicate_rate)}</td><td>{evaluationPercent(report.candidate.duplicate_rate)}</td><td>双方复核一致的配对样本</td></tr>
          <tr><td>平均任务到结果耗时</td><td>{formatDuration(report.baseline.mean_turnaround_ms)}</td><td>{formatDuration(report.candidate.mean_turnaround_ms)}</td><td>{report.performance_pairs} 对，含 CI 与排队</td></tr>
          <tr><td>平均累计模型请求耗时</td><td>{formatDuration(report.baseline.mean_model_duration_ms)}</td><td>{formatDuration(report.candidate.mean_model_duration_ms)}</td><td>并行请求也分别计时</td></tr>
          <tr><td>平均估算费用</td><td>{money(report.baseline.mean_estimated_cost_usd)}</td><td>{money(report.candidate.mean_estimated_cost_usd)}</td><td>{report.priced_pairs} 对，双方费用已知</td></tr>
          <tr><td>输入 / 输出 Token 合计</td><td>{report.baseline.input_tokens} / {report.baseline.output_tokens}</td><td>{report.candidate.input_tokens} / {report.candidate.output_tokens}</td><td>{report.performance_pairs} 对，输入含缓存</td></tr>
        </tbody>
      </table></div>
      <div className="evaluation-form-grid">
        {(["baseline", "candidate"] as const).map(variant => <div key={variant} className="evaluation-confidence">
          <strong>{variant === "baseline" ? "基线" : "候选"} · 95% 区间</strong>
          <p>有效问题比例：{report[variant].precision_ci95 ? evaluationPercent(report[variant].precision_ci95.lower) + " ～ " + evaluationPercent(report[variant].precision_ci95.upper) : "样本不足，暂不计算"}</p>
          <p>已知缺陷找回率：{report[variant].recall_ci95 ? evaluationPercent(report[variant].recall_ci95.lower) + " ～ " + evaluationPercent(report[variant].recall_ci95.upper) : "样本不足，暂不计算"}</p>
        </div>)}
      </div>
      <p className="evaluation-hint">样本类型：正常变更 {report.normal_count} · 已知缺陷 {report.known_defect_count} · 跨文件问题 {report.cross_file_count}。报告衡量当前样本上的审查工作流效果。</p>
    </>}
  </section>;
}
