import { useEffect, useState } from "react";
import { api } from "./api";
import type { EvaluationOverview } from "./types";

export default function EvaluationOverviewPanel({datasetId, version, onError, caseId, variant}: {
  datasetId: string; version: number; onError: (error: unknown) => void; caseId?: string; variant?: import("./types").EvaluationVariant;
}) {
  const [overview, setOverview] = useState<EvaluationOverview | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    api.evaluationOverview(datasetId, controller.signal, caseId, variant).then(result => {
      if (!controller.signal.aborted) setOverview(result);
    }).catch(error => {if (!controller.signal.aborted) onError(error);});
    return () => controller.abort();
  }, [datasetId, version, onError, caseId, variant]);
  return <section className="evaluation-card evaluation-progress-summary" aria-label="已完成的复核">
    <h3>评测结果 · {overview?.review_mode === "dual" ? "双人验收" : "单人核对"}</h3>
    <p>{caseId ? "以下只统计当前选择的这份审查。" : "以下统计这份评测记录中的全部审查。"}{overview?.review_mode === "dual" ? "有效和误报来自两人提交后的一致判断。" : "有效和误报来自你保存的判断。"}</p>
    <div className="evaluation-stats">
      <div><strong>{overview?.case_count ?? "—"}</strong><span>真实 PR 样本</span></div>
      <div><strong>{overview ? `${overview.reviewed_observations}/${overview.observation_count}` : "—"}</strong><span>已完成复核的审查</span></div>
      <div><strong>{overview?.valid_findings ?? "—"}</strong><span>复核认为有效的问题</span></div>
      <div><strong>{overview?.false_positive_findings ?? "—"}</strong><span>复核认为是误报</span></div>
    </div>
    {overview?.review_mode === "dual" && <p>处置分歧 {overview.disputed_findings} 条 · 参考匹配分歧 {overview.reference_disagreements} 条 · 定位分歧 {overview.location_disagreements} 条</p>}
    <div className="evaluation-stats">
      <div><strong>{overview?.unreviewed_findings ?? "—"}</strong><span>未核对</span></div>
      <div><strong>{overview?.uncertain_findings ?? "—"}</strong><span>暂不确定</span></div>
      <div><strong>{overview ? (overview.model_duration_ms / 1000).toFixed(1) + " 秒" : "—"}</strong><span>AI 总耗时</span></div>
      <div><strong>{overview?.estimated_cost_microusd == null ? "未记录" : "$" + (overview.estimated_cost_microusd / 1e6).toFixed(4)}</strong><span>已知估算费用（{overview?.unpriced_observations ?? 0} 份缺价格）</span></div>
    </div>
    <p className="ws-note">{overview ? overview.unreviewed_findings ? `还有 ${overview.unreviewed_findings} 条未核对，可返回继续判断。` : "当前问题已全部标记。" : "正在读取核对进度…"} 未配置价格的历史费用保留为未知。</p>
    <small>来源：{overview?.review_source ?? "正在读取核对方式"}。不确定和未核对不算有效；没有可靠的漏报参考时不计算召回率。</small>
  </section>;
}
