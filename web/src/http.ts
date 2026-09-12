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

export class ApiTimeoutError extends Error {
  constructor(readonly timeoutMs: number) {
    super(`请求超过 ${Math.ceil(timeoutMs / 1000)} 秒仍未完成`);
    this.name = "ApiTimeoutError";
  }
}

const DEFAULT_API_TIMEOUT_MS = 30_000;
export const CONNECTION_TEST_TIMEOUT_MS = 210_000;
export const DASHBOARD_CACHE_TTL_MS = 3_000;
export const reviewListKey = (status = "all", query = "", limit = 10) => `reviews:${status}:${query}:${limit}`;

// 管理页会在路由切换时重新挂载。短时缓存既能让返回页面立即显示，也能把
// StrictMode/重复挂载产生的并发 GET 合并成一次；写入操作和显式刷新会清空它。
export const SETTINGS_CACHE_TTL_MS = 15_000;
// 新鲜期结束后仍保留一份有界快照，用于 stale-while-revalidate：页面先绘制旧
// 数据，随后后台静默校验。这个窗口只存在于当前浏览器内存，登出/写入会清空。
const READ_CACHE_STALE_TTL_MS = 5 * 60_000;
const READ_CACHE_MAX_ENTRIES = 128;
const READ_CACHE_MAX_BYTES = 8 * 1024 * 1024;
type ReadCacheEntry = {
  value: unknown;
  expiresAt: number;
  staleUntil: number;
  sizeBytes: number;
};
type ReadRequestEntry = {
  promise: Promise<unknown>;
  controller: AbortController;
  consumers: number;
  settled: boolean;
  cacheable: boolean;
  abortTimer?: ReturnType<typeof globalThis.setTimeout>;
};
type ReadCacheListener = (value: unknown) => void;
const readCache = new Map<string, ReadCacheEntry>();
const readRequests = new Map<string, ReadRequestEntry>();
const readCacheListeners = new Map<string, Set<ReadCacheListener>>();
const readKeyGenerations = new Map<string, number>();
let readCacheGeneration = 0;
let readCacheBytes = 0;
let readCacheExpiryTimer: ReturnType<typeof globalThis.setTimeout> | undefined;
let readCacheEncoder: TextEncoder | undefined;

function estimateCacheBytes(value: unknown): number {
  try {
    const serialized = JSON.stringify(value);
    if (serialized === undefined) return 0;
    if (typeof TextEncoder !== "undefined") {
      readCacheEncoder ??= new TextEncoder();
      return readCacheEncoder.encode(serialized).byteLength;
    }
    // 旧浏览器没有 TextEncoder 时按 UTF-16 上界近似，宁可更早淘汰。
    return serialized.length * 2;
  } catch {
    // 无法安全估算的对象不进入缓存。
    return READ_CACHE_MAX_BYTES + 1;
  }
}

function deleteReadCacheEntry(key: string): boolean {
  const entry = readCache.get(key);
  if (!entry) return false;
  readCache.delete(key);
  readCacheBytes = Math.max(0, readCacheBytes - entry.sizeBytes);
  return true;
}

function pruneExpiredReadCache(now = Date.now()): boolean {
  let changed = false;
  for (const [key, entry] of readCache) {
    if (entry.staleUntil <= now) changed = deleteReadCacheEntry(key) || changed;
  }
  return changed;
}

function scheduleReadCacheExpiry(): void {
  if (readCacheExpiryTimer !== undefined) {
    globalThis.clearTimeout(readCacheExpiryTimer);
    readCacheExpiryTimer = undefined;
  }
  let nextExpiry = Number.POSITIVE_INFINITY;
  for (const entry of readCache.values()) {
    nextExpiry = Math.min(nextExpiry, entry.staleUntil);
  }
  if (!Number.isFinite(nextExpiry)) return;
  readCacheExpiryTimer = globalThis.setTimeout(() => {
    readCacheExpiryTimer = undefined;
    pruneExpiredReadCache();
    scheduleReadCacheExpiry();
  }, Math.max(0, nextExpiry - Date.now()));
}

