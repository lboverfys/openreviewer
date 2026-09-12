import { useCallback, useEffect, useState } from "react";
import Pagination from "./Pagination";
import { platformApi } from "./platform-api";
import type { PlatformPanelProps } from "./PlatformPage";
import type { DiagnosticReport } from "./types";
import { useCursorPage } from "./useCursorPage";
import { formatDate } from "./utils";

const duration = (value: number | null | undefined) => value == null ? "未记录" : `${(value / 1000).toFixed(1)} 秒`;

export default function DiagnosticsPanel({ onError }: PlatformPanelProps) {
  const [days, setDays] = useState(7);
  const [version, setVersion] = useState(0);
  const [report, setReport] = useState<DiagnosticReport | null>(null);
  const loadAudits = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.audits(undefined, cursor, signal, force), []);
  const audits = useCursorPage({ cacheKey: "platform-audits:all", load: loadAudits, onError });
  useEffect(() => { const controller = new AbortController(); setReport(null); void platformApi.diagnostics(days, controller.signal).then(setReport).catch(error => { if (!controller.signal.aborted) onError(error); }); return () => controller.abort(); }, [days, version, onError]);
  return <>
    {Boolean(report?.provider_channels?.length) && <section className="team-card"><h2>模型供应商通道</h2><p className="team-hint">每个连接最多同时发送 3 个请求；连续 5 次服务故障或限流后暂停 60 秒，再允许一个请求探测恢复。</p><div className="team-table-wrap"><table><thead><tr><th>供应商 / 连接标识</th><th>进行中请求</th><th>连续故障</th><th>暂停至</th></tr></thead><tbody>{report?.provider_channels?.map(item => <tr key={item.key}><td>{item.provider} · {item.key.slice(0, 12)}</td><td>{item.in_flight}</td><td>{item.failure_count}</td><td>{item.open_until ? formatDate(item.open_until) : "可用"}</td></tr>)}</tbody></table></div></section>}
    <section className="team-card"><div className="team-toolbar"><h2>运行诊断</h2><label>统计范围<select value={days} onChange={event => setDays(Number(event.target.value))}><option value={7}>最近 7 天</option><option value={30}>最近 30 天</option></select></label><button onClick={() => { setVersion(value => value + 1); void audits.refresh(); }}>刷新</button></div>
      <p className="team-hint">当前积压包含更早的待处理任务。排队耗时从创建到首次领取计算；P95 表示 95% 的已领取任务不超过该时间。模型耗时包含并行请求各自耗时。</p>
      {!report && <p role="status">正在读取运行统计…</p>}
      {report && <><div className="team-table-wrap"><table><thead><tr><th>仓库</th><th>排队 / 运行 / 暂停</th><th>审查产出 / 失败</th><th>最早积压</th><th>平均 / P95 排队</th><th>平均模型耗时</th><th>仓库并发上限</th></tr></thead><tbody>{report.repositories.map(item => <tr key={item.repository}><td>{item.repository}</td><td>{item.queued} / {item.running} / {item.paused}</td><td>{item.completed} / {item.failed}</td><td>{item.oldest_queued_at ? formatDate(item.oldest_queued_at) : "无"}</td><td>{duration(item.mean_queue_ms)} / {duration(item.p95_queue_ms)}</td><td>{duration(item.mean_model_ms)}</td><td>{item.max_concurrent_reviews ?? "未限制"}</td></tr>)}</tbody></table></div>{report.truncated && <p>当前展示前 100 个仓库。</p>}<h3>最近任务错误</h3><div className="platform-metrics">{report.failures.map(item => <div key={item.code}><strong>{item.count} 次</strong><span>{item.code}</span></div>)}</div>{!report.failures.length && <p>当前范围内没有已记录的任务错误。</p>}</>}
    </section>
    <section className="team-card"><h2>操作记录</h2><div className="team-table-wrap"><table><thead><tr><th>时间</th><th>仓库</th><th>操作人</th><th>操作</th><th>版本</th></tr></thead><tbody>{audits.data?.items.map(item => <tr key={item.id}><td>{formatDate(item.created_at)}</td><td>{item.repository}</td><td>{item.actor}</td><td>{item.event_type}</td><td>{item.revision ?? "—"}</td></tr>)}</tbody></table></div><Pagination page={audits.page} count={audits.data?.items.length ?? 0} hasNext={Boolean(audits.data?.next_cursor)} busy={audits.loading} onPrevious={audits.previous} onNext={audits.next} /></section>
  </>;
}
