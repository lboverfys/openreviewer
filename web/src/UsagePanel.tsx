import { useCallback, useEffect, useState } from "react";
import Pagination from "./Pagination";
import { platformApi } from "./platform-api";
import type { PlatformPanelProps } from "./PlatformPage";
import type { UsageBreakdown, UsageMonth } from "./types";
import { useCursorPage } from "./useCursorPage";
import { formatDate } from "./utils";

export const formatMoney = (value: number | null | undefined) => value == null ? "未记录" : `$${(value / 1_000_000).toFixed(6)}`;

export default function UsagePanel({ onError }: PlatformPanelProps) {
  const [month, setMonth] = useState(new Date().toISOString().slice(0, 7));
  const [selected, setSelected] = useState<UsageMonth | null>(null);
  const load = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.usage(month, cursor, signal, force), [month]);
  const page = useCursorPage({ cacheKey: `usage:${month}`, load, onError });
  return <>
    <section className="team-card">
      <div className="team-toolbar"><h2>仓库月度用量</h2><label>月份（UTC）<input aria-label="用量月份" type="month" value={month} onChange={event => { setMonth(event.target.value); setSelected(null); }} /></label><button onClick={() => void page.refresh()}>刷新</button><a href="#team">配置预算与价格</a></div>
      <p className="team-hint">审查流程按配置价格估算费用，包含审查中的向量和精排请求。未知费用单独显示；未确认请求保留预占。月度预算在团队管理中设置，模型价格在 AI 设置中维护。</p>
      <div className="team-table-wrap"><table><thead><tr><th>仓库 / 安装范围</th><th>请求</th><th>已估算费用</th><th>待确认预占</th><th>月度预算</th><th>用量状态</th><th>明细</th></tr></thead>
        <tbody>{page.data?.items.map(item => <tr key={item.id}><td>{item.repository}<small>安装 {item.installation_id}</small></td><td>{item.request_count}</td><td>{formatMoney(item.estimated_cost_microusd)}</td><td>{formatMoney(item.reserved_cost_microusd)}</td><td>{item.budget_microusd == null ? "未限制" : formatMoney(item.budget_microusd)}</td><td>{item.warning ? "已达到预算提醒线" : "正常"}<small>费用未知 {item.unknown_count} · 请求不确定 {item.uncertain_count}</small></td><td><button onClick={() => setSelected(item)}>查看请求</button></td></tr>)}</tbody></table></div>
      {!page.loading && !page.data?.items.length && <p className="platform-empty">该月份暂无新账本记录。历史费用不会自动补成零。</p>}
      <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={page.loading} onPrevious={page.previous} onNext={page.next} />
    </section>
    {selected && <UsageDetails key={selected.id} month={selected} onError={onError} />}
  </>;
}

function UsageDetails({ month, onError }: PlatformPanelProps & { month: UsageMonth }) {
  const [groups, setGroups] = useState<UsageBreakdown[]>([]);
  const load = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.requests(month.id, cursor, signal, force), [month.id]);
  const page = useCursorPage({ cacheKey: `usage-requests:${month.id}`, load, onError });
  useEffect(() => { const controller = new AbortController(); void platformApi.breakdown(month.id, controller.signal).then(setGroups).catch(error => { if (!controller.signal.aborted) onError(error); }); return () => controller.abort(); }, [month.id, onError]);
  return <section className="team-card"><h2>{month.repository} · 请求账本</h2>
    <div className="platform-metrics">{groups.map(item => <div key={`${item.purpose}:${item.model}`}><strong>{item.model}</strong><span>{item.purpose} · {item.request_count} 次</span><span>{formatMoney(item.estimated_cost_microusd)} · {item.unknown_count} 次未知费用</span></div>)}</div>
    <div className="team-table-wrap"><table><thead><tr><th>时间</th><th>模型 / 角色</th><th>状态</th><th>输入 / 输出 Token</th><th>估算费用</th><th>耗时</th><th>来源</th></tr></thead><tbody>{page.data?.items.map(item => <tr key={item.id}><td>{formatDate(item.created_at)}</td><td>{item.model}<small>{item.agent} · {item.purpose}</small></td><td>{({ reserved: "待确认", settled: "已记录", uncertain: "结果不确定" } as Record<string, string>)[item.status] ?? item.status}</td><td>{item.input_tokens ?? "未知"} / {item.output_tokens ?? "未知"}</td><td>{formatMoney(item.estimated_cost_microusd)}</td><td>{item.duration_ms == null ? "未知" : `${item.duration_ms} ms`}</td><td><a href={`#review/${encodeURIComponent(item.review_run_id)}`}>审查任务</a></td></tr>)}</tbody></table></div>
    <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={page.loading} onPrevious={page.previous} onNext={page.next} />
  </section>;
}
