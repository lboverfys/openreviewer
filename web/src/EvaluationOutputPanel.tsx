import { Table, TableHeader, TableRow, TableHead, TableBody, TableCell } from "./components/ui/table";
import { useCallback } from "react";
import Pagination from "./Pagination";
import { request } from "./http";
import type { EvaluationOutput } from "./types";
import { useCursorPage, type CursorPage } from "./useCursorPage";

const statuses: Record<string, string> = {
  pending: "捕获未完成", captured: "已保存", parse_failed: "已保存 · 输出解析失败",
  transport_failed: "传输或协议解析失败", oversized: "输出超过单条上限", run_limit: "运行留存容量已用完",
  missing: "缺少可保存输出", expired: "在线正文已到期",
};

export default function EvaluationOutputPanel({runId, onError}: {runId: string; onError: (error: unknown) => void}) {
  const load = useCallback((cursor?: string, signal?: AbortSignal) => request<CursorPage<EvaluationOutput>>(
    `/api/v1/evaluations/runs/${encodeURIComponent(runId)}/outputs?` + new URLSearchParams({limit:"10", ...(cursor ? {cursor} : {})}),
    {signal}), [runId]);
  const page = useCursorPage({cacheKey:"evaluation-outputs:" + runId, load, onError});
  return <section aria-label="模型调用证据">
    <h4>模型调用证据</h4>
    <p>这里只展示来源与完整性。受控正文默认保存 30 天，由维护归档工具导出；每次调用不单独重复计算工作流的最终问题。</p>
    <div className="evaluation-table-wrap"><Table><TableHeader><TableRow><TableHead>请求与角色</TableHead><TableHead>尝试</TableHead><TableHead>留存状态</TableHead><TableHead>版本</TableHead></TableRow></TableHeader>
      <TableBody>{page.data?.items.map(item => <TableRow key={item.id}>
        <TableCell><code>{item.id}</code><br />{item.agent} · 批次 {item.batch_number ?? "未记录"}</TableCell>
        <TableCell>{item.attempt_kind} · 拆分层级 {item.split_depth}</TableCell>
        <TableCell>{statuses[item.status] ?? item.status} · {item.byte_size} 字节<br />{item.output_format === "stream_reassembled_output_text" ? "流式重组输出文本" : "供应商输出文本"}</TableCell>
        <TableCell>{item.model} / {item.api_protocol}<br /><code>{item.prompt_content_sha256.slice(0, 12)}</code></TableCell>
      </TableRow>)}</TableBody></Table></div>
    <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={page.loading}
      onPrevious={page.previous} onNext={page.next} label="模型调用证据分页" />
  </section>;
}
