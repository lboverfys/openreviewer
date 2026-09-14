import { useState } from "react";
import { api } from "./api";
import { Notice } from "./Feedback";
import RetrievalTracePanel, { retrievalStrategyLabels } from "./RetrievalTracePanel";
import type { RetrievalEvaluationReport, RetrievalTrace } from "./types";

function CaseDetails({entry}: {entry: Record<string, unknown>}) {
  const [trace, setTrace] = useState<RetrievalTrace | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const expected = Array.isArray(entry.expected_symbols) ? entry.expected_symbols.map(String) : [];
  const found = Array.isArray(entry.symbols) ? entry.symbols.map(String) : [];
  async function load() {
    if (trace || busy || typeof entry.trace_id !== "string") return;
    setBusy(true); setError("");
    try {setTrace(await api.retrievalHistoryDetail(entry.trace_id));}
    catch(error) {setError(error instanceof Error ? error.message : "无法读取代码");}
    finally {setBusy(false);}
  }
  return <article className="retrieval-case-basis">
    <p><strong>查询</strong>：{String(entry.query ?? entry.case_id)}</p>
    <p>仓库 {String(entry.repository ?? "历史未记录")} · 提交 {String(entry.head_sha ?? "历史未记录").slice(0, 12)}</p>
    <h4>应该找到的代码</h4><ul>{expected.map(symbol => <li key={symbol} data-found={found.includes(symbol)}><strong>{found.includes(symbol) ? "已找到" : "未找到"}</strong> · <code>{symbol}</code></li>)}</ul>
    {!expected.length && <p>这份历史报告没有保存参考符号。</p>}
    <h4>实际返回顺序</h4><ol>{found.map((symbol, rank) => <li key={rank}>{rank + 1}. <code>{symbol}</code></li>)}</ol>
    {Array.isArray(entry.routes) && <div className="retrieval-route-metrics">{entry.routes.map((route, index) => {
      const item = route as {route: string; candidate_count: number; duration_ms: number};
      return <div key={index}><span>{({bm25:"关键词",vector:"语义",relation:"代码关系"} as Record<string,string>)[item.route] ?? item.route}</span><strong>{item.candidate_count} 条候选</strong><small>{item.duration_ms} 毫秒{item.candidate_count === 0 ? " · 没有参与最终结果" : ""}</small></div>;
    })}</div>}
    {typeof entry.trace_id === "string" ? <section><button type="button" disabled={busy || Boolean(trace)} onClick={() => void load()}>{trace ? "已载入保存的代码" : "查看返回代码和完整检索过程"}</button>
      {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}{busy && <p role="status">正在读取保存的代码…</p>}
      {trace && <RetrievalTracePanel traces={[trace]}/>} {!busy && !trace && <button type="button" onClick={() => void load()}>重新读取</button>}
    </section> : <p className="ws-note">这份历史报告未保存代码正文和各路过程，不能事后补写。新对比会保存完整检索过程。</p>}
  </article>;
}

export default function RetrievalReportDetails({report}: {report: RetrievalEvaluationReport}) {
  const [strategy, setStrategy] = useState(report.strategies[0]?.strategy);
  const selected = report.strategies.find(item => item.strategy === strategy);
  return <><nav className="ws-tabs" aria-label="查看检索方式">{report.strategies.map(item => <button type="button" key={item.strategy} aria-pressed={strategy === item.strategy} onClick={() => setStrategy(item.strategy)}>{retrievalStrategyLabels[item.strategy]}</button>)}</nav>
    {selected?.cases.map((entry, index) => <CaseDetails key={strategy + ":" + index} entry={entry}/>)}
  </>;
}