function notifyReadCacheListeners(key: string, value: unknown): void {
  const listeners = readCacheListeners.get(key);
  if (!listeners || listeners.size === 0) return;
  // Copy the set so a listener can unsubscribe while processing an update
  // without affecting the remaining subscribers. Listener failures must not
  // turn a successful API response into a rejected request.
  for (const listener of [...listeners]) {
    try {
      listener(value);
    } catch {
      // UI subscribers are best-effort; the cache/API result remains valid.
    }
  }
}

function storeReadCacheEntry(
  key: string,
  value: unknown,
  ttlMs: number,
  staleTtlMs = READ_CACHE_STALE_TTL_MS,
): void {
  pruneExpiredReadCache();
  if (key.startsWith("dashboard:")) {
    const previous = readCache.get(key)?.value as { generated_at?: unknown } | undefined;
    const previousGeneratedAt = previous?.generated_at;
    const nextGeneratedAt = (value as { generated_at?: unknown } | undefined)?.generated_at;
    if (
      typeof previousGeneratedAt === "string"
      && (
        typeof nextGeneratedAt !== "string"
        || nextGeneratedAt < previousGeneratedAt
      )
    ) return;
  }
  deleteReadCacheEntry(key);
  const sizeBytes = estimateCacheBytes(value);
  if (sizeBytes > READ_CACHE_MAX_BYTES) {
    scheduleReadCacheExpiry();
    return;
  }
  const now = Date.now();
  readCache.set(key, {
    value,
    expiresAt: now + ttlMs,
    // staleTtlMs 表示从写入开始的总保留时间；至少覆盖新鲜期，避免
    // 自定义极短 TTL 产生“刚写入就已不可用”的快照。
    staleUntil: now + Math.max(ttlMs, staleTtlMs),
    sizeBytes,
  });
  readCacheBytes += sizeBytes;
  while (
    readCache.size > READ_CACHE_MAX_ENTRIES
    || readCacheBytes > READ_CACHE_MAX_BYTES
  ) {
    const oldest = readCache.keys().next().value;
    if (oldest === undefined) break;
    deleteReadCacheEntry(oldest);
  }
  scheduleReadCacheExpiry();
  notifyReadCacheListeners(key, value);
}

function abortError(signal: AbortSignal): Error {
  const reason = signal.reason;
  if (reason instanceof Error) return reason;
  return new DOMException("请求已取消", "AbortError");
}

function withAbort<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(abortError(signal));
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => {
      cleanup();
      reject(abortError(signal));
    };
    const cleanup = () => signal.removeEventListener("abort", onAbort);
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => {
        cleanup();
        resolve(value);
      },
      (error) => {
        cleanup();
        reject(error);
      },
    );
  });
}

function subscribeReadRequest<T>(
  key: string,
  entry: ReadRequestEntry,
  signal?: AbortSignal,
): Promise<T> {
  // React StrictMode 会在开发环境同步执行一次 effect 的挂载、清理和重新
  // 挂载。清理产生的延迟取消会在第二次订阅到来时被撤销，从而继续复用同一 GET。
  if (entry.abortTimer !== undefined) {
    globalThis.clearTimeout(entry.abortTimer);
    entry.abortTimer = undefined;
  }
  entry.consumers += 1;
  return new Promise<T>((resolve, reject) => {
    let released = false;
    const release = () => {
      if (released) return;
      released = true;
      signal?.removeEventListener("abort", onAbort);
      entry.consumers = Math.max(0, entry.consumers - 1);
      // 一个调用方取消只结束自己的等待。最后一个调用方离开后延迟到下一轮
      // 事件循环再终止共享 GET，让 StrictMode 的同步重新挂载有机会重新订阅。
      if (entry.consumers === 0 && !entry.settled) {
        entry.abortTimer = globalThis.setTimeout(() => {
          entry.abortTimer = undefined;
          if (entry.consumers !== 0 || entry.settled) return;
          entry.cacheable = false;
          if (readRequests.get(key) === entry) readRequests.delete(key);
          entry.controller.abort();
        }, 0);
      }
    };
    const onAbort = () => {
      release();
      reject(abortError(signal!));
    };
    if (signal?.aborted) {
      onAbort();
      return;
    }
    signal?.addEventListener("abort", onAbort, { once: true });
    entry.promise.then(
      (value) => {
        release();
        resolve(value as T);
      },
      (error) => {
        release();
        reject(error);
      },
    );
  });
}

