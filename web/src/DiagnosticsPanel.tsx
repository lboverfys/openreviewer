import { NativeSelect } from "./components/ui/native-select";
import { Button } from "./components/ui/button";
import { Table, TableHeader, TableRow, TableHead, TableBody, TableCell } from "./components/ui/table";
import { DetailDialog } from "./Feedback";
import { useCallback, useEffect, useState } from "react";
import Pagination from "./Pagination";
import { platformApi } from "./platform-api";
import type { PlatformPanelProps } from "./PlatformPage";
import type { DiagnosticReport } from "./types";
import { useCursorPage } from "./useCursorPage";
import { formatDate } from "./utils";
import { WorkspaceBadge, WorkspaceEmpty } from "./Workspace";
import WorkerNodesPanel from "./WorkerNodesPanel";
import ReviewInsightsPanel from "./ReviewInsightsPanel";

const duration = (value: number | null | undefined) => value == null ? "未记录" : `${(value / 1000).toFixed(1)} 秒`;
const auditLabels: Record<string, string> = { "platform.profile.created": "保存审查方案", "platform.profile.activated": "启用审查方案", "platform.static.imported": "导入静态报告", "platform.egress.denied": "外发策略阻止请求", "platform.work.created": "创建待办", "platform.work.updated": "更新待办" };

