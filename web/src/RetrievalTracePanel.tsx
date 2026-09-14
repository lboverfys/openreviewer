import { DetailDialog } from "./Feedback";
import { useState } from "react";
import Pagination, { PAGE_SIZE } from "./Pagination";
import type { ContextEvidence, RetrievalTrace } from "./types";
import { formatDuration } from "./review-details";

export const retrievalStrategyLabels = {
  bm25: "关键词搜索",
  lexical_relations: "关键词 + 代码关系",
  hybrid: "关键词 + 语义搜索",
  hybrid_relations: "关键词 + 语义 + 代码关系",
  reranked: "完整检索（含相关性排序）",
} as const;

const routeLabels: Record<string, string> = { bm25: "BM25", vector: "向量", relation: "代码关系" };
const agentLabels: Record<string, string> = { security: "安全", convention: "规范", logic: "逻辑", summary: "汇总" };

export function vectorSearchLabel(mode: string | undefined): string {
  const labels: Record<string, string> = {unused: "未执行", not_used: "未执行", exact_snapshot: "提交内精确检索", hnsw_snapshot: "HNSW 加速 · 提交过滤", exact_fallback: "候选不足 · 精确回退"};
  return (mode ?? "unused").split(",").map(item => labels[item] ?? item).join(" / ");
}

export function EvidenceSnippet({ evidence }: { evidence: ContextEvidence }) {
  return <DetailDialog className="retrieval-evidence">
    <summary>
      <span className="retrieval-rank">#{evidence.rank}</span>
      <span className="retrieval-evidence-title"><strong>{evidence.symbol}</strong><small>{evidence.file} · L{evidence.start_line}–{evidence.end_line}</small></span>
      <span className={evidence.selected ? "retrieval-tag selected" : "retrieval-tag"}>{evidence.selected ? "进入上下文" : "候选"}</span>
    </summary>
    <div className="retrieval-evidence-meta">
      <span>来源：{evidence.routes.map(route => routeLabels[route] ?? route).join(" + ")}</span>
      {Object.entries(evidence.route_scores ?? {}).map(([route, score]) => <span key={route}>{routeLabels[route]} 排名 #{evidence.route_ranks?.[route] ?? "—"} · 得分 {score.toFixed(4)}</span>)}
      <span>融合排名 #{evidence.fused_rank}</span>
      <span>RRF 得分 {evidence.fusion_score.toFixed(5)}</span>
      {evidence.rerank_score != null && <span>精排得分 {evidence.rerank_score.toFixed(4)}</span>}
      <span>提交 {evidence.head_sha.slice(0, 12)}</span>
    </div>
    <pre className="retrieval-code"><code>{evidence.content}</code></pre>
    <small className="retrieval-reference-id">证据 ID：{evidence.reference_id}</small>
  </DetailDialog>;
}

function EvidenceList({items}: {items: ContextEvidence[]}) {
  const [page, setPage] = useState(1);
  return <>
    {items.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE).map(evidence => <EvidenceSnippet key={evidence.reference_id} evidence={evidence} />)}
    {items.length > PAGE_SIZE && <Pagination page={page} count={Math.min(PAGE_SIZE, items.length - (page - 1) * PAGE_SIZE)} total={items.length} hasNext={page * PAGE_SIZE < items.length} onPrevious={() => setPage(value => value - 1)} onNext={() => setPage(value => value + 1)} label="代码证据分页" />}
  </>;
}

export default function RetrievalTracePanel({ traces, compact = false }: { traces: RetrievalTrace[]; compact?: boolean }) {
  if (!traces.length) return <div className="retrieval-empty">暂无检索记录。启用混合检索后，审查会保存使用的代码上下文。</div>;
  return <div className="retrieval-traces">
    {traces.map(trace => <section className="retrieval-trace" key={trace.id}>
      <header className="retrieval-trace-heading">
        <div><h3>{trace.agent ? `${agentLabels[trace.agent] ?? trace.agent} Agent 的上下文` : "检索结果"}</h3><span>{trace.strategies_used?.length ? trace.strategies_used.map(strategy => retrievalStrategyLabels[strategy]).join(" / ") : retrievalStrategyLabels[trace.strategy]}</span></div>
        <strong>{formatDuration(trace.duration_ms)}</strong>
      </header>
      <p className="retrieval-query">{trace.query}</p>
      {(trace.queries?.length ?? 0) > 0 && <DetailDialog className="retrieval-query-details"><summary>查看 {trace.queries?.length} 组检索查询 · {trace.covered_units ?? 0}/{trace.total_units ?? 0} 个单元获得分组上下文</summary>{trace.queries?.map((query, index) => <p className="retrieval-query" key={index}>{query}</p>)}</DetailDialog>}
      {trace.requested_strategy && trace.requested_strategy !== trace.strategy && <p className="retrieval-fallback">已保留可用结果 · 原策略：{retrievalStrategyLabels[trace.requested_strategy]}</p>}
      <div className="retrieval-route-metrics">
        {trace.routes.map(metric => <div key={metric.route}><span>{routeLabels[metric.route]}</span><strong>{metric.candidate_count} 条</strong><small>{formatDuration(metric.duration_ms)}</small></div>)}
        <div><span>最终上下文</span><strong>{trace.candidates.filter(item => item.selected).length} 条</strong><small>{trace.candidates.length} 条融合候选</small></div>
      </div>
      <div className="retrieval-measurements">
        <span>本次检索模型请求：{trace.model_requests ?? 0}</span>
        <span>向量搜索：{vectorSearchLabel(trace.vector_search_mode)}</span>
        {trace.rerank_cache_hit && <span>精排缓存命中</span>}
        <span>查询向量：{!trace.routes.some(item => item.route === "vector") ? "未使用" : trace.query_cache_hit ? "缓存命中" : "本次生成"}</span>
        <span>向量耗时：{trace.routes.some(item => item.route === "vector") ? formatDuration(trace.embedding_ms ?? 0) : "未执行"}</span>
        <span>精排耗时：{trace.candidates.some(item => item.rerank_score != null) ? formatDuration(trace.rerank_ms ?? 0) : "未执行"}</span>
        <span>向量 Token：{trace.input_tokens?.toLocaleString() ?? "—"}</span>
        <span>精排 Token：{trace.rerank_tokens?.toLocaleString() ?? "—"}</span>
      </div>
      {(trace.warnings ?? []).map(warning => <p className="retrieval-warning" key={warning}>{warning}</p>)}
      <EvidenceList key={trace.id} items={compact ? trace.candidates.filter(item => item.selected) : trace.candidates} />
    </section>)}
  </div>;
}
