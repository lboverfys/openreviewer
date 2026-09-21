import { Button } from "./components/ui/button";
import { NativeSelect } from "./components/ui/native-select";
import { Table, TableHeader, TableRow, TableHead, TableBody, TableCell } from "./components/ui/table";
import { useCallback, useEffect, useRef, useState } from "react";
import Pagination from "./Pagination";
import { platformApi } from "./platform-api";
import type { PlatformPanelProps } from "./PlatformPage";
import type { WorkerNodePage } from "./types";
import { useCursorPage } from "./useCursorPage";
import { formatDate, workerLabels } from "./utils";
import { WorkspaceBadge, WorkspaceEmpty } from "./Workspace";

export default function WorkerNodesPanel({ onError, refreshVersion = 0 }: PlatformPanelProps & { refreshVersion?: number }) {
  const [state, setState] = useState<"online" | "offline" | "all">("online");
  const [metadata, setMetadata] = useState<Pick<WorkerNodePage, "generated_at" | "retention_days" | "online_window_seconds"> | null>(null);
  const load = useCallback(async (cursor?: string, signal?: AbortSignal, force?: boolean) => {
    const result = await platformApi.workers(state, cursor, signal, force);
    if (!signal?.aborted) setMetadata({ generated_at: result.generated_at, retention_days: result.retention_days, online_window_seconds: result.online_window_seconds });
    return result;
  }, [state]);
  const page = useCursorPage({ cacheKey: `worker-nodes:${state}`, load, onError });
  const refreshSeen = useRef(refreshVersion);
  useEffect(() => {
    if (refreshSeen.current === refreshVersion) return;
    refreshSeen.current = refreshVersion;
    void page.refresh();
  }, [refreshVersion, page.refresh]);
  return <section className="team-card" aria-label="Worker 节点管理">
    <div className="team-toolbar"><div><h2>Worker 执行节点</h2><p>在线节点与历史心跳分开查看，每页最多 10 条。</p></div><Button variant="outline" type="button" disabled={page.loading} onClick={() => void page.refresh()}>刷新节点</Button></div>
    <div className="ws-filterbar"><label>节点范围<NativeSelect value={state} onChange={event => setState(event.target.value as typeof state)}><option value="online">在线节点</option><option value="offline">离线与历史</option><option value="all">全部记录</option></NativeSelect></label><span className="ws-hint">{metadata?.generated_at ? `快照 ${formatDate(metadata.generated_at)}` : "正在读取…"}</span></div>
    <div className="team-table-wrap worker-history-table"><Table><TableHeader><TableRow><TableHead>节点 / 启动时间</TableHead><TableHead>连接状态</TableHead><TableHead>最后上报状态</TableHead><TableHead>当前任务</TableHead><TableHead>最近心跳</TableHead></TableRow></TableHeader><TableBody>{page.data?.items.map(item => <TableRow key={item.worker_id}>
      <TableCell><code className="worker-history-id" title={item.worker_id}>{item.worker_id}</code><small>{formatDate(item.started_at)}</small></TableCell>
      <TableCell><WorkspaceBadge tone={item.online ? "success" : "neutral"}>{item.online ? "在线" : "离线"}</WorkspaceBadge></TableCell>
      <TableCell>{workerLabels[item.status]}</TableCell>
      <TableCell>{item.current_review_run_id ? <a href={`#review/${encodeURIComponent(item.current_review_run_id)}`}>查看审查 →</a> : item.online && item.status === "busy" ? "任务执行中" : "—"}</TableCell>
      <TableCell>{formatDate(item.last_seen_at)}</TableCell>
    </TableRow>)}</TableBody></Table></div>
    {!page.loading && !page.data?.items.length && <WorkspaceEmpty title={state === "online" ? "暂无在线节点" : "暂无符合条件的节点记录"} description={state === "online" ? "可切换到离线与历史，核对最近一次心跳。" : "历史记录由后台按照保留策略分批清理。"} />}
    <Pagination label="Worker 节点分页" page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={page.loading} onPrevious={page.previous} onNext={page.next} />
    <p className="ws-hint">在线状态按最近 {metadata?.online_window_seconds ?? 15} 秒心跳判定，停止中的节点不计为在线。历史心跳按 {metadata?.retention_days ?? 7} 天保留策略自动分批清理。</p>
  </section>;
}
