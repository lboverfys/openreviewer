import type {
  AuthUser,
  DashboardSnapshot,
  ReviewAccepted,
  ReviewRequest,
} from "./types";

export class ApiError extends Error {
  /**
   * 创建带 HTTP 状态码的 API 业务错误。
   *
   * `Error.message` 保存可以展示给用户的后端 detail，`status` 保留原始状态码，
   * 让组件能够把 401（回到登录页）、429（稍后重试）和 422（修正输入）区别处理。
   * 该类不保存响应体、Cookie 或请求参数，避免错误对象意外携带敏感数据。
   */
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  /**
   * 统一发送同源 API 请求并把后端错误转换成 ApiError。
   *
   * 所有请求都携带浏览器 Cookie；当请求有 body 时自动声明 JSON。成功响应按
   * 泛型 T 解析，204 则返回 undefined；非 2xx 响应会尽力读取后端的 detail，
   * 这样页面可以显示可理解的错误，而不会把网络层细节散落在每个组件里。
   *
   * 参数：
   * - `path`：同源 API 相对路径，例如 `/api/v1/dashboard`。
   * - `init`：可选的 Fetch 请求配置；调用方可以提供 method、body 和额外请求头。
   *
   * 返回：
   * - 2xx 且有 JSON body 时，解析为调用方指定的 `T`。
   * - 204 时返回 `undefined`，仍通过泛型保持调用方类型一致。
   *
   * 异常：
   * - 网络层异常原样抛出，由页面显示网络不可用提示。
   * - 非 2xx 响应转换成 `ApiError`；若响应不是 JSON，则使用安全的 HTTP 状态兜底文案。
   *
   * 请求始终使用 `same-origin` 凭据策略，因此浏览器会自动携带 HttpOnly 会话 Cookie；
   * API 层不会把 Token 读取到 JavaScript 或 `localStorage`。登录页的“记住账号密码”
   * 由独立的凭据适配层交给浏览器密码库处理，不改变这里的会话传输边界。
   */
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) message = body.detail;
    } catch {
      // Keep the safe HTTP fallback when a proxy returns a non-JSON error page.
    }
    throw new ApiError(message, response.status);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  /**
   * 读取当前管理员会话。
   *
   * 返回用户名和会话到期时间；未登录、过期或签名无效时抛出状态码为 401 的
   * `ApiError`，由根组件切换到登录页。请求没有副作用，也不会刷新会话期限。
   */
  me: () => request<AuthUser>("/api/v1/auth/me"),
  /**
   * 提交管理员凭据并接收服务端设置的 HttpOnly 会话 Cookie。
   *
   * 参数：
   * - `username`：管理员账号文本。
   * - `password`：本次登录使用的明文密码，只交给 HTTPS API，不在模块中保存。
   *
   * 返回登录后的公开用户信息；密码错误通常得到 401，限流得到 429，配置或网络
   * 故障则由统一请求函数转换/传播。Cookie 由浏览器管理，返回值不包含 Token。
   */
  login: (username: string, password: string) =>
    request<AuthUser>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  /**
   * 请求服务端删除当前会话 Cookie。
   *
   * 该操作对已经没有 Cookie 的客户端也是幂等的；成功时返回 204。网络失败时
   * 调用方仍可清空本地页面状态，因为应用没有保存会话 Token；浏览器密码库中若有
   * 用户主动记住的账号密码，不会被注销接口删除。
   */
  logout: () =>
    request<void>("/api/v1/auth/logout", {
      method: "POST",
    }),
  /**
   * 读取一次完整 Dashboard 快照。
   *
   * 首屏加载和用户点击“立即刷新”都会调用它；返回状态计数、Worker 心跳和最近
   * 任务。数据库暂时不可用时抛出 `ApiError(503)`，不会伪造空快照覆盖旧数据。
   */
  dashboard: () => request<DashboardSnapshot>("/api/v1/dashboard"),
  /**
   * 创建一个幂等的审查任务。
   *
   * 参数：
   * - `payload`：安装、仓库、PR 编号和完整 head SHA。
   * - `idempotencyKey`：一次逻辑提交的稳定键；相同键重试不会重复创建任务。
   *
   * 返回 202 接受结果；`created` 可区分首次创建和幂等重试。输入不合法时为 422，
   * 同一键对应不同内容时为 409，认证失效时为 401。该方法不等待 Worker 完成审查。
   */
  createReview: (payload: ReviewRequest, idempotencyKey: string) =>
    request<ReviewAccepted>("/api/v1/reviews", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(payload),
    }),
};
