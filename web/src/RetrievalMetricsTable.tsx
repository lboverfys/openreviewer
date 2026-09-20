import { formatDuration } from "./review-details";
import { retrievalStrategyLabels } from "./RetrievalTracePanel";
import type { RetrievalEvaluationReport } from "./types";

export type RetrievalReportSplit = "validation" | "development" | "all";

export default function RetrievalMetricsTable({report, split}: {report: RetrievalEvaluationReport; split: RetrievalReportSplit}) {
  const rows = report.strategies.map(item => ({item, score:split === "all" ? item : item.splits?.find(value => value.split === split)}));
  const baseline = rows.find(row => row.item.strategy === "bm25")?.score;
  const percent = (value: number | null | undefined) => value == null ? "—" : (value * 100).toFixed(1) + "%";
  const duration = (value: number | null | undefined) => value == null ? "—" : formatDuration(Math.round(value));
  return <div className="retrieval-table-scroll"><table><thead><tr><th>策略</th><th>样本数</th><th>Recall@K</th><th>MRR</th><th>中位耗时</th><th>P95 耗时</th><th>召回率较基线</th><th>全样本请求 / 估算费</th></tr></thead><tbody>
    {rows.map(({item,score}) => <tr key={item.strategy}>
      <td>{retrievalStrategyLabels[item.strategy]}</td><td>{score?.sample_count ?? "未记录分层"}</td>
      <td>{percent(score?.recall_at_k)} <small>K={item.k}</small></td><td>{score?.mrr == null ? "—" : score.mrr.toFixed(3)}</td>
      <td>{duration(score?.median_duration_ms)}</td><td>{duration(score?.p95_duration_ms)}</td>
      <td>{score?.recall_at_k != null && baseline?.recall_at_k != null ? `${score.recall_at_k >= baseline.recall_at_k ? "+" : ""}${((score.recall_at_k - baseline.recall_at_k) * 100).toFixed(1)} 个百分点` : "—"}</td>
      <td>{item.model_requests ?? "未记录"} / {item.estimated_cost_microusd == null ? "未知" : "$" + (item.estimated_cost_microusd / 1e6).toFixed(6)}</td>
    </tr>)}
  </tbody></table></div>;
}
