import { useCallback, useEffect, useState } from "react";
import { Check } from "lucide-react";
import { api } from "./api";
import { Button } from "./components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "./components/ui/dialog";
import Pagination from "./Pagination";
import { fileDecisionLabels } from "./ReviewSidebarPanels";
import type { ReviewDetails } from "./types";
import { useCursorPage } from "./useCursorPage";
import { errorMessage } from "./utils";

export default function CoverageFilesDialog({open, onOpenChange, details, canApprove, busy, submitError, onConfirm}: {
  open: boolean; onOpenChange: (open: boolean) => void; details: ReviewDetails;
  canApprove: boolean; busy: boolean; submitError: string; onConfirm: () => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const [loadError, setLoadError] = useState("");
  useEffect(() => {setConfirmed(false);}, [open, details.change_token]);
  const load = useCallback((cursor?: string, signal?: AbortSignal) => {
    setLoadError("");
    return api.excludedFilePage(details.review_run_id, details.review_plan_id!, cursor, signal);
  }, [details.review_run_id, details.review_plan_id]);
  const onError = useCallback((error: unknown) => setLoadError(errorMessage(error)), []);
  const page = useCursorPage<ReviewDetails["excluded_file_examples"][number]>({
    cacheKey:`excluded-files:${details.review_run_id}:${details.review_plan_id}`, load, onError, enabled:open,
  });
  const count = Object.entries(details.plan_file_decisions).reduce((sum, [decision, value]) => sum + (decision === "planned" ? 0 : value), 0);
  return <Dialog open={open} onOpenChange={value => {if (!busy) onOpenChange(value);}}>
    <DialogContent className="sm:max-w-2xl max-h-[85vh] overflow-y-auto" showCloseButton={!busy}
      onInteractOutside={event => {if (busy) event.preventDefault();}} onEscapeKeyDown={event => {if (busy) event.preventDefault();}}>
      <DialogHeader><DialogTitle>确认审查范围</DialogTitle><DialogDescription>共有 {count} 个文件未经过 AI 文本审查。</DialogDescription></DialogHeader>
      <div className="space-y-3 min-w-0">
        {(loadError || submitError) && <p role="alert" className="text-sm text-destructive">{loadError || submitError}</p>}
        {loadError && <Button variant="outline" disabled={page.loading} onClick={() => void page.refresh()}>重试读取文件</Button>}
        <ul className="divide-y text-sm" aria-label="未审查文件">
          {(page.data?.items ?? []).map(item => <li key={item.file} className="py-2 break-all">
            <a href={`https://github.com/${details.repository}/blob/${details.head_sha}/${item.file.split("/").map(encodeURIComponent).join("/")}`} target="_blank" rel="noreferrer">{item.file}</a>
            <span className="block text-muted-foreground">{fileDecisionLabels[item.decision] ?? item.decision}</span>
          </li>)}
        </ul>
        {page.loading && <p role="status">正在读取文件</p>}
        <Pagination page={page.page} count={page.data?.items.length ?? 0} total={count} hasNext={Boolean(page.data?.next_cursor)} busy={page.loading}
          onPrevious={page.previous} onNext={page.next} label="未审查文件分页" />
        {canApprove && <label className="flex gap-3 text-sm leading-6">
          <input type="checkbox" className="mt-1 size-4 shrink-0" checked={confirmed} disabled={busy || page.loading || Boolean(loadError)} onChange={event => setConfirmed(event.target.checked)} />
          <span>我已人工核对这些二进制或生成文件，确认批准已审查的文本范围。</span>
        </label>}
      </div>
      <DialogFooter><Button variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>关闭</Button>
        {canApprove && <Button disabled={!confirmed || !page.data || page.loading || busy || Boolean(loadError)} onClick={onConfirm}><Check aria-hidden="true" />{busy ? "正在批准" : "确认范围并批准"}</Button>}
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}
