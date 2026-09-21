import { Button } from "./components/ui/button";
import { useRef } from "react";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "./components/ui/dialog";
import SnapshotProfilePicker from "./SnapshotProfilePicker";

export default function SnapshotReviewDialog({ open, onOpenChange, repository, canChooseProfile, profileId, onProfileChange, capture, onCaptureChange, busy, onError, onConfirm }: {
  open: boolean; onOpenChange: (open: boolean) => void; repository: string;
  canChooseProfile: boolean; profileId: string; onProfileChange: (id: string) => void;
  capture: boolean; onCaptureChange: (value: boolean) => void; busy: boolean;
  onError: (error: unknown) => void; onConfirm: () => void;
}) {
  const returnFocus = useRef<HTMLElement | null>(null);
  return <Dialog open={open} onOpenChange={value => { if (!busy) onOpenChange(value); }}>
    <DialogContent className="snapshot-review-dialog sm:max-w-xl" onOpenAutoFocus={() => { returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; }}
      onCloseAutoFocus={event => { if (returnFocus.current?.isConnected) { event.preventDefault(); returnFocus.current.focus(); } }}
      onInteractOutside={event => { if (busy) event.preventDefault(); }} onEscapeKeyDown={event => { if (busy) event.preventDefault(); }} showCloseButton={!busy}>
      <DialogHeader><DialogTitle>复查此版本</DialogTitle><DialogDescription>使用已保存的代码重新调用模型，结果另存为一条审查记录。</DialogDescription></DialogHeader>
      <div className="space-y-5 py-2">
        {canChooseProfile && <SnapshotProfilePicker repository={repository} selected={profileId} onSelected={onProfileChange} disabled={busy} onError={onError} />}
        <label className="flex items-start gap-3 rounded-md border p-4 text-sm leading-6">
          <input type="checkbox" className="mt-1 size-4 shrink-0 accent-primary" checked={capture} disabled={busy} onChange={event => onCaptureChange(event.target.checked)} />
          <span>留存本次评测输出<small className="block text-muted-foreground">保存 30 天，可能包含业务代码，仅在需要核对模型原始答案时启用。</small></span>
        </label>
        <p className="rounded-md bg-muted p-3 text-sm text-muted-foreground">本次会产生模型调用费用。不会重新运行 CI 或发布到 GitHub，原任务与仓库默认方案保持不变。</p>
      </div>
      <DialogFooter><Button variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>取消</Button><Button disabled={busy} onClick={onConfirm}>{busy ? "正在创建…" : "确认并开始复查"}</Button></DialogFooter>
    </DialogContent>
  </Dialog>;
}
