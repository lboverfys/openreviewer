import { Input } from "./components/ui/input";
import { Button } from "./components/ui/button";
import { Table, TableHeader, TableRow, TableHead, TableBody, TableCell } from "./components/ui/table";
import { NativeSelect } from "./components/ui/native-select";
import { DetailDialog } from "./Feedback";
import { useCallback, useEffect, useState } from "react";
import Pagination from "./Pagination";
import { platformApi } from "./platform-api";
import type { PlatformPanelProps } from "./PlatformPage";
import type { UsageBreakdown, UsageMonth, UsageRequest } from "./types";
import { useCursorPage } from "./useCursorPage";
import { formatDate } from "./utils";
import { WorkspaceBack, WorkspaceBadge, WorkspaceEmpty } from "./Workspace";

export const formatMoney = (value: number | null | undefined) => value == null ? "未记录" : `$${(value / 1_000_000).toFixed(6)}`;
const purposeLabels: Record<string, string> = { review: "代码审查", embedding: "向量检索", rerank: "精排", all: "全部用途" };
const agentLabels: Record<string, string> = { security: "安全", convention: "规范", logic: "逻辑", summary: "汇总", retrieval: "检索" };
const costReasonLabels: Record<NonNullable<UsageRequest["cost_reason"]>, string> = {
  pending: "等待请求返回", usage_missing: "未返回完整用量", pricing_missing: "调用时未配置单价",
  cache_price_missing: "调用时未配置缓存单价", incomplete_response: "响应未完整确认，费用依据不完整",
  legacy_unknown: "历史记录未保存费用缺失原因",
};
const usageStatusLabels: Record<UsageRequest["usage_status"], string> = {
  pending: "用量待返回", recorded: "用量已记录", missing: "用量缺失", partial: "仅有部分用量", unverified: "用量完整性待核对",
};

export function UsageUnknownReasons({ statistics }: { statistics: Pick<UsageBreakdown, "reserved_count" | "missing_usage_count" | "missing_price_count" | "legacy_unknown_count" | "partial_cost_count"> }) {
  const reasons = [["等待响应", statistics.reserved_count], ["缺少完整用量", statistics.missing_usage_count],
    ["缺少单价", statistics.missing_price_count], ["历史原因未记录", statistics.legacy_unknown_count]] as const;
  const unknown = reasons.filter(([, count]) => count != null && count > 0).map(([label, count]) => `${label} ${count} 次`);
  return <>{unknown.length > 0 && <span>费用待确认原因：{unknown.join("；")}</span>}
    {(statistics.partial_cost_count ?? 0) > 0 && <span>另有 {statistics.partial_cost_count} 次仅记录部分估算费用，完整金额待核对。</span>}</>;
}

function UsageAccountingDetails({ item }: { item: UsageRequest }) {
  const prices = item.pricing_snapshot, usage = item.usage_details;
  if (!prices && !usage) return null;
  const priceLabels = { input_usd_per_million: "普通输入", output_usd_per_million: "输出",
    cache_read_usd_per_million: "缓存读取", cache_write_usd_per_million: "缓存写入" };
  return <DetailDialog><summary>计费依据</summary>
    <h3>{item.model} · 本次请求</h3>
    <p>使用请求发生时保存的单价，单位为美元 / 百万 Token。估算金额需以服务商账单核对。</p>
    {prices ? <ul>{Object.entries(priceLabels).filter(([key]) => key in prices).map(([key, label]) =>
      <li key={key}>{label}单价：{prices[key] ?? "未配置"}</li>)}</ul> : <p>本次请求未保存可用的单价配置。</p>}
    {usage ? <p>普通输入 {usage.input_tokens} Token；缓存读取 {usage.cache_read_input_tokens ?? 0} Token；缓存写入 {usage.cache_write_input_tokens ?? 0} Token；输出 {usage.output_tokens} Token。推理输出 {usage.reasoning_output_tokens ?? 0} Token 已包含在输出中。</p> : <p>未取得完整 Token 明细。</p>}
    {item.cost_reason && <p>{costReasonLabels[item.cost_reason]}</p>}
  </DetailDialog>;
}

