import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiTimeoutError } from "./api";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("API 请求生命周期", () => {
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
});
