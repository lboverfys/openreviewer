import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, peekReadCache, subscribeReadCache } from "./api";
import RetrievalTracePanel, { retrievalStrategyLabels, vectorSearchLabel } from "./RetrievalTracePanel";
import CodeIndexPanel from "./CodeIndexPanel";
import Pagination from "./Pagination";
import { useCursorPage } from "./useCursorPage";
import { formatDuration } from "./review-details";
import type { CodeIndexView, IndexTarget, RetrievalOperations, RetrievalEvaluationReport, RetrievalSettingsView, RetrievalStrategy, RetrievalTrace } from "./types";
import { errorMessage, formatDate } from "./utils";

type Tab = "search" | "evaluations";
const annotationLabels: Record<string, string> = { synthetic_contract: "合成契约样本", agent_annotated: "代理标注 · 非独立人工金标", independent_human: "独立人工标注" };

export default function RetrievalPage({ onSignedOut, initialReviewRunId }: {
  onSignedOut: (message?: string) => void; initialReviewRunId?: string;
}) {
  const [tab, setTab] = useState<Tab>("search");
  const [operations, setOperations] = useState<RetrievalOperations | null>(() => peekReadCache<RetrievalOperations>("retrieval-operations") ?? null);
  const cachedSettings = peekReadCache<RetrievalSettingsView>("retrieval-settings");
  const [view, setView] = useState<RetrievalSettingsView | null>(cachedSettings ?? null);
  const [selectedId, setSelectedId] = useState("");
  const [reviewRunId, setReviewRunId] = useState(initialReviewRunId ?? "");
  const [query, setQuery] = useState("");
  const [seedFiles, setSeedFiles] = useState("");
  const [strategy, setStrategy] = useState<RetrievalStrategy>("lexical_relations");
  const [trace, setTrace] = useState<RetrievalTrace | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const searchController = useRef<AbortController | null>(null);

  const handleError = useCallback((failure: unknown) => {
    if (failure instanceof ApiError && failure.status === 401) onSignedOut("登录已过期");
    else setError(errorMessage(failure));
  }, [onSignedOut]);

  const loadIndexes = useCallback((cursor?: string, signal?: AbortSignal, force = false) => api.retrievalIndexes(signal, cursor, force), []);
  const loadTargets = useCallback((cursor?: string, signal?: AbortSignal, force = false) => api.retrievalTargets(signal, cursor, force), []);
  const loadReports = useCallback((cursor?: string, signal?: AbortSignal, force = false) => api.retrievalEvaluations(signal, cursor, force), []);
  const indexPage = useCursorPage<CodeIndexView>({cacheKey: "retrieval-indexes", load: loadIndexes, onError: handleError, enabled: tab === "search"});
  const targetPage = useCursorPage<IndexTarget>({cacheKey: "retrieval-targets", load: loadTargets, onError: handleError, enabled: tab === "search"});
  const reportPage = useCursorPage<RetrievalEvaluationReport>({cacheKey: "retrieval-evaluations", load: loadReports, onError: handleError, enabled: tab === "evaluations"});
  const indexes = indexPage.data?.items ?? [];
  const targets = targetPage.data?.items ?? [];
  const reports = (reportPage.data?.items ?? []).map(report => report.strategies.some(item => item.strategy !== "bm25" && item.strategy !== "lexical_relations") ? report : {...report, query_cache_mode: "not_used", vector_search_mode: "not_used"});
  const selected = indexes.find(item => item.id === selectedId);

  const applySettings = useCallback((next: RetrievalSettingsView) => setView(next), []);
  const loadOverview = useCallback(async (signal?: AbortSignal, force = false) => {
    // 每块数据独立显示，慢请求或失败不会阻塞索引列表。
    await Promise.allSettled([
      api.retrievalSettings(signal, force).then(next => {
        if (!signal?.aborted) applySettings(next);
      }).catch(failure => {if (!signal?.aborted) handleError(failure);}),
      api.retrievalOperations(signal, force).then(next => {
        if (!signal?.aborted) setOperations(next);
      }).catch(failure => {if (!signal?.aborted) handleError(failure);}),
    ]);
  }, [handleError, applySettings]);
  useEffect(() => {
    const controller = new AbortController();
    const unsubscribe = subscribeReadCache<RetrievalOperations>("retrieval-operations", setOperations);
    const unsubscribeSettings = subscribeReadCache<RetrievalSettingsView>("retrieval-settings", applySettings);
    void loadOverview(controller.signal);
    return () => {controller.abort(); unsubscribe(); unsubscribeSettings(); searchController.current?.abort();};
  }, [loadOverview, applySettings]);
  useEffect(() => {
    const items = indexPage.data?.items;
    if (items) setSelectedId(current => items.some(item => item.id === current) ? current : items.find(item => item.lexical_ready)?.id ?? items[0]?.id ?? "");
  }, [indexPage.data]);
  const refresh = async () => {
    await Promise.allSettled([loadOverview(undefined, true), ...(tab === "search" ? [indexPage.refresh(), targetPage.refresh()] : tab === "evaluations" ? [reportPage.refresh()] : [])]);
  };
  const pending = tab === "search" && indexes.some(item => item.status === "queued" || item.status === "building");
  useEffect(() => {
    if (!pending) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (document.visibilityState !== "hidden") {
        await indexPage.refresh(true, controller.signal);
        try {
          const metrics = await api.retrievalOperations(controller.signal, true);
          if (!controller.signal.aborted) setOperations(metrics);
        } catch (failure) {if (!controller.signal.aborted) handleError(failure);}
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 5000);
    };
    timer = setTimeout(() => void poll(), 3000);
    return () => {controller.abort(); clearTimeout(timer);};
  }, [pending, indexPage.refresh, handleError]);

  async function action(name: string, operation: () => Promise<void>) {
    setBusy(name); setError(""); setMessage("");
    try { await operation(); } catch (failure) { handleError(failure); } finally { setBusy(""); }
  }

  function downloadReport(report: RetrievalEvaluationReport) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = `retrieval-evaluation-${report.id}.json`; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return <main className="retrieval-shell">
    <div className="retrieval-main">
    <section className="retrieval-hero">
      <div className="retrieval-hero-copy">
        <h1>代码检索</h1>
        <p>审查会自动从这里取关联代码；你也可以手动搜索，核对 AI 使用的依据。这里不直接生成代码审查结论。</p><nav className="workspace-links"><a href="#knowledge">← 知识文档</a><a href="#settings?section=retrieval">检索模型与开关 →</a></nav>
      </div>
      <button type="button" className="btn-ghost" disabled={Boolean(busy)} onClick={() => void refresh()}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
        刷新数据
      </button>
    </section>
    <div className="retrieval-overview" aria-label="检索运行概况">
      <div className="retrieval-overview-card">
        <span className="retrieval-overview-icon is-base" aria-hidden="true">⌘</span>
        <div><span>可用基础索引</span><strong>{operations?.available_indexes ?? "—"}</strong><small>关键词与代码关系</small></div>
      </div>
      <div className="retrieval-overview-card">
        <span className="retrieval-overview-icon is-queue" aria-hidden="true">⏳</span>
        <div><span>正在排队 / 构建</span><strong>{operations?.pending_indexes ?? "—"}</strong><small>最长等待 {operations ? formatDuration(Math.round((operations.oldest_pending_seconds ?? 0) * 1000)) : "—"}</small></div>
      </div>
      <div className="retrieval-overview-card">
        <span className="retrieval-overview-icon is-model" aria-hidden="true">⚡</span>
        <div><span>向量与精排</span><strong className="retrieval-overview-status">{view?.external_calls_paused ? "已暂停" : operations?.circuit_open ? "短暂熔断" : operations?.provider_busy ? "处理中" : view ? "按需调用" : "—"}</strong><small>基础检索始终独立运行</small></div>
      </div>
      <div className="retrieval-overview-card">
        <span className="retrieval-overview-icon is-limit" aria-hidden="true">▦</span>
        <div><span>每次操作请求上限</span><strong>{view?.settings.max_requests_per_operation ?? "—"}</strong><small>包含失败后的重试</small></div>
      </div>
    </div>
    <nav className="seg-tabs retrieval-tabs" aria-label="代码检索页面">
      {([["search", "索引与检索"], ["evaluations", "检索效果（高级）"]] as const).map(([key, label]) => <button key={key} className={tab === key ? "is-active" : ""} aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}
    </nav>
    {error && <div role="alert" className="toast-banner is-error">{error}</div>}
    {view?.external_calls_paused && <div className="retrieval-pause-note"><span aria-hidden="true">Ⅱ</span><div><strong>向量与精排调用已关闭</strong><p>关键词和代码关系检索仍可用于审查，GPT 审查不受影响。<a href="#settings?section=retrieval">到模型配置开启 →</a></p></div></div>}
    {message && <div role="status" className="toast-banner is-success">{message}</div>}
    {<>
      {tab === "search" && <div className="retrieval-workspace">
        <CodeIndexPanel
          indexPagination={<Pagination page={indexPage.page} count={indexes.length} hasNext={Boolean(indexPage.data?.next_cursor)} busy={indexPage.loading} onPrevious={indexPage.previous} onNext={indexPage.next} label="代码索引分页" />}
          targetPagination={<Pagination page={targetPage.page} count={targets.length} hasNext={Boolean(targetPage.data?.next_cursor)} busy={targetPage.loading} onPrevious={targetPage.previous} onNext={targetPage.next} label="审查提交分页" />}
          indexes={indexes} selectedId={selectedId} targets={targets} reviewRunId={reviewRunId} paused={Boolean(view?.external_calls_paused)} busy={Boolean(busy)}
          onSelect={id => {setSelectedId(id); setTrace(null); searchController.current?.abort();}} onTarget={setReviewRunId}
          onBuild={() => void action("index", async () => {const created = await api.createCodeIndex(reviewRunId); setSelectedId(created.id); indexPage.reset(); await refresh(); setMessage("基础索引已提交，不会调用模型");})}
          onRetry={id => void action("retry", async () => {await api.retryCodeIndex(id); await refresh();})}
          onEnrich={id => void action("enrich", async () => {await api.enrichCodeIndex(id); await refresh(); setMessage("向量补全已排队，受已保存的数量与请求上限控制");})} />
        <section className="retrieval-card retrieval-search-area">
          <div className="retrieval-section-label">02 / 检索工作区</div>
          <h2>沿着证据，找到代码</h2>
          <p className="retrieval-muted">输入方法名、业务问题或代码片段，查看每一路召回与排序依据。</p>
          <form onSubmit={event => {
            event.preventDefault();
            void action("search", async () => {
              searchController.current?.abort();
              const controller = new AbortController(); searchController.current = controller;
              const next = await api.searchCodeIndex(selectedId, {query, strategy, symbols: [], seed_files: seedFiles.split(/\r?\n/).map(value => value.trim()).filter(Boolean), limit: view?.settings.context_k ?? 8}, controller.signal);
              if (!controller.signal.aborted) setTrace(next);
            });
          }}>
            <label>需要查找的代码或问题<textarea rows={3} value={query} maxLength={4000} onChange={event => setQuery(event.target.value)} placeholder="例如：查找循环查询用户信息所调用的 Mapper 和 SQL，检查是否可改为批量查询。" /></label>
            <div className="retrieval-form-grid">
              <label>策略<select value={strategy} onChange={event => setStrategy(event.target.value as RetrievalStrategy)}>{Object.entries(retrievalStrategyLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
              <label>变更文件路径（每行一个，可选）<textarea rows={2} value={seedFiles} onChange={event => setSeedFiles(event.target.value)} placeholder="用于代码关系召回" /></label>
            </div>
            <button className="primary" disabled={Boolean(busy) || !selected?.lexical_ready || !query.trim()}>{busy === "search" ? "正在召回与排序…" : "执行检索"}</button>
          </form>
          {trace?.index_id === selectedId && <RetrievalTracePanel traces={[trace]} />}
          {!trace && <div className="retrieval-search-welcome"><span className="retrieval-empty-icon" aria-hidden="true">⌕</span><h3>从一个问题开始</h3><p>检索后，你可以展开源码、检查关联 SQL，并追溯到具体提交。</p><div className="retrieval-suggestions">{["查找用户权限校验及关联 SQL", "哪些方法包含事务与行锁？"].map(text => <button type="button" key={text} onClick={() => setQuery(text)}>{text} ↗</button>)}</div></div>}
        </section>
      </div>}
      {tab === "evaluations" && <section className="retrieval-card">
        <h2>固定样本策略对比</h2><p className="retrieval-muted">对照相同样本、模型与 K 值，查看每一路召回和精排带来的变化。检索指标不等同于代码审查准确率。</p>
        {!reports.length && <div className="retrieval-empty">尚未运行评测。完成样本标注并执行评测工具后，结果会显示在这里。</div>}
        {reports.map(report => {
          const baseline = report.strategies.find(item => item.strategy === "bm25");
          return <article className="retrieval-report" key={report.id}>
            <header><div><h3>{report.dataset_version}</h3><small>{annotationLabels[report.annotation_source]} · {formatDate(report.generated_at)}</small></div><button onClick={() => downloadReport(report)}>导出报告</button></header>
            <p>{report.embedding_model} / {report.rerank_model}</p><p className="retrieval-muted">查询缓存：{report.query_cache_mode === "not_used" ? "未使用" : report.query_cache_mode === "shared_warm" ? "统一预热" : report.query_cache_mode} · 向量搜索：{vectorSearchLabel(report.vector_search_mode)}</p>
            <p className="retrieval-muted">词法缓存：{report.lexical_cache_mode === "shared_warm" ? "各策略统一预热" : "未记录"}</p>
            <div className="retrieval-table-scroll"><table><thead><tr><th>策略</th><th>样本数</th><th>Recall@K</th><th>MRR</th><th>中位耗时</th><th>P95 耗时</th><th>召回率较基线</th></tr></thead><tbody>
              {report.strategies.map(item => <tr key={item.strategy}><td>{retrievalStrategyLabels[item.strategy]}</td><td>{item.sample_count}</td><td>{(item.recall_at_k * 100).toFixed(1)}% <small>K={item.k}</small></td><td>{item.mrr.toFixed(3)}</td><td>{formatDuration(Math.round(item.median_duration_ms))}</td><td>{formatDuration(Math.round(item.p95_duration_ms))}</td><td>{baseline ? `${item.recall_at_k >= baseline.recall_at_k ? "+" : ""}${((item.recall_at_k - baseline.recall_at_k) * 100).toFixed(1)} 个百分点` : "—"}</td></tr>)}
            </tbody></table></div>
            <p className="retrieval-muted">真实审查准确率：{report.real_review_accuracy == null ? "未评测" : `${(report.real_review_accuracy * 100).toFixed(1)}%`}。标注来源和样本规模是解释结果的必要条件。</p>
          </article>;
        })}
      <Pagination page={reportPage.page} count={reports.length} hasNext={Boolean(reportPage.data?.next_cursor)} busy={reportPage.loading} onPrevious={reportPage.previous} onNext={reportPage.next} label="评测报告分页" /></section>}

    </>}
    </div>
  </main>;
}
