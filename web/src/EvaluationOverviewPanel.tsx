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
    <h3>评测结果 · 单人核对</h3>
    <p>先核对单次审查是否有用；只有比较同一提交的两次独立审查，才需要“方案对比”。</p>
    <div className="evaluation-stats">
      <div><strong>{overview?.case_count ?? "—"}</strong><span>真实 PR 样本</span></div>
      <div><strong>{overview ? `${overview.reviewed_observations}/${overview.observation_count}` : "—"}</strong><span>已完成复核的审查</span></div>
      <div><strong>{overview?.valid_findings ?? "—"}</strong><span>复核认为有效的问题</span></div>
      <div><strong>{overview?.false_positive_findings ?? "—"}</strong><span>复核认为是误报</span></div>
    </div>
    <div className="evaluation-stats">
      <div><strong>{overview?.unreviewed_findings ?? "—"}</strong><span>未核对</span></div>
      <div><strong>{overview?.uncertain_findings ?? "—"}</strong><span>暂不确定</span></div>
      <div><strong>{overview ? (overview.model_duration_ms / 1000).toFixed(1) + " 秒" : "—"}</strong><span>AI 总耗时</span></div>
      <div><strong>{overview?.estimated_cost_microusd == null ? "未记录" : "$" + (overview.estimated_cost_microusd / 1e6).toFixed(4)}</strong><span>已知估算费用（{overview?.unpriced_observations ?? 0} 份缺价格）</span></div>
    </div>
    <p className="ws-note">{overview ? `尚有 ${overview.unreviewed_findings} 条问题尚未核对，${overview.missing_reference_cases} 个 PR 未填写参考缺陷。` : "正在读取复核进度…"} 保存问题结论即可更新统计；缺少费用请在模型配置补充价格，历史未知费用不会按零计算。</p>
    <small>来源：登录账号单人核对，以最近保存的判断为准，未经过双人独立审核。不确定和未核对不算有效；没有可靠的漏报参考时不计算召回率。</small>
  </section>;
}