export default function DiagnosticsPanel({ onError }: PlatformPanelProps) {
  const [days, setDays] = useState(7);
  const [version, setVersion] = useState(0);
  const [report, setReport] = useState<DiagnosticReport | null>(null);
  const [failed, setFailed] = useState(false);
  const [auditOpen, setAuditOpen] = useState(false);
  const loadAudits = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.audits(undefined, cursor, signal, force), []);
  const audits = useCursorPage({ cacheKey: "platform-audits:all", load: loadAudits, onError, enabled: auditOpen });
  useEffect(() => {
    const controller = new AbortController(); setReport(null); setFailed(false);
    void platformApi.diagnostics(days, controller.signal).then(value => {if (!controller.signal.aborted) setReport(value);})
      .catch(error => {if (!controller.signal.aborted) {setFailed(true); onError(error);}});
    return () => controller.abort();
  }, [days, version, onError]);
  const total = (key: "queued" | "running" | "paused" | "failed") => report?.repositories.reduce((value, item) => value + item[key], 0);
  return <>
    <section className="team-card"><div className="team-toolbar"><div><h2>后台运行概况</h2><p>后台节点负责执行审查。任务不动时，先确认节点在线，再查看失败原因。</p></div><div className="ws-actions"><label>统计范围<NativeSelect value={days} onChange={event => setDays(Number(event.target.value))}><option value={7}>最近 7 天</option><option value={30}>最近 30 天</option></NativeSelect></label><Button variant="outline" onClick={() => { setVersion(value => value + 1); if (auditOpen) void audits.refresh(); }}>刷新</Button></div></div></section>
    <WorkerNodesPanel onError={onError} refreshVersion={version} />
    {!report ? <section className="team-card"><WorkspaceEmpty loading={!failed} title={failed ? "运行统计读取失败，请点击刷新重试" : "正在读取运行统计…"} /></section> : <>
      <div className="ws-metrics">{([['queued', '当前排队'], ['running', '当前运行'], ['paused', '当前暂停'], ['failed', '区间失败']] as const).map(([key, label]) => <div className={`ws-metric${key === "running" ? " is-accent" : ""}`} key={key}><span>{label}</span><strong>{total(key)}</strong><small>{report.truncated ? "当前展示的仓库" : "可见仓库合计"}</small></div>)}</div>
      <section className="team-card"><div className="team-toolbar"><h2>仓库运行情况</h2><span className="ws-hint">{report.repositories.length} 个仓库{report.truncated ? " · 已达到展示上限" : ""}</span></div>
        <div className="team-table-wrap diagnostic-table"><Table><TableHeader><TableRow><TableHead>仓库</TableHead><TableHead>排队 / 运行 / 暂停</TableHead><TableHead>产出 / 失败</TableHead><TableHead>最早积压</TableHead><TableHead>平均 / P95 排队</TableHead><TableHead>平均模型耗时</TableHead><TableHead>并发上限</TableHead></TableRow></TableHeader><TableBody>{report.repositories.map(item => <TableRow key={item.repository}><TableCell><strong>{item.repository}</strong></TableCell><TableCell className="ws-numeric">{item.queued} / {item.running} / {item.paused}</TableCell><TableCell className="ws-numeric">{item.completed} / {item.failed}</TableCell><TableCell>{item.oldest_queued_at ? formatDate(item.oldest_queued_at) : "无"}</TableCell><TableCell>{duration(item.mean_queue_ms)} / {duration(item.p95_queue_ms)}</TableCell><TableCell>{duration(item.mean_model_ms)}</TableCell><TableCell>{item.max_concurrent_reviews ?? "未限制"}</TableCell></TableRow>)}</TableBody></Table></div>
        {!report.repositories.length && <WorkspaceEmpty title="暂无仓库运行数据" description="审查任务开始执行后，运行情况会显示在这里。" />}
        <p className="ws-hint">排队耗时按首次领取计算；P95 表示 95% 的已领取任务不超过该时间。</p>
      </section>
      <div className="diagnostic-secondary">
        <section className="team-card"><div className="team-toolbar"><h2>模型供应商通道</h2><WorkspaceBadge>{report.provider_channels?.length ?? 0} 个连接</WorkspaceBadge></div>
          {report.provider_channels?.length ? <div className="team-table-wrap"><Table><TableHeader><TableRow><TableHead>供应商</TableHead><TableHead>在途请求</TableHead><TableHead>连续故障</TableHead><TableHead>通道状态</TableHead></TableRow></TableHeader><TableBody>{report.provider_channels.map(item => <TableRow key={item.key}><TableCell><strong>{item.provider}</strong><small><code>{item.key.slice(0, 12)}</code></small></TableCell><TableCell>{item.in_flight} / 3</TableCell><TableCell>{item.failure_count}</TableCell><TableCell><WorkspaceBadge tone={item.open_until ? "warning" : "success"}>{item.open_until ? "等待恢复" : "可用"}</WorkspaceBadge>{item.open_until && <small>{formatDate(item.open_until)}</small>}</TableCell></TableRow>)}</TableBody></Table></div> : <WorkspaceEmpty title="暂无通道记录" description="模型请求使用过的连接会显示在这里。" />}
          <p className="ws-hint">连接最多 3 个在途请求，连续故障后短暂暂停并探测恢复。</p>
        </section>
        <section className="team-card"><div className="team-toolbar"><h2>最近任务错误</h2><span className="ws-hint">最近 {days} 天</span></div>{report.failures.length ? <div className="diagnostic-failure-list">{report.failures.map(item => <div key={item.code}><code>{item.code}</code><WorkspaceBadge tone="danger">{item.count} 次</WorkspaceBadge></div>)}</div> : <WorkspaceEmpty title="暂无已记录错误" description="当前统计范围内没有任务错误。" />}</section>
      </div>
      {report.insights && <ReviewInsightsPanel data={report.insights}/>}
    </>}
    <DetailDialog className="team-card" onToggle={event => setAuditOpen(event.currentTarget.open)}><summary>操作记录<span className="ws-hint">查看方案、待办与数据外发变更</span></summary>{auditOpen && <>
      <div className="team-table-wrap"><Table><TableHeader><TableRow><TableHead>时间</TableHead><TableHead>仓库</TableHead><TableHead>操作人</TableHead><TableHead>操作</TableHead><TableHead>版本</TableHead></TableRow></TableHeader><TableBody>{audits.data?.items.map(item => <TableRow key={item.id}><TableCell>{formatDate(item.created_at)}</TableCell><TableCell>{item.repository}</TableCell><TableCell>{item.actor}</TableCell><TableCell>{auditLabels[item.event_type] ?? item.event_type}</TableCell><TableCell>{item.revision ?? "—"}</TableCell></TableRow>)}</TableBody></Table></div>
      {!audits.loading && !audits.data?.items.length && <WorkspaceEmpty title="暂无操作记录" />}
      <Pagination page={audits.page} count={audits.data?.items.length ?? 0} hasNext={Boolean(audits.data?.next_cursor)} busy={audits.loading} onPrevious={audits.previous} onNext={audits.next} />
    </>}</DetailDialog>
  </>;
}
