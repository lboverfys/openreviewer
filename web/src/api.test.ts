import { afterEach, describe, expect, it, vi } from "vitest";

import {
  api,
  ApiTimeoutError,
  clearSettingsCache,
  peekReadCache,
  primeReadCache,
  subscribeReadCache,
} from "./api";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  clearSettingsCache();
});

describe("API 请求生命周期", () => {
  it("保留合法错误请求 ID，拒绝非法响应头并兼容非字符串 detail", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({detail:"请求冲突"}), {status:409,headers:{"X-Request-ID":"request-123"}}))
      .mockResolvedValueOnce(new Response(JSON.stringify({detail:[{msg:"invalid"}]}), {status:422,headers:{"X-Request-ID":"invalid value"}}));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.me()).rejects.toMatchObject({status:409, requestId:"request-123", message:"请求冲突\n请求 ID：request-123"});
    await expect(api.me()).rejects.toMatchObject({status:422, requestId:undefined, message:"请求失败（HTTP 422）"});
  });
  it("为所有请求携带同源凭据并解析成功响应", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          username: "reviewer",
          role: "viewer",
          permissions: ["reviews:view"],
          expires_at: "2026-08-30T00:00:00Z",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.me()).resolves.toMatchObject({ username: "reviewer" });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/me",
      expect.objectContaining({
        credentials: "same-origin",
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("超过默认时限后取消底层 fetch 并返回明确超时错误", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn((_path: string, init?: RequestInit) => new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        });
      })),
    );

    const pending = api.me();
    const rejection = expect(pending).rejects.toBeInstanceOf(ApiTimeoutError);
    await vi.advanceTimersByTimeAsync(30_000);
    await rejection;
  });

  it("调用方取消时立即中止请求且不会误报为超时", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((_path: string, init?: RequestInit) => new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        });
      })),
    );
    const controller = new AbortController();

    const pending = api.me(controller.signal);
    controller.abort();

    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("合并设置页并发读取，并在短时缓存命中时避免重复 GET", async () => {
    let resolveResponse!: (response: Response) => void;
    const fetchMock = vi.fn(
      () => new Promise<Response>((resolve) => { resolveResponse = resolve; }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const first = api.aiSettings();
    const second = api.aiSettings();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    resolveResponse(
      new Response(JSON.stringify({ revision: 7, providers: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(Promise.all([first, second])).resolves.toHaveLength(2);
    await expect(api.aiSettings()).resolves.toMatchObject({ revision: 7 });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    clearSettingsCache();
    let resolveRefresh!: (response: Response) => void;
    fetchMock.mockImplementationOnce(
      () => new Promise<Response>((resolve) => { resolveRefresh = resolve; }),
    );
    const refreshed = api.aiSettings();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    resolveRefresh(
      new Response(JSON.stringify({ revision: 8, providers: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(refreshed).resolves.toMatchObject({ revision: 8 });
  });

  it("允许页面同步读取旧快照，并在后台静默重新校验", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ revision: 11, providers: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ revision: 12, providers: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    expect(peekReadCache("ai-settings")).toBeUndefined();
    await expect(api.aiSettings()).resolves.toMatchObject({ revision: 11 });
    expect(peekReadCache<{ revision: number }>("ai-settings")).toMatchObject({
      revision: 11,
    });

    await vi.advanceTimersByTimeAsync(15_000);
    expect(peekReadCache<{ revision: number }>("ai-settings")).toMatchObject({
      revision: 11,
    });

    // 旧快照立即返回，同时只启动一次后台校验请求。
    await expect(api.aiSettings()).resolves.toMatchObject({ revision: 11 });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // 后台响应完成后缓存更新为新版本。
    await vi.waitFor(() => {
      expect(peekReadCache<{ revision: number }>("ai-settings")).toMatchObject({
        revision: 12,
      });
    });
  });

  it("后台校验写入新快照时通知仍挂载的页面", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ revision: 1 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ revision: 2 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetchMock);
    const updates: number[] = [];
    const unsubscribe = subscribeReadCache<{ revision: number }>(
      "ai-settings",
      (value) => updates.push(value.revision),
    );
    await api.aiSettings();
    await vi.advanceTimersByTimeAsync(15_000);
    await api.aiSettings();
    await vi.waitFor(() => expect(updates).toEqual([1, 2]));
    unsubscribe();
  });

  it("设置和 Agent 读取支持绕过缓存做后台新鲜度校验", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ revision: 1 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ revision: 2 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ revision: 3 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.aiSettings()).resolves.toMatchObject({ revision: 1 });
    // force=true 必须绕过仍有效的 revision=1 快照，并把新值写回缓存。
    await expect(api.aiSettings(undefined, true)).resolves.toMatchObject({ revision: 2 });
    await expect(api.aiSettings()).resolves.toMatchObject({ revision: 2 });

    // Agent 读取沿用同一语义，且参数位置保持 signal 在前以兼容旧调用方。
    await expect(api.agentSettings(undefined, true)).resolves.toMatchObject({ revision: 3 });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("配置审计读取支持显式强制刷新", async () => {
    const audit = {
      items: [],
      next_cursor: null,
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(audit), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        items: [{ revision: 2 }],
        next_cursor: null,
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.configurationAudits()).resolves.toMatchObject({ items: [] });
    await expect(api.configurationAudits(undefined, true)).resolves.toMatchObject({
      items: [{ revision: 2 }],
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("实时通道写入的快照也能供路由返回时同步绘制", () => {
    primeReadCache("dashboard:first:50", { generated_at: "now" }, 1_000);
    expect(peekReadCache<{ generated_at: string }>("dashboard:first:50"))
      .toEqual({ generated_at: "now" });
    clearSettingsCache();
    expect(peekReadCache("dashboard:first:50")).toBeUndefined();
  });

  it("实时快照不会被较晚完成的旧 HTTP 兜底响应覆盖", async () => {
    let resolveOld!: (response: Response) => void;
    const fetchMock = vi.fn(
      () => new Promise<Response>((resolve) => { resolveOld = resolve; }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const pending = api.dashboard();
    primeReadCache("dashboard:first:50", { generated_at: "newer" }, 1_000);
    resolveOld(
      new Response(JSON.stringify({ generated_at: "older" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await pending;
    expect(peekReadCache<{ generated_at: string }>("dashboard:first:50"))
      .toMatchObject({ generated_at: "newer" });
  });

  it("强制刷新保留旧快照直到成功，并拒绝倒退的 Dashboard 时间戳", async () => {
    primeReadCache("dashboard:first:50", { generated_at: "2026-08-30T00:00:02Z" }, 1_000);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ generated_at: "2026-08-30T00:00:01Z" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    await api.dashboard(undefined, 50, undefined, true);
    expect(peekReadCache<{ generated_at: string }>("dashboard:first:50"))
      .toMatchObject({ generated_at: "2026-08-30T00:00:02Z" });
  });

  it("设置读取在调用方已取消时不启动网络请求", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    controller.abort();

    await expect(api.aiSettings(controller.signal)).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("短时缓存知识库列表和文档详情，并在写入后失效", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        revision: 1,
        total: 1,
        enabled_count: 1,
        total_enabled_bytes: 12,
        items: [],
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        id: "doc-1",
        source: "rules.md",
        title: "规则",
        enabled: true,
        archived: false,
        current_version: 1,
        content_sha256: "abc",
        byte_size: 12,
        created_by: "admin",
        updated_by: "admin",
        created_at: "2026-08-30T00:00:00Z",
        updated_at: "2026-08-30T00:00:00Z",
        content: "# rules",
        versions: [],
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValue(new Response(JSON.stringify({
        revision: 1,
        total: 1,
        enabled_count: 1,
        total_enabled_bytes: 12,
        items: [],
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    const firstList = api.knowledgeDocuments();
    const secondList = api.knowledgeDocuments();
    await expect(Promise.all([firstList, secondList])).resolves.toHaveLength(2);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    const firstDocument = api.knowledgeDocument("doc-1");
    const secondDocument = api.knowledgeDocument("doc-1");
    await expect(Promise.all([firstDocument, secondDocument])).resolves.toHaveLength(2);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    clearSettingsCache();
    await expect(api.knowledgeDocuments()).resolves.toMatchObject({ revision: 1 });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("Dashboard 用户主动刷新时绕过短时缓存", async () => {
    const body = {
      generated_at: "2026-08-30T00:00:00Z",
      total_reviews: 0,
      status_counts: {},
      worker: {},
      workers: [],
      recent_reviews: [],
      next_cursor: null,
    };
    const fetchMock = vi.fn().mockImplementation(
      () => new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.dashboard();
    await api.dashboard();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await api.dashboard(undefined, 50, undefined, true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await api.dashboard();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("共享读取中一个调用方取消时不影响其他调用方", async () => {
    let resolveResponse!: (response: Response) => void;
    const fetchMock = vi.fn(
      () => new Promise<Response>((resolve) => { resolveResponse = resolve; }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const cancelled = new AbortController();
    const first = api.aiSettings(cancelled.signal);
    const second = api.aiSettings();
    cancelled.abort();

    await expect(first).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    resolveResponse(
      new Response(JSON.stringify({ revision: 9, providers: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(second).resolves.toMatchObject({ revision: 9 });
    await expect(api.aiSettings()).resolves.toMatchObject({ revision: 9 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("最后一个调用方取消后在下一轮事件循环中止共享底层请求", async () => {
    vi.useFakeTimers();
    let aborted = false;
    const fetchMock = vi.fn(
      (_path: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          aborted = true;
          reject(new DOMException("aborted", "AbortError"));
        });
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const cancelled = new AbortController();
    const pending = api.aiSettings(cancelled.signal);
    cancelled.abort();

    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(aborted).toBe(false);
    await vi.runOnlyPendingTimersAsync();
    expect(aborted).toBe(true);
  });

  it("开发模式同步重新挂载时复用尚未完成的设置请求", async () => {
    vi.useFakeTimers();
    let resolveResponse!: (response: Response) => void;
    const fetchMock = vi.fn(
      () => new Promise<Response>((resolve) => { resolveResponse = resolve; }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const firstMount = new AbortController();
    const first = api.aiSettings(firstMount.signal);

    // 模拟 StrictMode：第一次 effect 立即清理，随后在定时取消执行前重新挂载。
    firstMount.abort();
    const second = api.aiSettings();
    await expect(first).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    resolveResponse(
      new Response(JSON.stringify({ revision: 10, providers: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(second).resolves.toMatchObject({ revision: 10 });
    await vi.runOnlyPendingTimersAsync();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("清除缓存会使在途结果失效但不打断已有调用方", async () => {
    let resolveOld!: (response: Response) => void;
    let resolveFresh!: (response: Response) => void;
    const fetchMock = vi.fn()
      .mockImplementationOnce(
        () => new Promise<Response>((resolve) => { resolveOld = resolve; }),
      )
      .mockImplementationOnce(
        () => new Promise<Response>((resolve) => { resolveFresh = resolve; }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const old = api.aiSettings();
    clearSettingsCache();
    const fresh = api.aiSettings();
    expect(fetchMock).toHaveBeenCalledTimes(2);

    resolveOld(
      new Response(JSON.stringify({ revision: 1, providers: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    resolveFresh(
      new Response(JSON.stringify({ revision: 2, providers: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(old).resolves.toMatchObject({ revision: 1 });
    await expect(fresh).resolves.toMatchObject({ revision: 2 });
    await expect(api.aiSettings()).resolves.toMatchObject({ revision: 2 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
