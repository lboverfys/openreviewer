import { useEffect, useState } from "react";
import { api } from "./api";
import type { EvaluationOverview } from "./types";

export default function EvaluationOverviewPanel({datasetId, version, onError}: {
  datasetId: string; version: number; onError: (error: unknown) => void;
}) {
  const [overview, setOverview] = useState<EvaluationOverview | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    api.evaluationOverview(datasetId, controller.signal).then(result => {
      if (!controller.signal.aborted) setOverview(result);
    }).catch(error => {if (!controller.signal.aborted) onError(error);});
    return () => controller.abort();
  }, [datasetId, version, onError]);
  return <section className="evaluation-card evaluation-progress-summary" aria-label="已完成的复核">
    <h3>这份评测已经做了什么</h3>
    <p>先核对单次审查是否有用；只有比较同一提交的两次独立审查，才需要“方案对比”。</p>
    <div className="evaluation-stats">
      <div><strong>{overview?.case_count ?? "—"}</strong><span>真实 PR 样本</span></div>
      <div><strong>{overview ? `${overview.reviewed_observations}/${overview.observation_count}` : "—"}</strong><span>已完成复核的审查</span></div>
      <div><strong>{overview?.valid_findings ?? "—"}</strong><span>复核认为有效的问题</span></div>
      <div><strong>{overview?.false_positive_findings ?? "—"}</strong><span>复核认为是误报</span></div>
    </div>
    <p className="ws-note">{overview ? `尚有 ${overview.unreviewed_findings} 条问题所在的审查未完成复核，${overview.missing_reference_cases} 个 PR 未填写参考缺陷。` : "正在读取复核进度…"} 上述按审查记录统计，未配对的结果也会显示。</p>
    <small>有效性由复核人判断。AI 账号的结论属于 AI 复核，不能当作独立人工验证；没有报告问题也需要检查是否漏报。</small>
  </section>;
}