export function cachedGet<T>(
  key: string,
  loader: (signal: AbortSignal) => Promise<T>,
  signal?: AbortSignal,
  ttlMs = SETTINGS_CACHE_TTL_MS,
  force = false,
): Promise<T> {
  if (signal?.aborted) return Promise.reject(abortError(signal));
  if (pruneExpiredReadCache()) scheduleReadCacheExpiry();
  if (force) {
    const previousRequest = readRequests.get(key);
    if (previousRequest) {
      previousRequest.cacheable = false;
      if (previousRequest.abortTimer !== undefined) {
        globalThis.clearTimeout(previousRequest.abortTimer);
        previousRequest.abortTimer = undefined;
      }
      // 显式强制刷新会取代同一键的旧请求，避免连续点击累积并发请求。
      previousRequest.controller.abort();
    }
    readRequests.delete(key);
    readKeyGenerations.set(key, (readKeyGenerations.get(key) ?? 0) + 1);
    scheduleReadCacheExpiry();
  }
  const now = Date.now();
  // 强制刷新绕过旧值，但保留它直到新响应成功；这样旧快照既能继续被
  // 当前页面使用，也能防止较旧的刷新响应把缓存倒退。
  const cached = force ? undefined : readCache.get(key);
  if (cached && cached.staleUntil > now) {
    // Map 的插入顺序作为轻量 LRU；命中后移动到末尾。
    readCache.delete(key);
    readCache.set(key, cached);
    if (cached.expiresAt <= now && !readRequests.has(key)) {
      // 旧快照先交给当前页面使用；后台请求不绑定页面 AbortSignal，路由
      // 卸载后仍可完成并供下一次进入复用。失败时保留旧快照直到 staleUntil。
      void cachedGet(key, loader, undefined, ttlMs, true).catch(() => undefined);
    }
    return withAbort(Promise.resolve(cached.value as T), signal);
  }
  let requestEntry = readRequests.get(key);
  if (!requestEntry) {
    const generation = readCacheGeneration;
    const keyGeneration = readKeyGenerations.get(key) ?? 0;
    const controller = new AbortController();
    let loaded: Promise<T>;
    try {
      loaded = loader(controller.signal);
    } catch (error) {
      loaded = Promise.reject(error);
    }
    let createdEntry!: ReadRequestEntry;
    const promise = loaded
      .then((value) => {
        if (
          createdEntry.cacheable
          && !controller.signal.aborted
          && generation === readCacheGeneration
          && keyGeneration === (readKeyGenerations.get(key) ?? 0)
        ) {
          storeReadCacheEntry(key, value, ttlMs);
        }
        return value;
      })
      .finally(() => {
        createdEntry.settled = true;
        if (createdEntry.abortTimer !== undefined) {
          globalThis.clearTimeout(createdEntry.abortTimer);
          createdEntry.abortTimer = undefined;
        }
        if (readRequests.get(key) === createdEntry) readRequests.delete(key);
      });
    createdEntry = {
      promise,
      controller,
      consumers: 0,
      settled: false,
      cacheable: true,
    };
    requestEntry = createdEntry;
    readRequests.set(key, requestEntry);
  }
  return subscribeReadRequest<T>(key, requestEntry, signal);
}

/**
 * 同步读取 GET 快照；新鲜期结束但仍在保留窗口内时也返回旧快照。
 *
 * 页面路由采用条件渲染，切换页面会卸载组件。peek 只返回内存中的值，不会
 * 发起请求，适合用作 React state 初始值；实际 API 调用会在旧快照期间启动
 * 后台 revalidate。
 */
export function peekReadCache<T>(key: string): T | undefined {
  const entry = readCache.get(key);
  if (!entry || entry.staleUntil <= Date.now()) {
    if (entry) deleteReadCacheEntry(key);
    return undefined;
  }
  return entry.value as T;
}

