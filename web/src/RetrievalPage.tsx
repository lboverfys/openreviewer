import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { Brand } from "./Auth";
import RetrievalTracePanel, { retrievalStrategyLabels } from "./RetrievalTracePanel";
import CodeIndexPanel from "./CodeIndexPanel";
import { formatDuration } from "./review-details";
import type { AuthUser, CodeIndexView, IndexTarget, RetrievalOperations, RetrievalEvaluationReport, RetrievalSettings, RetrievalSettingsView, RetrievalStrategy, RetrievalTrace } from "./types";
import { errorMessage, formatDate } from "./utils";
import "./styles/retrieval.css";

type Tab = "search" | "evaluations" | "settings";
const annotationLabels: Record<string, string> = { synthetic_contract: "合成契约样本", agent_annotated: "代理标注 · 非独立人工金标", independent_human: "独立人工标注" };

export default function RetrievalPage({ user, onBack, onSignedOut, initialReviewRunId }: {
  user: AuthUser; onBack: () => void; onSignedOut: (message?: string) => void; initialReviewRunId?: string;
}) {
  const [tab, setTab] = useState<Tab>("search");
  const [indexes, setIndexes] = useState<CodeIndexView[]>([]);
  const [targets, setTargets] = useState<IndexTarget[]>([]);
  const [operations, setOperations] = useState<RetrievalOperations | null>(null);
  const [view, setView] = useState<RetrievalSettingsView | null>(null);
  const [draft, setDraft] = useState<RetrievalSettings | null>(null);
  const [reports, setReports] = useState<RetrievalEvaluationReport[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [reviewRunId, setReviewRunId] = useState(initialReviewRunId ?? "");
  const [query, setQuery] = useState("");
  const [seedFiles, setSeedFiles] = useState("");
  const [strategy, setStrategy] = useState<RetrievalStrategy>("lexical_relations");
  const [trace, setTrace] = useState<RetrievalTrace | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const searchController = useRef<AbortController | null>(null);
  const selected = indexes.find(item => item.id === selectedId);

  const handleError = useCallback((failure: unknown) => {
    if (failure instanceof ApiError && failure.status === 401) onSignedOut("登录已过期");
    else setError(errorMessage(failure));
  }, [onSignedOut]);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const [nextIndexes, nextSettings, nextReports, nextTargets, nextOperations] = await Promise.all([
        api.retrievalIndexes(signal), api.retrievalSettings(signal), api.retrievalEvaluations(signal),
        api.retrievalTargets(signal), api.retrievalOperations(signal),
      ]);
      if (signal?.aborted) return;
      setIndexes(nextIndexes); setView(nextSettings); setDraft(nextSettings.settings);
      setTargets(nextTargets); setOperations(nextOperations);
      setReports(nextReports.map(report => report.strategies.some(item => item.strategy !== "bm25" && item.strategy !== "lexical_relations") ? report : {...report, query_cache_mode: "not_used", vector_search_mode: "not_used"}));
      setSelectedId(current => current || nextIndexes.find(item => item.status === "ready")?.id || nextIndexes[0]?.id || "");
    } catch (failure) {
      if (!signal?.aborted) handleError(failure);
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [handleError]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => { controller.abort(); searchController.current?.abort(); };
  }, [refresh]);

  const pending = indexes.some(item => item.status === "queued" || item.status === "building");
  useEffect(() => {
    if (!pending) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const [items, metrics] = await Promise.all([api.retrievalIndexes(controller.signal), api.retrievalOperations(controller.signal)]);
        if (!controller.signal.aborted) {setIndexes(items); setOperations(metrics);}
      } catch (failure) {
        if (!controller.signal.aborted) handleError(failure);
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 5000);
    };
    timer = setTimeout(() => void poll(), 3000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [pending, handleError]);

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

  return <main className="retrieval-page">
    <header className="retrieval-page-header"><Brand /><div><span>{user.username}</span><button onClick={onBack}>返回控制台</button></div></header>
    <div className="retrieval-title"><div><span className="retrieval-eyebrow">OPENREVIEWER / CODE INTELLIGENCE</span><h1>让每一次判断，都有代码依据。</h1><p>版本化索引、跨文件检索与效果对比，汇集到一个工作区。</p></div><button disabled={Boolean(busy)} onClick={() => void refresh()}>刷新数据 ↗</button></div>
    <div className="retrieval-overview" aria-label="检索运行概况">
      <div><span>可用基础索引</span><strong>{operations?.available_indexes ?? "—"}</strong><small>关键词与代码关系</small></div>
      <div><span>正在排队 / 构建</span><strong>{operations?.pending_indexes ?? "—"}</strong><small>最长等待 {operations ? formatDuration(Math.round((operations.oldest_pending_seconds ?? 0) * 1000)) : "—"}</small></div>
      <div><span>模型调用状态</span><strong className="retrieval-overview-status">{view?.external_calls_paused ? "已暂停" : operations?.circuit_open ? "短暂熔断" : operations?.provider_busy ? "处理中" : view ? "按需调用" : "—"}</strong><small>基础检索始终独立运行</small></div>
      <div><span>每次操作请求上限</span><strong>{draft?.max_requests_per_operation ?? "—"}</strong><small>包含失败后的重试</small></div>
    </div>
    <nav className="retrieval-tabs" aria-label="代码检索页面">
      {([["search", "索引与检索"], ["evaluations", "评测对比"], ["settings", "模型配置"]] as const).map(([key, label]) => <button key={key} className={tab === key ? "active" : ""} aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}
    </nav>
    {error && <div role="alert" className="retrieval-error">{error}</div>}
    {view?.external_calls_paused && <div className="retrieval-pause-note"><span aria-hidden="true">Ⅱ</span><div><strong>真实模型调用已暂停</strong><p>可以建立基础索引，使用关键词和代码关系检索。已有向量会保留。</p></div></div>}
    {message && <div role="status" className="retrieval-success">{message}</div>}
    {loading ? <div className="retrieval-empty">正在加载检索数据…</div> : <>
      {tab === "search" && <div className="retrieval-workspace">
        <CodeIndexPanel indexes={indexes} selectedId={selectedId} targets={targets} reviewRunId={reviewRunId} paused={Boolean(view?.external_calls_paused)} busy={Boolean(busy)}
          onSelect={id => {setSelectedId(id); setTrace(null); searchController.current?.abort();}} onTarget={setReviewRunId}
          onBuild={() => void action("index", async () => {const created = await api.createCodeIndex(reviewRunId); setSelectedId(created.id); await refresh(); setMessage("基础索引已提交，不会调用模型");})}
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
              const next = await api.searchCodeIndex(selectedId, {query, strategy, symbols: [], seed_files: seedFiles.split(/\r?\n/).map(value => value.trim()).filter(Boolean), limit: draft?.context_k ?? 8}, controller.signal);
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
            <p>{report.embedding_model} / {report.rerank_model}</p><p className="retrieval-muted">查询缓存：{report.query_cache_mode === "not_used" ? "未使用" : report.query_cache_mode === "shared_warm" ? "统一预热" : report.query_cache_mode} · 向量搜索：{report.vector_search_mode === "not_used" ? "未执行" : report.vector_search_mode === "exact_snapshot" ? "指定提交内精确检索" : report.vector_search_mode}</p>
            <div className="retrieval-table-scroll"><table><thead><tr><th>策略</th><th>样本数</th><th>Recall@K</th><th>MRR</th><th>中位耗时</th><th>P95 耗时</th><th>召回率较基线</th></tr></thead><tbody>
              {report.strategies.map(item => <tr key={item.strategy}><td>{retrievalStrategyLabels[item.strategy]}</td><td>{item.sample_count}</td><td>{(item.recall_at_k * 100).toFixed(1)}% <small>K={item.k}</small></td><td>{item.mrr.toFixed(3)}</td><td>{formatDuration(Math.round(item.median_duration_ms))}</td><td>{formatDuration(Math.round(item.p95_duration_ms))}</td><td>{baseline ? `${item.recall_at_k >= baseline.recall_at_k ? "+" : ""}${((item.recall_at_k - baseline.recall_at_k) * 100).toFixed(1)} 个百分点` : "—"}</td></tr>)}
            </tbody></table></div>
            <p className="retrieval-muted">真实审查准确率：{report.real_review_accuracy == null ? "未评测" : `${(report.real_review_accuracy * 100).toFixed(1)}%`}。标注来源和样本规模是解释结果的必要条件。</p>
          </article>;
        })}
      </section>}
      {tab === "settings" && view && draft && <section className="retrieval-card">
        <h2>检索模型配置</h2><p className="retrieval-muted">向量与精排使用百炼接口。更换向量模型或接入域名后，需要建立对应的新索引。索引在请求前检查新增向量数量，超过上限会停止，已有缓存不计入新增数量。</p>
        <form onSubmit={event => { event.preventDefault(); void action("save", async () => { const next = await api.updateRetrievalSettings(draft, view.revision, apiKey.trim() || undefined); setView(next); setDraft(next.settings); setApiKey(""); setMessage("检索配置已保存"); }); }}>
          <label className="retrieval-checkbox"><input type="checkbox" checked={Boolean(draft.enabled)} onChange={event => setDraft({...draft, enabled: event.target.checked})} />在审查中使用代码上下文</label>
          <label>百炼 API Host<input value={draft.api_host ?? ""} onChange={event => setDraft({...draft, api_host: event.target.value})} placeholder="https://业务空间.cn-beijing.maas.aliyuncs.com" /></label>
          <label>API Key<input type="password" autoComplete="new-password" value={apiKey} onChange={event => setApiKey(event.target.value)} placeholder={view.key_configured ? "已保存；留空保留当前密钥" : "填写百炼 API Key"} /></label>
          <div className="retrieval-form-grid">
            <label>向量模型<input value={draft.embedding_model ?? ""} onChange={event => setDraft({...draft, embedding_model: event.target.value})} /></label>
            <label>重排模型<input value={draft.rerank_model ?? ""} onChange={event => setDraft({...draft, rerank_model: event.target.value})} /></label>
            <label>单个索引最多新增向量数<input type="number" min={0} max={20000} value={draft.max_new_vectors_per_index ?? 100} onChange={event => setDraft({...draft, max_new_vectors_per_index: Number(event.target.value)})} /></label>
            <label>每次操作最多模型请求数<input type="number" min={0} max={300} value={draft.max_requests_per_operation ?? 12} onChange={event => setDraft({...draft, max_requests_per_operation: Number(event.target.value)})} /></label>
            <label>每路候选上限<input type="number" min={1} max={50} value={draft.candidate_k ?? 20} onChange={event => setDraft({...draft, candidate_k: Number(event.target.value)})} /></label>
            <label>上下文数量上限<input type="number" min={1} max={20} value={draft.context_k ?? 8} onChange={event => setDraft({...draft, context_k: Number(event.target.value)})} /></label>
            <label>默认策略<select value={draft.strategy ?? "reranked"} onChange={event => setDraft({...draft, strategy: event.target.value as RetrievalStrategy})}>{Object.entries(retrievalStrategyLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
            <label>接口超时（秒）<input type="number" min={5} max={180} value={draft.timeout_seconds ?? 60} onChange={event => setDraft({...draft, timeout_seconds: Number(event.target.value)})} /></label>
          </div>
          <div className="retrieval-actions"><button className="primary" disabled={Boolean(busy)}>{busy === "save" ? "保存中…" : "保存配置"}</button><button type="button" disabled={Boolean(busy) || !view.key_configured || view.external_calls_paused} onClick={() => void action("test", async () => { const next = await api.testRetrievalSettings(); setView(next); setMessage("向量与精排接口测试通过"); })}>{busy === "test" ? "测试中…" : "测试已保存的连接"}</button><span>{view.tested ? "当前配置已通过连接测试" : "当前配置尚未验证"} · 1024 维</span></div>
        </form>
      </section>}
    </>}
  </main>;
}
