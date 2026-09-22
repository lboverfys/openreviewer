import { Button } from "./components/ui/button";
import { DetailDialog, Notice } from "./Feedback";
import { useCallback, useState } from "react";
import { api } from "./api";
import Pagination from "./Pagination";
import type { BatchSnapshot } from "./types";
import { useCursorPage } from "./useCursorPage";
import { errorMessage } from "./utils";

export default function ReviewBatchList({ runId, agent, total, changeToken, onRetry, busy, stopped = false, paused = false }: {
  runId: string; agent: string; total: number; changeToken: string;
  onRetry?: (agent: string, number?: number) => void; busy: boolean;
  stopped?: boolean;
  paused?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const load = useCallback((cursor?: string, signal?: AbortSignal) =>
    api.batchPage(runId, agent, Number(cursor ?? 0), signal), [runId, agent, changeToken]);
  const onError = useCallback((reason: unknown) => setError(errorMessage(reason)), []);
  const page = useCursorPage<BatchSnapshot>({cacheKey: `batches:${runId}:${agent}`, load, onError, enabled: open});
  const labels: Record<string, string> = {succeeded: "已完成", failed: "失败", pending: "等待发送", running: "进行中"};
  return <DetailDialog className="review-agent-batches" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>查看批次（{total}）</summary>
    {open && <>
      <p>按输入容量和关联文件分组自动分批，文件数量不平均分配；大文件可能分片。</p>
      {error && <Notice kind="error" onDismiss={() => setError("")}>{error}</Notice>}
      {page.loading && !page.data && <p role="status">正在读取批次…</p>}
      {page.data?.items.map(batch => <div className="review-agent-batch-row" key={batch.batch_number}>
        <span>第 {batch.batch_number}/{total} 批</span><b>{stopped && batch.status !== "succeeded" ? paused ? "已暂停" : "已停止" : labels[batch.status] ?? batch.status}</b>
        <small>{batch.error_message ?? (batch.duration_ms === null ? "" : `${batch.duration_ms} ms`)}</small>
        <small>{batch.files?.length ?? 0} 个文件 · 预估输入 {(batch.estimated_input_tokens ?? 0).toLocaleString()} Token</small>
        {Boolean(batch.files?.length) && <details><summary>展开文件清单</summary>{batch.files?.map(file => <p key={file}>{file}</p>)}</details>}
        {Boolean(batch.candidates?.length) && <DetailDialog><summary>查看已保存的候选问题（{batch.candidates.length}）</summary><p>这是 AI 批次的过程结果，尚需汇总和核对；最终列表为空不代表没有发现问题。</p>{batch.candidates.map((item, index) => <article key={index}><h4>{item.title}</h4><p>{item.evidence}</p><p>{item.impact}</p><p>{item.suggestion}</p></article>)}</DetailDialog>}
        {batch.status === "failed" && onRetry && !stopped && <Button variant="outline" type="button" disabled={busy}
          onClick={() => onRetry(agent, batch.batch_number)}>重试第 {batch.batch_number} 批</Button>}
      </div>)}
      <Pagination page={page.page} count={page.data?.items.length ?? 0} total={total}
        hasNext={Boolean(page.data?.next_cursor)} busy={page.loading}
        onPrevious={page.previous} onNext={page.next} label="批次分页" />
    </>}
  </DetailDialog>;
}
