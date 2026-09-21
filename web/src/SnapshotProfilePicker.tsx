import { NativeSelect } from "./components/ui/native-select";
import { useCallback } from "react";
import Pagination from "./Pagination";
import { platformApi } from "./platform-api";
import { useCursorPage } from "./useCursorPage";

export default function SnapshotProfilePicker({repository, selected, onSelected, onError, disabled}: {
  repository: string; selected: string; onSelected: (id: string) => void; onError: (error: unknown) => void; disabled: boolean;
}) {
  const load = useCallback((cursor?: string, signal?: AbortSignal, force?: boolean) => platformApi.profiles(repository, cursor, signal, force), [repository]);
  const page = useCursorPage({cacheKey:"snapshot-profiles:" + repository,load,onError});
  return <div className="snapshot-profile-picker space-y-2"><label className="grid gap-2 text-sm font-medium">本次试跑方案<NativeSelect aria-label="本次试跑方案" disabled={disabled || page.loading} value={selected} onChange={event => onSelected(event.target.value)}>
    <option value="">仓库当前方案</option>
    {selected && !page.data?.items.some(item => item.id === selected) && <option value={selected}>已选方案 {selected.slice(0,8)}</option>}
    {page.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.prompt_content_sha256?.slice(0,8) ?? item.fingerprint.slice(0,8)}</option>)}
  </NativeSelect></label>
    <small className="block text-muted-foreground">仅用于本次固定提交的复查，关闭结果复用。</small>
    {(page.page > 1 || page.data?.next_cursor) && <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={disabled || page.loading}
      onPrevious={page.previous} onNext={page.next} label="试跑方案分页"/>}
  </div>;
}
