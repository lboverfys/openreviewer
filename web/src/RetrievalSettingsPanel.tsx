import { Input } from "./components/ui/input";
import { NativeSelect } from "./components/ui/native-select";
import { Button } from "./components/ui/button";
import { Notice } from "./Feedback";
import { ChevronDown, Settings2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "./api";
import type { RetrievalSettings, RetrievalSettingsView, RetrievalStrategy } from "./types";
import { retrievalStrategyLabels } from "./RetrievalTracePanel";
import { errorMessage } from "./utils";

export default function RetrievalSettingsPanel({ onError }: { onError: (error: unknown) => void }) {
  const [view, setView] = useState<RetrievalSettingsView | null>(null);
  const [draft, setDraft] = useState<RetrievalSettings | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [limitsOpen, setLimitsOpen] = useState(false);
  const normalize = (value: RetrievalSettingsView) => ({...value.settings,
    external_calls_enabled: value.settings.external_calls_enabled ?? !value.external_calls_paused});
  useEffect(() => {
    const controller = new AbortController();
    api.retrievalSettings(controller.signal, true).then(value => {
      if (!controller.signal.aborted) {setView(value); setDraft(normalize(value));}
    }).catch(error => {if (!controller.signal.aborted) onError(error);});
    return () => controller.abort();
  }, [onError]);
  if (!view || !draft) return <p role="status">正在读取代码检索配置…</p>;
  const dirty = Boolean(apiKey) || JSON.stringify(draft) !== JSON.stringify(normalize(view));
  const update = <K extends keyof RetrievalSettings>(key: K, value: RetrievalSettings[K]) => setDraft({...draft, [key]: value});
  async function save(event: React.FormEvent) {
    event.preventDefault(); if (!view || !draft) return;
    setBusy("save"); setMessage("");
    try {
      const value = await api.updateRetrievalSettings(draft, view.revision, apiKey.trim() || undefined);
      setView(value); setDraft(normalize(value)); setApiKey(""); setMessage("已保存，下一次检索操作使用新配置。");
    } catch (error) {onError(error);} finally {setBusy("");}
  }
  async function test() {
    setBusy("test"); setMessage("");
    try {const value = await api.testRetrievalSettings(); setView(value); setMessage("向量与精排连接均已通过真实请求验证。");}
    catch (error) {onError(new Error(errorMessage(error)));} finally {setBusy("");}
  }
  return <section className="workspace-surface settings-retrieval" aria-label="代码上下文配置">
    <div className="ws-editor-heading"><div><h2>代码上下文</h2><p>审查前自动查找同一提交的关联代码，帮助 AI 理解跨文件调用和 SQL。</p></div><a href="#retrieval">查看索引与检索结果 →</a></div>
    <form onSubmit={save}><fieldset disabled={Boolean(busy)} className="settings-fieldset">
      <div className="feature-switches">
        <label><input type="checkbox" checked={Boolean(draft.enabled)} onChange={e => update("enabled", e.target.checked)} /><span><strong>审查时自动补充关联代码</strong><small>开启后，Worker 自动准备索引并把相关片段送给审查 Agent。</small></span></label>
        <label><input type="checkbox" role="switch" checked={Boolean(draft.external_calls_enabled)} onChange={e => update("external_calls_enabled", e.target.checked)} /><span><strong>允许向量与精排模型调用</strong><small>开启后允许发送代码片段到下方百炼服务并产生费用；关闭时继续使用关键词和代码关系检索，审查仍可继续，关联代码检索可能降级。</small></span></label>
      </div>
      <div className="ws-form-grid">
        <label>百炼服务地址<Input value={draft.api_host} onChange={e => update("api_host", e.target.value)} placeholder="https://dashscope.aliyuncs.com" /></label>
        <label>百炼密钥<Input type="password" autoComplete="new-password" value={apiKey} onChange={e => setApiKey(e.target.value)} placeholder={view.key_configured ? "已保存，留空保留" : "填写 API Key"} /></label>
        <label>向量模型<Input value={draft.embedding_model} onChange={e => update("embedding_model", e.target.value)} /></label>
        <label>精排模型<Input value={draft.rerank_model} onChange={e => update("rerank_model", e.target.value)} /></label>
        <label>检索方式<NativeSelect value={draft.strategy} onChange={e => update("strategy", e.target.value as RetrievalStrategy)}>{Object.entries(retrievalStrategyLabels).map(([key,label]) => <option key={key} value={key}>{label}</option>)}</NativeSelect></label>
      </div>
      <section className="retrieval-index-settings" aria-label="索引与向量补全">
        <h3>索引与向量补全</h3>
        <p>独立索引 Worker 分轮补全，已有向量直接复用。每轮读取已保存配置；达到累计请求上限后停止，保留已完成部分。</p>
        <div className="ws-form-grid">
          <label>每轮最多新增向量数<Input type="number" required min="0" max="20000" step="1" value={draft.max_new_vectors_per_index} onChange={e => update("max_new_vectors_per_index", Number(e.target.value))} /><small>0 表示不新增向量；未补齐且额度仍有剩余时自动继续下一轮。</small></label>
          <label>单次任务模型请求上限<Input type="number" required min="0" max="300" step="1" value={draft.max_requests_per_operation} onChange={e => update("max_requests_per_operation", Number(e.target.value))} /><small>补全任务跨轮累计，不因自动续补或错误重试而重置；检索操作单独计数。</small></label>
          <label>单次向量请求文本上限（KB）<Input type="number" required min="32" max="512" step="1" value={(draft.embedding_batch_max_bytes ?? 64000) / 1000} onChange={e => update("embedding_batch_max_bytes", Number(e.target.value) * 1000)} /><small>32–512 KB，1 KB = 1000 字节。每次最多 20 段；按数量或文本上限分批。调整不会重算已有向量。</small></label>
        </div>
        {draft.max_new_vectors_per_index > draft.max_requests_per_operation * 20 && <p className="retrieval-warning" role="status">新增 {draft.max_new_vectors_per_index} 个向量至少需要 {Math.ceil(draft.max_new_vectors_per_index / 20)} 次请求，当前上限为 {draft.max_requests_per_operation} 次，可能提前停止。文本大小也会影响实际请求数。</p>}
      </section>
      <Button variant="outline" type="button" className="retrieval-settings-toggle" aria-expanded={limitsOpen} aria-controls="retrieval-extra-settings" onClick={() => setLimitsOpen(value => !value)}><Settings2 aria-hidden="true" size={20}/><span>请求限额、索引与费用</span><strong>{limitsOpen ? "收起设置" : "展开设置"}</strong><ChevronDown aria-hidden="true" size={20} className={limitsOpen ? "is-expanded" : ""}/></Button>
      {limitsOpen && <div id="retrieval-extra-settings" className="ws-disclosure-body">
        <p>关键词和代码关系无需额外模型调用；向量负责找语义相近代码，精排负责从候选里挑更相关的内容。已有向量缓存会复用。</p>
        <p>百炼公开参考价（2026-09-13，北京地域）：qwen3.7-text-embedding / qwen3.7-text-rerank 均为 ¥0.50 / 百万输入 Token。按 2026-09-11 参考汇率 1 美元 = 6.7082 元折算为 $0.074536；这是估算，不是服务商账单。<a href="https://help.aliyun.com/zh/model-studio/model-pricing" target="_blank" rel="noreferrer">原始报价</a> · <a href="https://api.frankfurter.dev/v1/2026-09-11?base=USD&symbols=CNY" target="_blank" rel="noreferrer">汇率来源</a></p>
        <Button variant="outline" type="button" disabled={draft.embedding_model !== "qwen3.7-text-embedding" || draft.rerank_model !== "qwen3.7-text-rerank"} onClick={() => setDraft({...draft, embedding_usd_per_million: "0.074536", rerank_usd_per_million: "0.074536"})}>填入百炼参考折算价</Button>
        <div className="ws-form-grid">
          <label>向量单价（美元 / 百万 Token）<Input type="number" min="0" max="1000000" step="any" value={draft.embedding_usd_per_million ?? ""} onChange={e => update("embedding_usd_per_million", e.target.value || null)} /></label>
          <label>精排单价（美元 / 百万 Token）<Input type="number" min="0" max="1000000" step="any" value={draft.rerank_usd_per_million ?? ""} onChange={e => update("rerank_usd_per_million", e.target.value || null)} /></label>
          <label>每路候选上限<Input type="number" min="1" max="50" value={draft.candidate_k} onChange={e => update("candidate_k", Number(e.target.value))} /></label>
          <label>送入审查的片段上限<Input type="number" min="1" max="20" value={draft.context_k} onChange={e => update("context_k", Number(e.target.value))} /></label>
          <label>关联代码正文上限（KB）<Input type="number" required min="4" max="256" step="1" value={(draft.context_max_bytes ?? 24000) / 1000} onChange={e => update("context_max_bytes", Number(e.target.value) * 1000)} /><small>4–256 KB，每个 Agent 的额外参考正文总量。与片段数同时限制；模型容量不足时自动少选片段，保留变更代码。</small></label>
          <label>接口超时（秒）<Input type="number" min="5" max="180" value={draft.timeout_seconds} onChange={e => update("timeout_seconds", Number(e.target.value))} /></label>
        </div>
      </div>}
      <div className="ws-form-actions"><Button variant="default" className="settings-primary-btn" type="submit" disabled={!dirty}>{busy === "save" ? "保存中…" : "保存检索配置"}</Button><Button variant="outline" type="button" disabled={dirty || !view.key_configured || view.external_calls_paused} onClick={() => void test()}>{busy === "test" ? "测试中…" : "测试已保存的连接"}</Button><span>{view.external_calls_paused ? "向量与精排：已关闭" : "向量与精排：允许调用"} · {view.tested ? "连接已验证" : "连接待验证"}</span></div>
      {message && <Notice kind="success" onDismiss={() => setMessage("")}>{message}</Notice>}
    </fieldset></form>
  </section>;
}
