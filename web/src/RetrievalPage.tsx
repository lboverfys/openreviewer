import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { Brand } from "./Auth";
import RetrievalTracePanel, { retrievalStrategyLabels } from "./RetrievalTracePanel";
import { formatDuration } from "./review-details";
import type { AuthUser, CodeIndexView, RetrievalEvaluationReport, RetrievalSettings, RetrievalSettingsView, RetrievalStrategy, RetrievalTrace } from "./types";
import { errorMessage, formatDate } from "./utils";
import "./styles/retrieval.css";

type Tab = "search" | "evaluations" | "settings";
const statusLabels: Record<string, string> = { queued: "等待构建", building: "构建中", ready: "可检索", failed: "构建失败" };
const annotationLabels: Record<string, string> = { synthetic_contract: "合成契约样本", agent_annotated: "代理标注 · 非独立人工金标", independent_human: "独立人工标注" };

export default function RetrievalPage({ user, onBack, onSignedOut, initialReviewRunId }: {
  user: AuthUser; onBack: () => void; onSignedOut: (message?: string) => void; initialReviewRunId?: string;
}) {
  const [tab, setTab] = useState<Tab>("search");
  const [indexes, setIndexes] = useState<CodeIndexView[]>([]);
  const [view, setView] = useState<RetrievalSettingsView | null>(null);
  const [draft, setDraft] = useState<RetrievalSettings | null>(null);
  const [reports, setReports] = useState<RetrievalEvaluationReport[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [reviewRunId, setReviewRunId] = useState(initialReviewRunId ?? "");
  const [query, setQuery] = useState("");
  const [seedFiles, setSeedFiles] = useState("");
  const [strategy, setStrategy] = useState<RetrievalStrategy>("reranked");
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
      const [nextIndexes, nextSettings, nextReports] = await Promise.all([
        api.retrievalIndexes(signal), api.retrievalSettings(signal), api.retrievalEvaluations(signal),
      ]);
      if (signal?.aborted) return;
      setIndexes(nextIndexes); setView(nextSettings); setDraft(nextSettings.settings);
      setReports(nextReports.map(report => report.strategies.some(item => item.strategy !== "bm25") ? report : {...report, query_cache_mode: "not_used", vector_search_mode: "not_used"}));
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
        const items = await api.retrievalIndexes(controller.signal);
        if (!controller.signal.aborted) setIndexes(items);
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
    <div className="retrieval-title"><div><span className="retrieval-eyebrow">CODE RETRIEVAL</span><h1>代码检索与评测</h1><p>查看索引版本、检索来源与排序过程，用固定样本比较策略效果。</p></div><button disabled={Boolean(busy)} onClick={() => void refresh()}>刷新数据</button></div>
    <nav className="retrieval-tabs" aria-label="代码检索页面">
      {([["search", "索引与检索"], ["evaluations", "评测对比"], ["settings", "模型配置"]] as const).map(([key, label]) => <button key={key} className={tab === key ? "active" : ""} aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}
    </nav>
    {error && <div role="alert" className="retrieval-error">{error}</div>}
    {view?.external_calls_paused && <div className="retrieval-warning">服务器已暂停真实向量与精排请求。当前可查看索引、历史评测和运行 BM25 检索。</div>}
    {message && <div role="status" className="retrieval-success">{message}</div>}
    {loading ? <div className="retrieval-empty">正在加载检索数据…</div> : <>
      {tab === "search" && <>
        <section className="retrieval-card">
          <h2>版本化代码索引</h2>
          <form className="retrieval-create-form" onSubmit={event => {
            event.preventDefault();
            void action("index", async () => {
              const created = await api.createCodeIndex(reviewRunId.trim());
              setIndexes(current => [created, ...current.filter(item => item.id !== created.id)]);
              setSelectedId(created.id); setMessage(created.status === "ready" ? "该提交已有可用索引" : "索引已进入后台构建队列");
            });
          }}>
            <label>审查运行 ID<input value={reviewRunId} onChange={event => setReviewRunId(event.target.value)} placeholder="从审查详情页进入，或填写运行 ID" /></label>
            <button className="primary" disabled={Boolean(busy) || !reviewRunId.trim() || view?.external_calls_paused}>{busy === "index" ? "提交中…" : "为此提交建立索引"}</button>
          </form>
          <label className="retrieval-index-picker">选择索引<select value={selectedId} onChange={event => { setSelectedId(event.target.value); setTrace(null); searchController.current?.abort(); }}>
            <option value="">请选择索引</option>
            {indexes.map(item => <option value={item.id} key={item.id}>{item.repository} · {item.head_sha.slice(0, 10)} · {statusLabels[item.status] ?? item.status}</option>)}
          </select></label>
          {selected ? <div className="retrieval-index-info">
            <div className="retrieval-index-heading"><strong>{selected.repository}</strong><span className={`retrieval-tag ${selected.status === "ready" ? "selected" : ""}`}>{statusLabels[selected.status] ?? selected.status}</span><code>{selected.head_sha.slice(0, 12)}</code></div>
            <div className="retrieval-index-metrics">
              <div><span>源文件</span><strong>{selected.file_count.toLocaleString()}</strong></div>
              <div><span>代码块</span><strong>{selected.chunk_count.toLocaleString()}</strong></div>
              <div><span>静态关系</span><strong>{selected.status === "ready" ? selected.relation_count.toLocaleString() : "未完成"}</strong></div>
              <div><span>新增 / 复用向量</span><strong>{selected.embedded_count} / {selected.reused_count}</strong></div>
              <div><span>构建耗时</span><strong>{formatDuration(selected.status === "ready" ? selected.duration_ms : null)}</strong></div>
            </div>
            {selected.status === "ready" ? <p>本次解析 {selected.parsed_files ?? 0} 个文件 · 复用解析 {selected.reused_files ?? 0} 个文件</p> : <p>索引完成后提供解析复用统计。</p>}
            <p>{selected.embedding_model} · {selected.dimensions} 维 · 创建于 {formatDate(selected.created_at)}</p>
            {(selected.parse_error_files ?? []).length > 0 && <details><summary>有 {selected.parse_error_files?.length} 个文件需要核对解析结果</summary><ul>{selected.parse_error_files?.map(path => <li key={path}>{path}</li>)}</ul></details>}
            {selected.error && <p className="retrieval-warning">{selected.error}</p>}
            {selected.status === "failed" && <button disabled={Boolean(busy) || view?.external_calls_paused} onClick={() => void action("retry", async () => { await api.retryCodeIndex(selected.id); setIndexes(current => current.map(item => item.id === selected.id ? {...item, status: "queued", error: null} : item)); })}>重试索引</button>}
          </div> : <p className="retrieval-empty">建立索引后即可检索对应提交的代码。</p>}
        </section>
        <section className="retrieval-card">
          <h2>检索实验</h2>
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
            <button className="primary" disabled={Boolean(busy) || selected?.status !== "ready" || !query.trim() || (Boolean(view?.external_calls_paused) && strategy !== "bm25")}>{busy === "search" ? "正在召回与排序…" : "执行检索"}</button>
          </form>
          {trace?.index_id === selectedId && <RetrievalTracePanel traces={[trace]} />}
        </section>
      </>}
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
          <label className="retrieval-checkbox"><input type="checkbox" checked={Boolean(draft.enabled)} onChange={event => setDraft({...draft, enabled: event.target.checked})} />启用审查中的混合检索</label>
          <label>百炼 API Host<input value={draft.api_host ?? ""} onChange={event => setDraft({...draft, api_host: event.target.value})} placeholder="https://业务空间.cn-beijing.maas.aliyuncs.com" /></label>
          <label>API Key<input type="password" autoComplete="new-password" value={apiKey} onChange={event => setApiKey(event.target.value)} placeholder={view.key_configured ? "已保存；留空保留当前密钥" : "填写百炼 API Key"} /></label>
          <div className="retrieval-form-grid">
            <label>向量模型<input value={draft.embedding_model ?? ""} onChange={event => setDraft({...draft, embedding_model: event.target.value})} /></label>
            <label>重排模型<input value={draft.rerank_model ?? ""} onChange={event => setDraft({...draft, rerank_model: event.target.value})} /></label>
            <label>单个索引最多新增向量数<input type="number" min={0} max={20000} value={draft.max_new_vectors_per_index ?? 100} onChange={event => setDraft({...draft, max_new_vectors_per_index: Number(event.target.value)})} /></label>
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
