import type { ExecutionStatus, ReviewItem, WorkerStatus } from "./types";

export const statusLabels: Record<ExecutionStatus, string> = {
  queued: "排队中",
  ci: "CI 阶段",
  planning: "规划中",
  agent_batches: "三路 Agent 审查",
  aggregating: "汇总中",
  awaiting_approval: "待人工批准",
  approved: "已批准",
  rejected: "已驳回",
  awaiting_publish: "待人工发布",
  publishing: "发布中",
  paused: "已暂停",
  waiting_for_ci: "等待 CI",
  running: "处理中",
  ready_for_review: "等待 AI",
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
  queued: "接收排队",
  intake: "接收任务",
  context: "读取 PR",
  ci: "等待 CI",
  planning: "生成审查计划",
  model: "AI 分析",
  agent_batches: "三路 Agent",
  aggregating: "结果汇总",
  awaiting_approval: "等待人工批准",
  approved: "已批准",
  rejected: "已驳回",
  awaiting_publish: "等待人工发布",
  publishing: "发布中",
  paused: "已暂停",
  completed: "已完成",
  approval: "人工批准",
  publish: "人工发布",
  result: "审查结果",
};

export const phaseLabels: Record<string, string> = {
  retrieval_started: "准备代码检索上下文",
  retrieval_completed: "代码检索上下文已就绪",
  queued: "任务已进入队列，等待 Worker 领取",
  context_loading: "Worker 正在读取 PR、变更文件和 CI 状态",
  waiting_ci: "代码已读取，正在等待 GitHub CI 结束",
  ci_checking: "Worker 正在刷新 GitHub CI 状态",
  planning_queued: "CI 已结束，等待生成审查计划",
  planning_running: "正在整理规则、文件和审查范围",
  model_queued: "审查计划已准备，等待 AI 分析",
  model_retry_waiting: "上一轮 AI 请求失败，系统会按计划自动重试",
  model_running: "AI 正在分析变更，请稍候",
  model_failed: "AI 分析失败，请查看错误和日志后重试",
  agent_batches_running: "安全、规范和逻辑 Agent 正在并行审查",
  aggregating_running: "三路 Agent 已完成，汇总 Agent 正在整理最终结果",
  planning_failed: "审查计划生成失败，请查看日志后重试",
  context_failed: "读取 GitHub 上下文失败，请查看日志后重试",
  ci_timed_out: "等待 CI 超时，任务没有被视为审查通过",
  completed: "审查流程已完成",
  awaiting_approval: "三路审查已完成，等待人工批准",
  awaiting_publish: "结果已批准，等待人工点击发布",
  publishing: "正在人工发布到 GitHub",
  approved: "审查已批准，等待发布",
  rejected: "审查结果已驳回",
  paused: "审查已暂停，可继续或重试指定阶段",
  cancelled: "任务已取消",
  superseded: "任务已被同一 PR 的新提交替代",
};

export function reviewDisplayStatus(review: Pick<ReviewItem, "execution_status" | "model_review_completed_at"> & Partial<Pick<ReviewItem, "workflow_status">>): ExecutionStatus {
  if (review.workflow_status && ["cancelled", "superseded", "paused", "rejected", "completed"].includes(review.workflow_status)) return review.workflow_status;
  if (review.execution_status === "failed") return "failed";
  if (review.workflow_status && ["awaiting_approval", "awaiting_publish", "publishing"].includes(review.workflow_status)) return review.workflow_status;
  if (review.execution_status === "ready_for_review" && review.model_review_completed_at) {
    return "completed";
  }
  return review.execution_status;
}

export function reviewDisplayLabel(review: Pick<ReviewItem, "execution_status" | "model_review_completed_at"> & Partial<Pick<ReviewItem, "workflow_status">>): string {
  const state = reviewDisplayStatus(review);
  return state === "awaiting_approval" ? "待核对结果" : state === "awaiting_publish" ? "待发布" : statusLabels[state];
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
