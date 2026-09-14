import { useCallback, useState } from "react";
import { api } from "./api";
import { DetailDialog } from "./Feedback";
import Pagination from "./Pagination";
import RetrievalTracePanel, { retrievalStrategyLabels } from "./RetrievalTracePanel";
import { useCursorPage } from "./useCursorPage";
import type { RetrievalTrace } from "./types";
import { formatDate, shortSha } from "./utils";

export default function RetrievalHistoryPanel({indexId, onError}: {indexId: string; onError: (error: unknown) => void}) {
  const [query, setQuery] = useState("");
  const [strategy, setStrategy] = useState("");
  const [trace, setTrace] = useState<RetrievalTrace | null>(null);
  const load = useCallback((cursor?: string, signal?: AbortSignal) => api.retrievalHistory(indexId, query, strategy, cursor, signal), [indexId, query, strategy]);
  const page = useCursorPage({cacheKey: `search-history:${indexId}:${query}:${strategy}`, load, onError, enabled: Boolean(indexId)});
  return <section className="retrieval-card">
    <h2>搜索记录</h2><p>每次手动搜索都会保存。这里按所选仓库和提交回看当时的代码、实际策略与耗时。</p>
    <div className="retrieval-form-grid"><label>查询原文（精确匹配）<input value={query} onChange={event => setQuery(event.target.value)} /></label>
      <label>请求的策略<select value={strategy} onChange={event => setStrategy(event.target.value)}><option value="">全部</option>{Object.entries(retrievalStrategyLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label></div>
    <button type="button" disabled={page.loading} onClick={() => void page.refresh()}>刷新记录</button>
    <div className="retrieval-table-scroll"><table><thead><tr><th>时间 / 提交</th><th>查询</th><th>实际策略</th><th>耗时</th><th>操作</th></tr></thead><tbody>
      {page.data?.items.map(item => <tr key={item.id}><td>{formatDate(item.created_at)}<small>{item.repository} · {shortSha(item.head_sha)}</small></td><td>{item.query}</td><td>{retrievalStrategyLabels[item.strategy]}</td><td>{item.duration_ms} ms</td><td><button onClick={() => {void api.retrievalHistoryDetail(item.id).then(setTrace).catch(onError);}}>查看返回代码</button></td></tr>)}
    </tbody></table></div>
    {!page.loading && !page.data?.items.length && <p>这个提交还没有符合条件的搜索记录。执行一次搜索后即可回看。</p>}
    <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={page.loading} onPrevious={page.previous} onNext={page.next}/>
    {trace && <DetailDialog open hideTrigger onToggle={event => {if (!event.currentTarget.open) setTrace(null);}}><summary>搜索记录 · {trace.query}</summary><RetrievalTracePanel traces={[trace]}/></DetailDialog>}
  </section>;
}