export default function UsagePanel({ onError }: PlatformPanelProps) {
  const [month, setMonth] = useState(new Date().toISOString().slice(0, 7));
  const [selected, setSelected] = useState<UsageMonth | null>(null);
  const load = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.usage(month, cursor, signal, force), [month]);
  const page = useCursorPage({ cacheKey: `usage:${month}`, load, onError });
  if (selected) return <><WorkspaceBack onClick={() => setSelected(null)}>返回月度用量</WorkspaceBack><UsageDetails key={selected.id} month={page.data?.items.find(item => item.id === selected.id) ?? selected} onError={onError} onRefreshSummary={() => void page.refresh()} /></>;
  return <section className="team-card">
    <div className="team-toolbar"><div><h2>仓库月度用量</h2><p>查看审查费用、预占金额与预算状态。</p></div><div className="ws-actions"><a className="ws-button-link" href="#settings?section=agents">模型与费用配置</a></div></div>
    <div className="ws-filterbar"><label>月份（UTC）<Input aria-label="用量月份" type="month" value={month} onChange={event => setMonth(event.target.value)} /></label><div className="ws-actions"><Button variant="outline" disabled={page.loading} onClick={() => void page.refresh()}>刷新</Button></div></div>
    <div className="team-table-wrap"><Table><TableHeader><TableRow><TableHead>仓库</TableHead><TableHead>请求</TableHead><TableHead>已估算费用</TableHead><TableHead>待确认预占</TableHead><TableHead>月度预算</TableHead><TableHead>用量状态</TableHead><TableHead>操作</TableHead></TableRow></TableHeader>
      <TableBody>{page.data?.items.map(item => <TableRow key={item.id}><TableCell><strong>{item.repository}</strong><small>安装 {item.installation_id}</small></TableCell><TableCell className="ws-numeric">{item.request_count}</TableCell><TableCell className="ws-numeric">{formatMoney(item.estimated_cost_microusd)}</TableCell><TableCell className="ws-numeric">{formatMoney(item.reserved_cost_microusd)}</TableCell><TableCell className="ws-numeric">{item.budget_microusd == null ? "未限制" : formatMoney(item.budget_microusd)}</TableCell><TableCell><WorkspaceBadge tone={item.warning ? "warning" : "success"}>{item.warning ? "已达到预算提醒线" : "正常"}</WorkspaceBadge><small>费用未知 {item.unknown_count} · 请求不确定 {item.uncertain_count}</small></TableCell><TableCell><div className="ws-actions"><Button variant="outline" className="ws-link-button" onClick={() => setSelected(item)}>查看请求</Button><a className="ws-button-link" href={"#team?section=budget&repository=" + encodeURIComponent(item.repository)}>预算设置</a></div></TableCell></TableRow>)}</TableBody>
    </Table></div>
    {!page.loading && !page.data?.items.length && <WorkspaceEmpty title="该月份暂无用量记录" description="新审查流程的模型请求会记入账本，历史费用不会补记为零。" />}
    <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={page.loading} onPrevious={page.previous} onNext={page.next} />
    <p className="ws-hint">费用按配置价格估算，包含审查中的向量与精排。未知费用与待确认预占单独保留。</p>
  </section>;
}

