import { useCallback, useEffect, useRef } from "react";
import { api } from "./api";
import Pagination from "./Pagination";
import type { EvaluationRunOption } from "./types";
import { useCursorPage } from "./useCursorPage";
import { formatDate, shortSha } from "./utils";
import { WorkspaceBadge, WorkspaceEmpty } from "./Workspace";

export default function EvaluationSourcePicker({ datasetId, caseId, selected, onSelected, onError, disabled = false, excludeRunId, single = false }: {
  datasetId?: string; caseId?: string; selected: string[]; onSelected: (ids: string[]) => void;
  onError: (error: unknown) => void; disabled?: boolean; excludeRunId?: string; single?: boolean;
}) {
  const known = useRef(new Map<string, EvaluationRunOption>());
  useEffect(() => {
    for (const id of known.current.keys()) if (!selected.includes(id)) known.current.delete(id);
  }, [selected]);
  const load = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) =>
    api.evaluationSources(datasetId, caseId, cursor, signal, force), [datasetId, caseId]);
  const page = useCursorPage({cacheKey:"evaluation-sources:" + (datasetId ?? "all") + ":" + (caseId ?? "all"), load, onError});
  function toggle(item: EvaluationRunOption) {
    known.current.set(item.review_run_id, item);
    if (selected.includes(item.review_run_id)) {
      onSelected(selected.filter(id => id !== item.review_run_id));
      return;
    }
    if (single) { onSelected([item.review_run_id]); return; }
    const remaining = selected.filter(id => {
      const previous = known.current.get(id);
      return !previous || previous.repository !== item.repository || previous.pull_request_number !== item.pull_request_number;
    });
    if (remaining.length >= 30) { onError(new Error("每批最多选择 30 条运行")); return; }
    onSelected([...remaining, item.review_run_id]);
  }
  return <section className="evaluation-picker" aria-label="选择审查运行">
    <div className="evaluation-toolbar"><strong>选择已完成的审查运行</strong><WorkspaceBadge tone="accent">已选择 {selected.length} 条</WorkspaceBadge>
      <button type="button" disabled={disabled || !selected.length} onClick={() => onSelected([])}>清空选择</button></div>
    <p className="evaluation-hint">仅显示覆盖完整的审查。同一 PR 在一个分组中选择一条运行；候选必须与基线使用同一提交。</p>
    <div className="evaluation-table-wrap"><table>
      <thead><tr><th>选择</th><th>仓库 / PR</th><th>提交</th><th>模型 / 问题数</th><th>时间</th></tr></thead>
      <tbody>{page.data?.items.map(item => <tr key={item.review_run_id} className={selected.includes(item.review_run_id) ? "is-selected" : undefined}>
        <td><input type="checkbox" aria-label={"选择 PR #" + item.pull_request_number + " 运行 " + item.review_run_id}
          checked={selected.includes(item.review_run_id)} disabled={disabled || item.review_run_id === excludeRunId} onChange={() => toggle(item)} /></td>
        <td><strong>{item.repository} · PR #{item.pull_request_number}</strong><small>{item.title || "未记录标题"}</small></td>
        <td><code title={item.head_sha}>{shortSha(item.head_sha)}</code><small title={item.review_run_id}>{item.review_run_id.slice(0, 8)}</small></td>
        <td>{item.model}<small>{item.finding_count} 条问题</small></td>
        <td>{formatDate(item.completed_at)}</td>
      </tr>)}</tbody>
    </table></div>
    {!page.loading && page.data?.items.length === 0 && <WorkspaceEmpty title="暂无符合条件的运行" description="完成审查后收录；候选结果需要使用与基线相同的提交。" />}
    <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)}
      busy={disabled || page.loading} onPrevious={page.previous} onNext={page.next} label="审查运行分页" />
  </section>;
}
