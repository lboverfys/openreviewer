import type { ExecutionStatus, ReviewItem, WorkerStatus } from "./types";

export const statusLabels: Record<ExecutionStatus, string> = {
  queued: "排队中",
  waiting_for_ci: "等待 CI",
  running: "处理中",
  ready_for_review: "待处理",
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

export const stageLabels: Record<string, string> = {
  intake: "接收任务",
  context: "读取 PR",
  ci: "等待 CI",
  planning: "生成审查计划",
  model: "AI 分析",
  verification: "复核问题",
  publication: "发布结果",
};

export const phaseLabels: Record<string, string> = {
  queued: "任务已进入队列，等待 Worker 领取",
  context_loading: "Worker 正在读取 PR、变更文件和 CI 状态",
  waiting_ci: "代码已读取，正在等待 GitHub CI 结束",
  ci_checking: "Worker 正在刷新 GitHub CI 状态",
  planning_queued: "CI 已结束，等待生成审查计划",
  planning_running: "正在整理规则、文件和审查范围",
  model_queued: "审查计划已准备，等待 AI 分析",
  model_running: "AI 正在分析变更，请稍候",
  awaiting_verification: "AI 已返回结果，请逐条确认问题是否成立",
  awaiting_publication: "结果已准备，等待发布流程",
  model_failed: "AI 分析失败，请查看错误和日志后重试",
  planning_failed: "审查计划生成失败，请查看日志后重试",
  context_failed: "读取 GitHub 上下文失败，请查看日志后重试",
  ci_timed_out: "等待 CI 超时，任务没有被视为审查通过",
  completed: "审查流程已完成",
  cancelled: "任务已取消",
  superseded: "任务已被同一 PR 的新提交替代",
};

export function reviewDisplayLabel(review: Pick<ReviewItem, "execution_status" | "model_review_completed_at">): string {
  if (review.execution_status === "ready_for_review" && review.model_review_completed_at) {
    return "待复核结果";
  }
  return statusLabels[review.execution_status];
}

export function shortSha(sha: string): string {
  /**
   * 生成适合表格显示的短提交标识。
   *
   * 参数：
   * - `sha`：API 返回的完整提交 SHA；函数不验证其长度或十六进制格式。
   *
   * 返回：
   * - 原字符串前八个 UTF-16 代码单元。正常 SHA 因此显示八位，短输入则原样截短。
   *
   * 这里只改变展示文本，详细 SHA 仍保留在数据模型中，便于后续复制或精确比较。
   */
  return sha.slice(0, 8);
}

export function formatDate(value: string | null): string {
  /**
   * 将 API 返回的 ISO 时间格式化为中文本地时间。
   *
   * 空值或非法日期统一显示短横线，避免 Dashboard 在数据尚未到达时出现
   * ``Invalid Date`` 这类面向开发者的文本。
   *
   * 参数：
   * - `value`：后端返回的 ISO 8601 时间文本，或表示“尚未有时间”的 `null`。
   *
   * 返回：
   * - 合法时间按浏览器本地时区格式化为 `MM/DD HH:mm:ss` 风格的中文时间；
   *   空值和无法解析的文本统一返回 `—`。
   *
   * 函数不修改输入，也不抛出日期解析异常；显示层因此可以安全处理部分快照。
   */
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
  /**
   * 把未知异常归一化为页面可显示的错误文本。
   *
   * 参数：
   * - `error`：任意 `catch` 得到的值，因为 JavaScript 允许抛出非 `Error` 对象。
   *
   * 返回：
   * - `Error` 实例的 message，或不明类型的通用重试提示。
   *
   * 该函数不把对象序列化到页面，避免把响应头、堆栈或其他调试字段直接暴露给用户；
   * API 状态码相关的特殊处理应在调用它之前完成。
   */
  if (error instanceof Error) return error.message;
  return "发生了未知错误，请稍后重试";
}
