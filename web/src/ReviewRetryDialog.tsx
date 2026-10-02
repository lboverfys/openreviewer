import { useRef } from "react";
import { Button } from "./components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "./components/ui/dialog";
import { NativeSelect } from "./components/ui/native-select";
import { agentDefinitions } from "./review-details";
import type { ReviewDetails } from "./types";

export type RetryTargetStage = "ci" | "planning" | "agent_batches" | "aggregating";

export const retryTargetOptions: ReadonlyArray<[RetryTargetStage, string]> = [
  ["ci", "CI 检查"], ["planning", "审查规划"],
  ["agent_batches", "全部 AI 审查"], ["aggregating", "仅重新汇总"],
];

export function retryStageNotice(stage: RetryTargetStage): string {
  switch (stage) {
    case "ci": return "重新检查 CI，并重新生成审查规划、AI 审查和汇总结果。";
    case "planning": return "保留 CI 结果，重新生成审查规划、AI 审查和汇总结果。";
    case "agent_batches": return "保留 CI 和审查规划，重新执行安全、规范、逻辑审查，再生成汇总。";
    case "aggregating": return "保留安全、规范、逻辑审查的成功批次，只重新生成汇总与问题列表。";
  }
}

export default function ReviewRetryDialog({ open, onClose, details, stage, onStageChange, agent, stageRetry, busy, error, onConfirm }: {
  open: boolean; onClose: () => void; details: ReviewDetails; stage: RetryTargetStage;
  onStageChange: (stage: RetryTargetStage) => void; agent?: string; stageRetry: boolean;
  busy: boolean; error: string; onConfirm: () => void;
}) {
  const returnFocus = useRef<HTMLElement | null>(null);
  const impacts = Object.values(details.retry_impacts ?? {}).filter(item => item.restart && (!agent || item.agent === agent));
  const agentName = (key: string) => agentDefinitions.find(item => item.key === key)?.label ?? key;
  return <Dialog open={open} onOpenChange={value => { if (!value && !busy) onClose(); }}>
    <DialogContent className="review-retry-dialog w-[calc(100%-2rem)] max-w-[560px] sm:max-w-[560px] gap-[22px] rounded-2xl p-7 max-[480px]:gap-[18px] max-[480px]:px-5 max-[480px]:py-[22px] motion-reduce:animate-none" showCloseButton={!busy}
      onOpenAutoFocus={() => { returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; }}
      onCloseAutoFocus={event => { if (returnFocus.current?.isConnected) { event.preventDefault(); returnFocus.current.focus(); } }}
      onInteractOutside={event => { if (busy) event.preventDefault(); }} onEscapeKeyDown={event => { if (busy) event.preventDefault(); }}>
      <DialogHeader className="gap-2.5 pr-5 text-left"><DialogTitle>{stageRetry ? "重新执行审查" : "重新执行失败节点"}</DialogTitle>
        <DialogDescription>使用当前已启用的配置。本轮执行期间保持配置一致。</DialogDescription></DialogHeader>
      {stageRetry ? <>
        <label className="review-retry-field"><span>从哪一步开始</span>
          <NativeSelect value={stage} disabled={busy} onChange={event => onStageChange(event.target.value as RetryTargetStage)}>
            {retryTargetOptions.filter(([value]) => !details.snapshot_review || value !== "ci").map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </NativeSelect>
        </label>
        <div className="review-retry-scope"><strong>本次执行范围</strong><p>{retryStageNotice(stage)}</p></div>
      </> : <div className="review-retry-scope"><strong>需要重跑的节点</strong>
        {impacts.map(item => <div className="review-retry-impact" key={item.agent}>
          <span>{agentName(item.agent)}<b>{item.batch_count} 批全部重跑</b></span>
          <p>{item.previous_models.length > 0 && item.previous_models.some(model => model !== item.model)
            ? `模型已变更：${item.previous_models.join("、")} → ${item.model}`
            : `使用 ${item.model} 重新生成结果，避免复用不兼容的配置或合并失败的批次。`}</p>
        </div>)}
        <p>保留其他已完成的审查节点、CI 和审查规划，完成后重新汇总。</p>
      </div>}
      <p className="review-retry-cost">重新调用模型会消耗 Token，已完成的调用仍计入用量。任务身份、提交版本和审计记录保留。</p>
      {error && <p className="review-retry-error" role="alert">{error}</p>}
      <DialogFooter className="flex-row justify-end gap-2.5 pt-1"><Button variant="outline" disabled={busy} onClick={onClose}>取消</Button>
        <Button disabled={busy} onClick={onConfirm}>{busy ? "正在提交…" : "确认重新执行"}</Button></DialogFooter>
    </DialogContent>
  </Dialog>;
}