/**
 * 订阅指定 GET 快照的后台更新。
 *
 * stale-while-revalidate 会先把旧值交给页面，再在后台写入新值。如果页面
 * 只在请求返回时更新 React state，就会永远停留在旧值；订阅让缓存写入能
 * 及时驱动仍挂载的页面。返回取消函数，路由卸载时必须调用。
 */
export function subscribeReadCache<T>(
  key: string,
  listener: (value: T) => void,
): () => void {
  const wrapped = listener as ReadCacheListener;
  let listeners = readCacheListeners.get(key);
  if (!listeners) {
    listeners = new Set<ReadCacheListener>();
    readCacheListeners.set(key, listeners);
  }
  listeners.add(wrapped);
  return () => {
    const current = readCacheListeners.get(key);
    if (!current) return;
    current.delete(wrapped);
    if (current.size === 0) readCacheListeners.delete(key);
  };
}

/**
 * 把已经由实时通道取得的快照放入同一缓存。
 * 仅用于避免路由切换时丢掉刚收到的实时数据，不会发起网络请求。
 */
export function primeReadCache<T>(
  key: string,
  value: T,
  ttlMs = SETTINGS_CACHE_TTL_MS,
): void {
  // 实时快照优先于同键尚未结束的 HTTP 兜底请求；推进代次可阻止旧响应
  // 在稍后完成时覆盖这次写入。
  readKeyGenerations.set(key, (readKeyGenerations.get(key) ?? 0) + 1);
  storeReadCacheEntry(key, value, ttlMs);
}

/** 清除所有短时 GET 缓存；登录切换、写入配置和手动刷新时调用。 */
export function clearReadCache(): void {
  readCache.clear();
  readCacheBytes = 0;
  if (readCacheExpiryTimer !== undefined) {
    globalThis.clearTimeout(readCacheExpiryTimer);
    readCacheExpiryTimer = undefined;
  }
  // 让在途响应不再写缓存，但保留已有调用方的结果；调用方自己的 AbortSignal
  // 仍可单独离开，最后一个调用方离开时会由 subscribeReadRequest 中止底层 GET。
  for (const entry of readRequests.values()) {
    entry.cacheable = false;
    if (entry.abortTimer !== undefined) {
      globalThis.clearTimeout(entry.abortTimer);
      entry.abortTimer = undefined;
    }
  }
  readRequests.clear();
  readKeyGenerations.clear();
  readCacheGeneration += 1;
}

/**
 * 执行写请求并让所有依赖同一会话的读取快照失效。
 * 即使响应在服务端提交后丢失，也不能继续展示旧配置或旧列表。
 */
export async function mutation<T>(operation: () => Promise<T>): Promise<T> {
  try {
    return await operation();
  } finally {
    clearReadCache();
  }
}

/** 设置页旧调用方保留的别名；现在同时清除 Dashboard/知识库快照。 */
export function clearSettingsCache(): void {
  clearReadCache();
}

export async function request<T>(
  path: string,
  init?: RequestInit,
  timeoutMs = DEFAULT_API_TIMEOUT_MS,
): Promise<T> {
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
  const controller = new AbortController();
  const callerSignal = init?.signal;
  let timedOut = false;
  const relayAbort = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) relayAbort();
  else callerSignal?.addEventListener("abort", relayAbort, { once: true });
  const timeout = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      signal: controller.signal,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
  } catch (error) {
    if (timedOut) throw new ApiTimeoutError(timeoutMs);
    throw error;
  } finally {
    globalThis.clearTimeout(timeout);
    callerSignal?.removeEventListener("abort", relayAbort);
  }

  if (!response.ok) {
    if (response.status === 401) clearSettingsCache();
    let message = `请求失败（HTTP ${response.status}）`;
    try {
      const body = (await response.json()) as {
        detail?: string;
        error?: { message?: string };
      };
      if (body.detail) message = body.detail;
      else if (body.error?.message) message = body.error.message;
    } catch {
      // 代理返回非 JSON 错误页面时，保留安全的 HTTP 兜底信息。
    }
    throw new ApiError(message, response.status);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
