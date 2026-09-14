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
  return <details className="review-agent-batches" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>查看批次（{total}）</summary>
    {open && <>
      {error && <p role="alert">{error}</p>}
      {page.loading && !page.data && <p role="status">正在读取批次…</p>}
      {page.data?.items.map(batch => <div className="review-agent-batch-row" key={batch.batch_number}>
        <span>第 {batch.batch_number}/{total} 批</span><b>{stopped && batch.status !== "succeeded" ? paused ? "已暂停" : "已停止" : labels[batch.status] ?? batch.status}</b>
        <small>{batch.error_message ?? (batch.duration_ms === null ? "" : `${batch.duration_ms} ms`)}</small>
        {Boolean(batch.candidates?.length) && <details><summary>查看已保存的候选问题（{batch.candidates.length}）</summary><p>这是 AI 批次的过程结果，尚需汇总和核对；最终列表为空不代表没有发现问题。</p>{batch.candidates.map((item, index) => <article key={index}><h4>{item.title}</h4><p>{item.evidence}</p><p>{item.impact}</p><p>{item.suggestion}</p></article>)}</details>}
        {batch.status === "failed" && onRetry && !stopped && <button type="button" disabled={busy}
          onClick={() => onRetry(agent, batch.batch_number)}>重试第 {batch.batch_number} 批</button>}
      </div>)}
      <Pagination page={page.page} count={page.data?.items.length ?? 0} total={total}
        hasNext={Boolean(page.data?.next_cursor)} busy={page.loading}
        onPrevious={page.previous} onNext={page.next} label="批次分页" />
    </>}
  </details>;
}
