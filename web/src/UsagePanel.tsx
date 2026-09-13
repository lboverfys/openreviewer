import { useCallback, useEffect, useState } from "react";
import Pagination from "./Pagination";
import { platformApi } from "./platform-api";
import type { PlatformPanelProps } from "./PlatformPage";
import type { UsageBreakdown, UsageMonth } from "./types";
import { useCursorPage } from "./useCursorPage";
import { formatDate } from "./utils";
import { WorkspaceBack, WorkspaceBadge, WorkspaceEmpty } from "./Workspace";

export const formatMoney = (value: number | null | undefined) => value == null ? "未记录" : `$${(value / 1_000_000).toFixed(6)}`;
const purposeLabels: Record<string, string> = { review: "代码审查", embedding: "向量检索", rerank: "精排" };
const agentLabels: Record<string, string> = { security: "安全", convention: "规范", logic: "逻辑", summary: "汇总", retrieval: "检索" };

export default function UsagePanel({ onError }: PlatformPanelProps) {
  const [month, setMonth] = useState(new Date().toISOString().slice(0, 7));
  const [selected, setSelected] = useState<UsageMonth | null>(null);
  const load = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.usage(month, cursor, signal, force), [month]);
  const page = useCursorPage({ cacheKey: `usage:${month}`, load, onError });
  if (selected) return <><WorkspaceBack onClick={() => setSelected(null)}>返回月度用量</WorkspaceBack><UsageDetails key={selected.id} month={page.data?.items.find(item => item.id === selected.id) ?? selected} onError={onError} onRefreshSummary={() => void page.refresh()} /></>;
  return <section className="team-card">
    <div className="team-toolbar"><div><h2>仓库月度用量</h2><p>查看审查费用、预占金额与预算状态。</p></div><div className="ws-actions"><a className="ws-button-link" href="#team">预算设置</a><a className="ws-button-link" href="#settings">模型价格</a></div></div>
    <div className="ws-filterbar"><label>月份（UTC）<input aria-label="用量月份" type="month" value={month} onChange={event => setMonth(event.target.value)} /></label><div className="ws-actions"><button disabled={page.loading} onClick={() => void page.refresh()}>刷新</button></div></div>
    <div className="team-table-wrap"><table><thead><tr><th>仓库</th><th>请求</th><th>已估算费用</th><th>待确认预占</th><th>月度预算</th><th>用量状态</th><th>操作</th></tr></thead>
      <tbody>{page.data?.items.map(item => <tr key={item.id}><td><strong>{item.repository}</strong><small>安装 {item.installation_id}</small></td><td className="ws-numeric">{item.request_count}</td><td className="ws-numeric">{formatMoney(item.estimated_cost_microusd)}</td><td className="ws-numeric">{formatMoney(item.reserved_cost_microusd)}</td><td className="ws-numeric">{item.budget_microusd == null ? "未限制" : formatMoney(item.budget_microusd)}</td><td><WorkspaceBadge tone={item.warning ? "warning" : "success"}>{item.warning ? "已达到预算提醒线" : "正常"}</WorkspaceBadge><small>费用未知 {item.unknown_count} · 请求不确定 {item.uncertain_count}</small></td><td><button className="ws-link-button" onClick={() => setSelected(item)}>查看请求</button></td></tr>)}</tbody>
    </table></div>
    {!page.loading && !page.data?.items.length && <WorkspaceEmpty title="该月份暂无用量记录" description="新审查流程的模型请求会记入账本，历史费用不会补记为零。" />}
    <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={page.loading} onPrevious={page.previous} onNext={page.next} />
    <p className="ws-hint">费用按配置价格估算，包含审查中的向量与精排。未知费用与待确认预占单独保留。</p>
  </section>;
}

function UsageDetails({ month, onError, onRefreshSummary }: PlatformPanelProps & { month: UsageMonth; onRefreshSummary: () => void }) {
  const [groups, setGroups] = useState<UsageBreakdown[]>([]);
  const [version, setVersion] = useState(0);
  const load = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.requests(month.id, cursor, signal, force), [month.id]);
  const page = useCursorPage({ cacheKey: `usage-requests:${month.id}`, load, onError });
  useEffect(() => { const controller = new AbortController(); void platformApi.breakdown(month.id, controller.signal).then(setGroups).catch(error => { if (!controller.signal.aborted) onError(error); }); return () => controller.abort(); }, [month.id, onError, version]);
  return <>
    <section className="team-card"><div className="team-toolbar"><div><h2>{month.repository} · 请求账本</h2><p>{month.month} · UTC 自然月 · 安装 {month.installation_id}</p></div><button disabled={page.loading} onClick={() => { void page.refresh(); setVersion(value => value + 1); onRefreshSummary(); }}>刷新明细</button></div></section>
    <div className="ws-metrics">
      <div className="ws-metric is-accent"><span>已估算费用</span><strong>{formatMoney(month.estimated_cost_microusd)}</strong><small>按配置价格统计</small></div>
      <div className="ws-metric"><span>待确认预占</span><strong>{formatMoney(month.reserved_cost_microusd)}</strong><small>请求结果确认后结算</small></div>
      <div className="ws-metric"><span>模型请求</span><strong>{month.request_count}</strong><small>包含重试请求</small></div>
      <div className="ws-metric"><span>月度预算</span><strong>{month.budget_microusd == null ? "未限制" : formatMoney(month.budget_microusd)}</strong><small>费用未知 {month.unknown_count} 次</small></div>
    </div>
    <section className="team-card"><div className="team-toolbar"><h2>请求明细</h2><WorkspaceBadge>{month.month}</WorkspaceBadge></div>
      <div className="team-table-wrap"><table><thead><tr><th>时间</th><th>模型 / 用途</th><th>状态</th><th>输入 / 输出 Token</th><th>估算费用</th><th>耗时</th><th>来源</th></tr></thead><tbody>{page.data?.items.map(item => <tr key={item.id}>
        <td>{formatDate(item.created_at)}</td><td><strong>{item.model}</strong><small>{agentLabels[item.agent] ?? item.agent} · {purposeLabels[item.purpose] ?? item.purpose}</small></td><td><WorkspaceBadge tone={item.status === "settled" ? "success" : "warning"}>{({ reserved: "待确认", settled: "已记录", uncertain: "结果不确定" } as Record<string, string>)[item.status] ?? item.status}</WorkspaceBadge></td><td className="ws-numeric">{item.input_tokens ?? "未知"} / {item.output_tokens ?? "未知"}</td><td className="ws-numeric">{formatMoney(item.estimated_cost_microusd)}</td><td className="ws-numeric">{item.duration_ms == null ? "未知" : `${(item.duration_ms / 1000).toFixed(1)} 秒`}</td><td><a href={`#review/${encodeURIComponent(item.review_run_id)}`}>审查任务 →</a></td>
      </tr>)}</tbody></table></div>
      {!page.loading && !page.data?.items.length && <WorkspaceEmpty title="暂无请求明细" />}
      <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={page.loading} onPrevious={page.previous} onNext={page.next} />
      <details className="usage-groups"><summary>按模型与用途汇总<span className="ws-chip">{groups.length} 组</span></summary><div className="platform-metrics">{groups.map(item => <div key={`${item.purpose}:${item.model}`}><strong>{item.model}</strong><span>{purposeLabels[item.purpose] ?? item.purpose} · {item.request_count} 次</span><span>{formatMoney(item.estimated_cost_microusd)} · {item.unknown_count} 次费用未知</span></div>)}</div></details>
    </section>
  </>;
}
