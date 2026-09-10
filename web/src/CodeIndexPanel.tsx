import type { CodeIndexView, IndexTarget } from "./types";
import { formatDate } from "./utils";

const vectorLabels: Record<string, string> = {pending: "待补全", paused: "已暂停", limited: "达到上限", failed: "补全失败", ready: "已就绪"};

export default function CodeIndexPanel({ indexes, selectedId, targets, reviewRunId, paused, busy, onSelect, onTarget, onBuild, onRetry, onEnrich }: {
  indexes: CodeIndexView[]; selectedId: string; targets: IndexTarget[]; reviewRunId: string;
  paused: boolean; busy: boolean; onSelect: (id: string) => void; onTarget: (id: string) => void;
  onBuild: () => void; onRetry: (id: string) => void; onEnrich: (id: string) => void;
}) {
  const selected = indexes.find(item => item.id === selectedId);
  const pending = selected?.status === "queued" || selected?.status === "building";
  return <aside className="retrieval-card retrieval-library">
    <div className="retrieval-section-label">01 / 代码来源</div>
    <h2>仓库与版本</h2>
    <p className="retrieval-muted">每份索引固定到一次提交，证据始终可追溯。</p>
    <form onSubmit={event => { event.preventDefault(); onBuild(); }}>
      <label>选择仓库与 PR<select value={reviewRunId} onChange={event => onTarget(event.target.value)}>
        <option value="">选择最近审查的提交</option>
        {reviewRunId && !targets.some(item => item.review_run_id === reviewRunId) && <option value={reviewRunId}>当前审查提交</option>}
        {targets.map(item => <option key={item.review_run_id} value={item.review_run_id}>{item.repository} · PR #{item.pull_request_number} · {item.head_sha.slice(0, 7)}</option>)}
      </select></label>
      <button className="primary retrieval-full-button" disabled={busy || !reviewRunId}>建立基础索引</button>
      <p className="retrieval-form-hint">只解析代码，基础索引不调用模型。</p>
    </form>
    <div className="retrieval-library-divider" />
    <label>选择索引<select value={selectedId} onChange={event => onSelect(event.target.value)}>
      <option value="">请选择索引</option>
      {indexes.map(item => <option value={item.id} key={item.id}>{item.repository} · {item.head_sha.slice(0, 7)}</option>)}
    </select></label>
    {selected ? <>
      <div className="retrieval-repository-name"><span className="retrieval-repository-icon" aria-hidden="true">⌘</span><div><strong>{selected.repository}</strong><code>{selected.head_sha.slice(0, 12)}</code></div></div>
      <ol className="retrieval-stages" aria-label="索引阶段">
        <li className={selected.lexical_ready ? "done" : pending ? "working" : ""}><span className="retrieval-stage-dot" /><div><strong>源码与关系</strong><small>{selected.lexical_ready ? "基础检索可用" : pending ? "正在读取和解析" : "等待建立索引"}</small></div></li>
        <li className={selected.vector_status === "ready" ? "done" : ""}><span className="retrieval-stage-dot" /><div><strong>向量补全</strong><small>{vectorLabels[selected.vector_status ?? "pending"]} · {selected.vector_count ?? 0} / {selected.chunk_count} 块</small></div></li>
        <li><span className="retrieval-stage-dot" /><div><strong>检索时精排</strong><small>{paused ? "模型调用已暂停" : "按需执行，失败保留基础结果"}</small></div></li>
      </ol>
      <div className="retrieval-source-counts"><div><strong>{selected.file_count.toLocaleString()}</strong><span>文件</span></div><div><strong>{selected.chunk_count.toLocaleString()}</strong><span>代码块</span></div><div><strong>{selected.relation_count.toLocaleString()}</strong><span>关系</span></div></div>
      <p className="retrieval-form-hint">解析 {selected.parsed_files ?? 0} · 复用 {selected.reused_files ?? 0} · {formatDate(selected.created_at)}</p>
      {(selected.error || selected.vector_error) && <div className="retrieval-warning">{selected.error || selected.vector_error}</div>}
      {!selected.lexical_ready && !pending && <button className="retrieval-full-button" disabled={busy} onClick={() => onRetry(selected.id)}>恢复基础索引</button>}
      {selected.lexical_ready && selected.vector_status !== "ready" && <button className="retrieval-full-button" disabled={busy || paused || pending} onClick={() => onEnrich(selected.id)}>补全缺失向量</button>}
      {(selected.parse_error_files?.length ?? 0) > 0 && <details><summary>查看解析提示</summary>{selected.parse_error_files?.map(file => <p className="retrieval-form-hint" key={file}>{file}</p>)}</details>}
    </> : <div className="retrieval-empty"><span className="retrieval-empty-icon" aria-hidden="true">⌘</span><p>建立索引后即可检索对应提交的代码。</p></div>}
  </aside>;
}
