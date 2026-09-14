import { useState } from "react";
import { api } from "./api";
import { retrievalStrategyLabels } from "./RetrievalTracePanel";
import type { RetrievalStrategy } from "./types";

export default function RetrievalCompareForm({indexId, onSaved, onError}: {indexId: string; onSaved: () => void; onError: (error: unknown) => void}) {
  const [query, setQuery] = useState("");
  const [expected, setExpected] = useState("");
  const [strategies, setStrategies] = useState<RetrievalStrategy[]>(["bm25", "lexical_relations"]);
  const [busy, setBusy] = useState(false);
  return <form className="retrieval-compare-form" onSubmit={event => {
    event.preventDefault(); setBusy(true);
    void api.compareRetrieval(indexId, {query, relevant_symbols: [...new Set(expected.split(/\r?\n/).map(value => value.trim()).filter(Boolean))], strategies, k: 8})
      .then(onSaved).catch(onError).finally(() => setBusy(false));
  }}>
    <h2>比较检索方式</h2><p>先根据代码确认“应该找到什么”，再用同一提交和查询比较。普通搜索只保存记录，不自动产生评测分数。</p>
    <label>测试查询<textarea required maxLength={4000} value={query} onChange={event => setQuery(event.target.value)}/></label>
    <label>应该找到的完整代码符号名（每行一个）<textarea required value={expected} onChange={event => setExpected(event.target.value)} placeholder="从该提交的源码核对完整方法名或 SQL 名称，不要只照抄搜索返回的结果"/></label>
    <fieldset className="retrieval-strategy-options" disabled={busy}><legend>需要比较的方式</legend>{Object.entries(retrievalStrategyLabels).map(([key, label]) => <label className="ws-check" key={key}><input type="checkbox" checked={strategies.includes(key as RetrievalStrategy)} onChange={event => setStrategies(current => event.target.checked ? [...current, key as RetrievalStrategy] : current.filter(item => item !== key))}/>{label}</label>)}</fieldset>
    <p>来源如实记录为单人标注。向量和精排策略可能调用已配置的模型，并受调用开关与预算限制；策略降级时不会生成误导性的对比分数。</p>
    <button className="ws-primary" disabled={busy || !indexId || strategies.length < 2}>{busy ? "正在比较…" : "按参考代码运行对比"}</button>
  </form>;
}