function UsageDetails({ month, onError, onRefreshSummary }: PlatformPanelProps & { month: UsageMonth; onRefreshSummary: () => void }) {
  const [groups, setGroups] = useState<UsageBreakdown[]>([]);
  const [groupBy, setGroupBy] = useState<"model" | "agent">("model");
  const [version, setVersion] = useState(0);
  const load = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.requests(month.id, cursor, signal, force), [month.id]);
  const page = useCursorPage({ cacheKey: `usage-requests:${month.id}`, load, onError });
  useEffect(() => { const controller = new AbortController(); void platformApi.breakdown(month.id, controller.signal, groupBy).then(value => {if (!controller.signal.aborted) setGroups(value);}).catch(error => { if (!controller.signal.aborted) onError(error); }); return () => controller.abort(); }, [month.id, onError, version, groupBy]);
  return <>
    <section className="team-card"><div className="team-toolbar"><div><h2>{month.repository} · 请求账本</h2><p>{month.month} · UTC 自然月 · 安装 {month.installation_id}</p></div><Button variant="outline" disabled={page.loading} onClick={() => { void page.refresh(); setVersion(value => value + 1); onRefreshSummary(); }}>刷新明细</Button></div></section>
    <div className="ws-metrics">
      <div className="ws-metric is-accent"><span>已估算费用</span><strong>{formatMoney(month.estimated_cost_microusd)}</strong><small>按配置价格统计</small></div>
      <div className="ws-metric"><span>待确认预占</span><strong>{formatMoney(month.reserved_cost_microusd)}</strong><small>完整费用确认前保留预占</small></div>
      <div className="ws-metric"><span>模型请求</span><strong>{month.request_count}</strong><small>包含重试请求</small></div>
      <div className="ws-metric"><span>月度预算</span><strong>{month.budget_microusd == null ? "未限制" : formatMoney(month.budget_microusd)}</strong><small>费用未知 {month.unknown_count} 次</small></div>
    </div>
    <section className="team-card"><div className="team-toolbar"><h2>请求明细</h2><WorkspaceBadge>{month.month}</WorkspaceBadge></div>
      <div className="team-table-wrap"><Table><TableHeader><TableRow><TableHead>时间</TableHead><TableHead>模型 / 用途</TableHead><TableHead>请求 / 用量</TableHead><TableHead>输入 / 输出 Token</TableHead><TableHead>估算费用</TableHead><TableHead>耗时</TableHead><TableHead>来源</TableHead></TableRow></TableHeader><TableBody>{page.data?.items.map(item => <TableRow key={item.id}>
        <TableCell>{formatDate(item.created_at)}</TableCell><TableCell><strong>{item.model}</strong><small>{agentLabels[item.agent] ?? item.agent} · {purposeLabels[item.purpose] ?? item.purpose}</small></TableCell>
        <TableCell><WorkspaceBadge tone={item.status === "settled" ? "success" : "warning"}>{item.status === "reserved" ? "等待响应" : "请求已结束"}</WorkspaceBadge><small>{item.response_status == null ? "未记录响应状态" : `HTTP ${item.response_status}`}</small><small>{usageStatusLabels[item.usage_status]}</small></TableCell>
        <TableCell className="ws-numeric">{item.input_tokens ?? "未知"} / {item.output_tokens ?? "未知"}</TableCell>
        <TableCell><strong>{item.estimated_cost_microusd == null ? "待确认" : formatMoney(item.estimated_cost_microusd)}</strong>{item.cost_status === "partial" && <small>部分估算</small>}{item.cost_reason && <small>{costReasonLabels[item.cost_reason]}</small>}<UsageAccountingDetails item={item} /></TableCell>
        <TableCell className="ws-numeric">{item.duration_ms == null ? "未知" : `${(item.duration_ms / 1000).toFixed(1)} 秒`}</TableCell><TableCell><a href={`#review/${encodeURIComponent(item.review_run_id)}`}>审查任务 →</a></TableCell>
      </TableRow>)}</TableBody></Table></div>
      {!page.loading && !page.data?.items.length && <WorkspaceEmpty title="暂无请求明细" />}
      <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={page.loading} onPrevious={page.previous} onNext={page.next} />
      <DetailDialog className="usage-groups"><summary>按模型、用途与角色汇总<span className="ws-chip">{groups.length} 组</span></summary>
        <label>费用分组<NativeSelect aria-label="费用分组" value={groupBy} onChange={event => setGroupBy(event.target.value as "model" | "agent")}><option value="model">模型与用途</option><option value="agent">Agent 角色</option></NativeSelect></label>
        {groups.some(item => item.groups_truncated) && <p className="ws-note">只显示前 100 组；合计与费用占比的分母仍覆盖全部请求。</p>}
        <div className="platform-metrics">{groups.map(item => <div key={`${item.agent ?? ""}:${item.provider ?? ""}:${item.purpose}:${item.model}`}>
          <strong>{item.agent ? agentLabels[item.agent] ?? item.agent : item.model}</strong><span>{item.provider ? item.provider + " · " : ""}{purposeLabels[item.purpose] ?? item.purpose} · {item.request_count} 次</span>
          <span>已知估算费用 {formatMoney(item.estimated_cost_microusd)} · 已知 {item.known_count ?? "未记录"} 次 / 未知 {item.unknown_count} 次</span>
          <span>占全部已知费用 {item.known_cost_share == null ? "—" : (item.known_cost_share * 100).toFixed(1) + "%"}</span>
          <span>等待响应 {item.reserved_count ?? "未记录"} · 待核对 {item.uncertain_count ?? "未记录"} · 已记录 {item.settled_count ?? "未记录"}</span>
          <UsageUnknownReasons statistics={item} />
          <span>请求 P50 / P95：{item.p50_duration_ms == null ? "未记录" : item.p50_duration_ms + " ms"} / {item.p95_duration_ms == null ? "未记录" : item.p95_duration_ms + " ms"}（{item.duration_sample_count ?? "未记录"} 次，含失败请求）</span>
          <span>同批已记录且费用已知请求 {item.settled_priced_count ?? "未记录"} 次：预占 {formatMoney(item.settled_reservation_microusd)} / 估算 {formatMoney(item.settled_cost_microusd)}</span>
        </div>)}</div>
        <p>费用按每次调用时的价格与实际用量估算，尚未与服务商账单核对。请求结束后仍可能缺少用量或单价；历史记录缺少的依据不会用当前价格补填。请求耗时采用离散 P50/P95，包含失败请求。</p>
      </DetailDialog>
    </section>
  </>;
}
