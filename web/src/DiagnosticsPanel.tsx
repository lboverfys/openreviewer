import { useCallback, useEffect, useState } from "react";
import Pagination from "./Pagination";
import { platformApi } from "./platform-api";
import type { PlatformPanelProps } from "./PlatformPage";
import type { DiagnosticReport } from "./types";
import { useCursorPage } from "./useCursorPage";
import { formatDate } from "./utils";
import { WorkspaceBadge, WorkspaceEmpty } from "./Workspace";
import WorkerNodesPanel from "./WorkerNodesPanel";

const duration = (value: number | null | undefined) => value == null ? "未记录" : `${(value / 1000).toFixed(1)} 秒`;
const auditLabels: Record<string, string> = { "platform.profile.created": "保存审查方案", "platform.profile.activated": "启用审查方案", "platform.static.imported": "导入静态报告", "platform.egress.denied": "外发策略阻止请求", "platform.work.created": "创建待办", "platform.work.updated": "更新待办" };

export default function DiagnosticsPanel({ onError }: PlatformPanelProps) {
  const [days, setDays] = useState(7);
  const [version, setVersion] = useState(0);
  const [report, setReport] = useState<DiagnosticReport | null>(null);
  const [auditOpen, setAuditOpen] = useState(false);
  const loadAudits = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.audits(undefined, cursor, signal, force), []);
  const audits = useCursorPage({ cacheKey: "platform-audits:all", load: loadAudits, onError, enabled: auditOpen });
  useEffect(() => { const controller = new AbortController(); setReport(null); void platformApi.diagnostics(days, controller.signal).then(setReport).catch(error => { if (!controller.signal.aborted) onError(error); }); return () => controller.abort(); }, [days, version, onError]);
  const total = (key: "queued" | "running" | "paused" | "failed") => report?.repositories.reduce((value, item) => value + item[key], 0);
  return <>
    <section className="team-card"><div className="team-toolbar"><div><h2>运行诊断</h2><p>查看任务积压、处理耗时与模型通道状态。</p></div><div className="ws-actions"><label>统计范围<select value={days} onChange={event => setDays(Number(event.target.value))}><option value={7}>最近 7 天</option><option value={30}>最近 30 天</option></select></label><button onClick={() => { setVersion(value => value + 1); if (auditOpen) void audits.refresh(); }}>刷新</button></div></div></section>
    <WorkerNodesPanel onError={onError} refreshVersion={version} />
    {!report ? <section className="team-card"><WorkspaceEmpty loading title="正在读取运行统计…" /></section> : <>
      <div className="ws-metrics">{([['queued', '当前排队'], ['running', '当前运行'], ['paused', '当前暂停'], ['failed', '区间失败']] as const).map(([key, label]) => <div className={`ws-metric${key === "running" ? " is-accent" : ""}`} key={key}><span>{label}</span><strong>{total(key)}</strong><small>{report.truncated ? "当前展示的仓库" : "可见仓库合计"}</small></div>)}</div>
      <section className="team-card"><div className="team-toolbar"><h2>仓库运行情况</h2><span className="ws-hint">{report.repositories.length} 个仓库{report.truncated ? " · 已达到展示上限" : ""}</span></div>
        <div className="team-table-wrap diagnostic-table"><table><thead><tr><th>仓库</th><th>排队 / 运行 / 暂停</th><th>产出 / 失败</th><th>最早积压</th><th>平均 / P95 排队</th><th>平均模型耗时</th><th>并发上限</th></tr></thead><tbody>{report.repositories.map(item => <tr key={item.repository}><td><strong>{item.repository}</strong></td><td className="ws-numeric">{item.queued} / {item.running} / {item.paused}</td><td className="ws-numeric">{item.completed} / {item.failed}</td><td>{item.oldest_queued_at ? formatDate(item.oldest_queued_at) : "无"}</td><td>{duration(item.mean_queue_ms)} / {duration(item.p95_queue_ms)}</td><td>{duration(item.mean_model_ms)}</td><td>{item.max_concurrent_reviews ?? "未限制"}</td></tr>)}</tbody></table></div>
        {!report.repositories.length && <WorkspaceEmpty title="暂无仓库运行数据" description="审查任务开始执行后，运行情况会显示在这里。" />}
        <p className="ws-hint">排队耗时按首次领取计算；P95 表示 95% 的已领取任务不超过该时间。</p>
      </section>
      <div className="diagnostic-secondary">
        <section className="team-card"><div className="team-toolbar"><h2>模型供应商通道</h2><WorkspaceBadge>{report.provider_channels?.length ?? 0} 个连接</WorkspaceBadge></div>
          {report.provider_channels?.length ? <div className="team-table-wrap"><table><thead><tr><th>供应商</th><th>在途请求</th><th>连续故障</th><th>通道状态</th></tr></thead><tbody>{report.provider_channels.map(item => <tr key={item.key}><td><strong>{item.provider}</strong><small><code>{item.key.slice(0, 12)}</code></small></td><td>{item.in_flight} / 3</td><td>{item.failure_count}</td><td><WorkspaceBadge tone={item.open_until ? "warning" : "success"}>{item.open_until ? "等待恢复" : "可用"}</WorkspaceBadge>{item.open_until && <small>{formatDate(item.open_until)}</small>}</td></tr>)}</tbody></table></div> : <WorkspaceEmpty title="暂无通道记录" description="模型请求使用过的连接会显示在这里。" />}
          <p className="ws-hint">连接最多 3 个在途请求，连续故障后短暂暂停并探测恢复。</p>
        </section>
        <section className="team-card"><div className="team-toolbar"><h2>最近任务错误</h2><span className="ws-hint">最近 {days} 天</span></div>{report.failures.length ? <div className="diagnostic-failure-list">{report.failures.map(item => <div key={item.code}><code>{item.code}</code><WorkspaceBadge tone="danger">{item.count} 次</WorkspaceBadge></div>)}</div> : <WorkspaceEmpty title="暂无已记录错误" description="当前统计范围内没有任务错误。" />}</section>
      </div>
    </>}
    <details className="team-card" onToggle={event => setAuditOpen(event.currentTarget.open)}><summary>操作记录<span className="ws-hint">查看方案、待办与数据外发变更</span></summary>{auditOpen && <>
      <div className="team-table-wrap"><table><thead><tr><th>时间</th><th>仓库</th><th>操作人</th><th>操作</th><th>版本</th></tr></thead><tbody>{audits.data?.items.map(item => <tr key={item.id}><td>{formatDate(item.created_at)}</td><td>{item.repository}</td><td>{item.actor}</td><td>{auditLabels[item.event_type] ?? item.event_type}</td><td>{item.revision ?? "—"}</td></tr>)}</tbody></table></div>
      {!audits.loading && !audits.data?.items.length && <WorkspaceEmpty title="暂无操作记录" />}
      <Pagination page={audits.page} count={audits.data?.items.length ?? 0} hasNext={Boolean(audits.data?.next_cursor)} busy={audits.loading} onPrevious={audits.previous} onNext={audits.next} />
    </>}</details>
  </>;
}
