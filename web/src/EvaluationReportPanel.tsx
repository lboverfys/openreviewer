import { NativeSelect } from "./components/ui/native-select";
import { Button } from "./components/ui/button";
import { Table, TableHeader, TableRow, TableHead, TableBody, TableCell } from "./components/ui/table";
import { DetailDialog } from "./Feedback";
import { failureReasonLabels } from "./evaluation-labels";
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
  return <section className="workspace-surface evaluation-card evaluation-report-card" aria-label="两份审查效果对比">
    <div className="evaluation-toolbar"><div><h2>两份审查效果对比</h2><p>比较同一提交的质量、耗时与估算成本。</p></div><div className="ws-actions">
      <label>报告样本集<NativeSelect value={split} onChange={event => setSplit(event.target.value as EvaluationSplit)}>
        <option value="validation">验收集</option><option value="tuning">调参集</option></NativeSelect></label>
      <Button variant="outline" type="button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>重新计算</Button>
      <Button variant="outline" type="button" disabled={!report} onClick={exportReport}>导出报告 JSON</Button>
      <Button variant="default" className="ws-primary" type="button" disabled={!report || split !== "validation"} onClick={() => void exportEvidence()}>导出验收记录</Button>
    </div></div>
    {loading && <p role="status">正在聚合评测结果…</p>}
    {report && <>
      <p className="evaluation-hint">{report.review_mode === "dual" ? "双人验收 · 严格排除来源不完整和处置分歧的配对" : "单人核对 · 不构成方案验收依据"} · 参考分歧 {report.reference_disputed_pairs ?? 0} 对 · 定位分歧 {report.location_disagreements ?? 0} 条</p>
      <div className="evaluation-stats">
        <div><strong>{report.case_count}</strong><span>PR 样本</span></div>
        <div><strong>{report.performance_pairs}</strong><span>同提交的两份审查</span></div>
        <div><strong>{report.quality_pairs}</strong><span>两份均已核对</span></div>
        <div><strong>{report.reference_pairs}</strong><span>参考标签已确认</span></div>
      </div>
      <div className="ws-toolbar"><WorkspaceBadge tone={report.quality_pairs ? "accent" : "warning"}>{report.quality_pairs ? "可以比较方案" : report.missing_candidate > 0 && !report.performance_pairs ? "尚未添加第二份审查" : "请完成两份审查的核对"}</WorkspaceBadge><span className="evaluation-hint">待复核 {report.pending_pairs} 对 · 分歧 {report.disputed_pairs} 对</span></div>
      {!report.performance_pairs && <p className="ws-note">当前已有的单组复核不会丢失，请在“核对问题”查看。这里仅比较同一提交的两条独立运行，没有第二份审查时不计算对比指标。</p>}
      <DetailDialog className="ws-disclosure evaluation-report-notes"><summary>数据完整性与统计口径</summary><div className="ws-disclosure-body">{report.notices.map(notice => <p key={notice} className="evaluation-notice">{notice}</p>)}<p className="evaluation-hint">缺第一份 {report.missing_baseline} · 缺第二份 {report.missing_candidate} · 待完成复核 {report.pending_pairs} 对 · 有分歧 {report.disputed_pairs} 对</p></div></DetailDialog>
      <div className="evaluation-table-wrap"><Table>
        <TableHeader><TableRow><TableHead>指标</TableHead><TableHead>第一份审查</TableHead><TableHead>第二份审查</TableHead><TableHead>统计范围</TableHead></TableRow></TableHeader>
        <TableBody>
          <TableRow><TableCell>有效问题比例</TableCell><TableCell>{evaluationPercent(report.baseline.precision)}</TableCell><TableCell>{evaluationPercent(report.candidate.precision)}</TableCell><TableCell>{report.quality_pairs} 对，两份均完成核对</TableCell></TableRow>
          <TableRow><TableCell>有效问题 / 已复核问题</TableCell><TableCell>{report.baseline.valid_count} / {report.baseline.finding_count}</TableCell><TableCell>{report.candidate.valid_count} / {report.candidate.finding_count}</TableCell><TableCell>按问题计数</TableCell></TableRow>
          <TableRow><TableCell>已知缺陷找回率</TableCell><TableCell>{evaluationPercent(report.baseline.recall)}</TableCell><TableCell>{evaluationPercent(report.candidate.recall)}</TableCell><TableCell>{report.reference_pairs} 对，参考标签由核对人确认</TableCell></TableRow>
          <TableRow><TableCell>找回 / 已知缺陷</TableCell><TableCell>{report.baseline.reference_true_positive_count} / {report.baseline.reference_expected_count}</TableCell><TableCell>{report.candidate.reference_true_positive_count} / {report.candidate.reference_expected_count}</TableCell><TableCell>同一缺陷多次匹配只计一次</TableCell></TableRow>
          <TableRow><TableCell>定位正确比例</TableCell><TableCell>{evaluationPercent(report.baseline.location_accuracy)}</TableCell><TableCell>{evaluationPercent(report.candidate.location_accuracy)}</TableCell><TableCell>核对人明确判断过位置的问题</TableCell></TableRow>
          <TableRow><TableCell>重复问题比例</TableCell><TableCell>{evaluationPercent(report.baseline.duplicate_rate)}</TableCell><TableCell>{evaluationPercent(report.candidate.duplicate_rate)}</TableCell><TableCell>两份均完成核对的配对样本</TableCell></TableRow>
          <TableRow><TableCell>平均任务到结果耗时</TableCell><TableCell>{formatDuration(report.baseline.mean_turnaround_ms)}</TableCell><TableCell>{formatDuration(report.candidate.mean_turnaround_ms)}</TableCell><TableCell>{report.performance_pairs} 对，含 CI 与排队</TableCell></TableRow>
          <TableRow><TableCell>平均累计模型请求耗时</TableCell><TableCell>{formatDuration(report.baseline.mean_model_duration_ms)}</TableCell><TableCell>{formatDuration(report.candidate.mean_model_duration_ms)}</TableCell><TableCell>并行请求也分别计时</TableCell></TableRow>
          <TableRow><TableCell>平均估算费用</TableCell><TableCell>{money(report.baseline.mean_estimated_cost_usd)}</TableCell><TableCell>{money(report.candidate.mean_estimated_cost_usd)}</TableCell><TableCell>{report.priced_pairs} 对，两份费用均已记录</TableCell></TableRow>
          <TableRow><TableCell>输入 / 输出 Token 合计</TableCell><TableCell>{report.baseline.input_tokens} / {report.baseline.output_tokens}</TableCell><TableCell>{report.candidate.input_tokens} / {report.candidate.output_tokens}</TableCell><TableCell>{report.performance_pairs} 对，输入含缓存</TableCell></TableRow>
          <TableRow><TableCell>干净 PR 误报率</TableCell><TableCell>{evaluationPercent(report.baseline.clean_pr_false_alarm_rate ?? null)}</TableCell><TableCell>{evaluationPercent(report.candidate.clean_pr_false_alarm_rate ?? null)}</TableCell><TableCell>{report.baseline.clean_pr_count ?? 0} 对，已确认没有已知缺陷的正常变更</TableCell></TableRow>
          <TableRow><TableCell>每个确认缺陷的估算成本</TableCell><TableCell>{money(report.baseline.cost_per_confirmed_defect_usd ?? null)}</TableCell><TableCell>{money(report.candidate.cost_per_confirmed_defect_usd ?? null)}</TableCell><TableCell>{report.baseline.priced_reference_pairs ?? 0} 对，双方已知费用且已确认参考标签</TableCell></TableRow>
          {Object.entries(failureReasonLabels).map(([reason,label]) => <TableRow key={reason}><TableCell>失败归因 · {label}</TableCell><TableCell>{report.baseline.failure_reasons?.[reason] ?? 0}</TableCell><TableCell>{report.candidate.failure_reasons?.[reason] ?? 0}</TableCell><TableCell>人工分析；双人模式仅统计一致归因</TableCell></TableRow>)}
        </TableBody>
      </Table></div>
      <div className="evaluation-form-grid">
        {(["baseline", "candidate"] as const).map(variant => <div key={variant} className="evaluation-confidence">
          <strong>{variant === "baseline" ? "第一份审查" : "第二份审查"} · 95% 区间</strong>
          <p>有效问题比例：{report[variant].precision_ci95 ? evaluationPercent(report[variant].precision_ci95.lower) + " ～ " + evaluationPercent(report[variant].precision_ci95.upper) : "样本不足，暂不计算"}</p>
          <p>已知缺陷找回率：{report[variant].recall_ci95 ? evaluationPercent(report[variant].recall_ci95.lower) + " ～ " + evaluationPercent(report[variant].recall_ci95.upper) : "样本不足，暂不计算"}</p>
        </div>)}
      </div>
      <p className="evaluation-hint">样本类型：正常变更 {report.normal_count} · 已知缺陷 {report.known_defect_count} · 跨文件问题 {report.cross_file_count}。报告衡量当前样本上的审查工作流效果。</p>
    </>}
  </section>;
}
