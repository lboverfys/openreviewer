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
