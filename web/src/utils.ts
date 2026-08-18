import type { ExecutionStatus, WorkerStatus } from "./types";

export const statusLabels: Record<ExecutionStatus, string> = {
  queued: "排队中",
  waiting_for_ci: "等待 CI",
  running: "处理中",
  completed: "已完成",
  failed: "失败",
  timed_out: "已超时",
  cancelled: "已取消",
  superseded: "已被替代",
};

export const workerLabels: Record<WorkerStatus, string> = {
  starting: "启动中",
  idle: "空闲",
  busy: "处理中",
  stopping: "停止中",
};

export function shortSha(sha: string): string {
  return sha.slice(0, 8);
}

export function formatDate(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return "发生了未知错误，请稍后重试";
}
