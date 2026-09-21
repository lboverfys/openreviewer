import { Table, TableHeader, TableRow, TableHead, TableBody, TableCell } from "./components/ui/table";
import { formatDuration } from "./review-details";
import { retrievalStrategyLabels } from "./RetrievalTracePanel";
import type { RetrievalEvaluationReport } from "./types";

export type RetrievalReportSplit = "validation" | "development" | "all";

export default function RetrievalMetricsTable({report, split}: {report: RetrievalEvaluationReport; split: RetrievalReportSplit}) {
  const rows = report.strategies.map(item => ({item, score:split === "all" ? item : item.splits?.find(value => value.split === split)}));
  const baseline = rows.find(row => row.item.strategy === "bm25")?.score;
  const percent = (value: number | null | undefined) => value == null ? "—" : (value * 100).toFixed(1) + "%";
  const duration = (value: number | null | undefined) => value == null ? "—" : formatDuration(Math.round(value));
  return <div className="retrieval-table-scroll"><Table><TableHeader><TableRow><TableHead>策略</TableHead><TableHead>样本数</TableHead><TableHead>Recall@K</TableHead><TableHead>MRR</TableHead><TableHead>中位耗时</TableHead><TableHead>P95 耗时</TableHead><TableHead>召回率较基线</TableHead><TableHead>全样本请求 / 估算费</TableHead></TableRow></TableHeader><TableBody>
    {rows.map(({item,score}) => <TableRow key={item.strategy}>
      <TableCell>{retrievalStrategyLabels[item.strategy]}</TableCell><TableCell>{score?.sample_count ?? "未记录分层"}</TableCell>
      <TableCell>{percent(score?.recall_at_k)} <small>K={item.k}</small></TableCell><TableCell>{score?.mrr == null ? "—" : score.mrr.toFixed(3)}</TableCell>
      <TableCell>{duration(score?.median_duration_ms)}</TableCell><TableCell>{duration(score?.p95_duration_ms)}</TableCell>
      <TableCell>{score?.recall_at_k != null && baseline?.recall_at_k != null ? `${score.recall_at_k >= baseline.recall_at_k ? "+" : ""}${((score.recall_at_k - baseline.recall_at_k) * 100).toFixed(1)} 个百分点` : "—"}</TableCell>
      <TableCell>{item.model_requests ?? "未记录"} / {item.estimated_cost_microusd == null ? "未知" : "$" + (item.estimated_cost_microusd / 1e6).toFixed(6)}</TableCell>
    </TableRow>)}
  </TableBody></Table></div>;
}
