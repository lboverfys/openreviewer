import { Input } from "./components/ui/input";
import { NativeSelect } from "./components/ui/native-select";
import { Button } from "./components/ui/button";
import { Table, TableHeader, TableRow, TableHead, TableBody, TableCell } from "./components/ui/table";
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
    <div className="retrieval-form-grid"><label>查询原文（精确匹配）<Input value={query} onChange={event => setQuery(event.target.value)} /></label>
      <label>请求的策略<NativeSelect value={strategy} onChange={event => setStrategy(event.target.value)}><option value="">全部</option>{Object.entries(retrievalStrategyLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</NativeSelect></label></div>
    <Button variant="outline" type="button" disabled={page.loading} onClick={() => void page.refresh()}>刷新记录</Button>
    <div className="retrieval-table-scroll"><Table><TableHeader><TableRow><TableHead>时间 / 提交</TableHead><TableHead>查询</TableHead><TableHead>实际策略</TableHead><TableHead>耗时</TableHead><TableHead>操作</TableHead></TableRow></TableHeader><TableBody>
      {page.data?.items.map(item => <TableRow key={item.id}><TableCell>{formatDate(item.created_at)}<small>{item.repository} · {shortSha(item.head_sha)}</small></TableCell><TableCell>{item.query}</TableCell><TableCell>{retrievalStrategyLabels[item.strategy]}</TableCell><TableCell>{item.duration_ms} ms</TableCell><TableCell><Button variant="outline" onClick={() => {void api.retrievalHistoryDetail(item.id).then(setTrace).catch(onError);}}>查看返回代码</Button></TableCell></TableRow>)}
    </TableBody></Table></div>
    {!page.loading && !page.data?.items.length && <p>这个提交还没有符合条件的搜索记录。执行一次搜索后即可回看。</p>}
    <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={page.loading} onPrevious={page.previous} onNext={page.next}/>
    {trace && <DetailDialog open hideTrigger onToggle={event => {if (!event.currentTarget.open) setTrace(null);}}><summary>搜索记录 · {trace.query}</summary><RetrievalTracePanel traces={[trace]}/></DetailDialog>}
  </section>;
}
