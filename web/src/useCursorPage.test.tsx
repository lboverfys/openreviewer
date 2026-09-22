// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ApiError, ApiTimeoutError, clearReadCache } from "./api";
import { useCursorPage, type CursorPage } from "./useCursorPage";

function deferredPage() {
  let resolve!: (page: CursorPage<string>) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<CursorPage<string>>((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

afterEach(() => { cleanup(); clearReadCache(); });

it("旧请求完成不能提前结束新刷新，也不能显示过时列表", async () => {
  const first = deferredPage();
  const second = deferredPage();
  const load = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  const onError = vi.fn();
  const { result } = renderHook(() => useCursorPage({ cacheKey: "concurrent", load, onError }));
  let refresh!: Promise<void>;
  act(() => { refresh = result.current.refresh(); });
  await act(async () => { first.resolve({ items: ["old"] }); });
  expect(result.current.loading).toBe(true);
  expect(result.current.data).toBeUndefined();
  await act(async () => { second.resolve({ items: ["new"] }); await refresh; });
  expect(result.current.data?.items).toEqual(["new"]);
  expect(result.current.loading).toBe(false);
  expect(onError).not.toHaveBeenCalled();
});

it.each(["success", "error"])("新请求完成后忽略旧请求晚到的 %s", async outcome => {
  const first = deferredPage();
  const second = deferredPage();
  const load = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  const onError = vi.fn();
  const { result } = renderHook(() => useCursorPage({ cacheKey: "concurrent", load, onError }));
  let refresh!: Promise<void>;
  act(() => { refresh = result.current.refresh(); });
  await act(async () => { second.resolve({ items: ["new"] }); await refresh; });
  await act(async () => {
    if (outcome === "success") first.resolve({ items: ["old"] });
    else first.reject(new ApiError("旧刷新失败", 503));
  });
  expect(result.current.data?.items).toEqual(["new"]);
  expect(result.current.loading).toBe(false);
  expect(onError).not.toHaveBeenCalled();
});

it.each([
  [new DOMException("signal is aborted without reason", "AbortError"), false],
  [new ApiTimeoutError(30_000), true],
  [new ApiError("列表读取失败", 503), true],
  [new TypeError("Failed to fetch"), true],
])("刷新结束时正确区分取消和真实失败：%s", async (error, shouldReport) => {
  const page = deferredPage();
  const load = vi.fn().mockReturnValue(page.promise);
  const onError = vi.fn();
  const { result } = renderHook(() => useCursorPage({ cacheKey: "failure", load, onError }));
  await act(async () => { page.reject(error); });
  expect(result.current.loading).toBe(false);
  if (shouldReport) expect(onError).toHaveBeenCalledExactlyOnceWith(error);
  else expect(onError).not.toHaveBeenCalled();
});

it.each(["disabled", "unmounted"])("页面离开后忽略没有外部信号的手动刷新：%s", async outcome => {
  const first = deferredPage();
  const second = deferredPage();
  const load = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  const onError = vi.fn();
  const { result, rerender, unmount } = renderHook(
    ({ enabled }) => useCursorPage({ cacheKey: "leaving", load, onError, enabled }),
    { initialProps: { enabled: true } },
  );
  await act(async () => { first.resolve({ items: ["existing"] }); });
  let refresh!: Promise<void>;
  act(() => { refresh = result.current.refresh(); });
  if (outcome === "unmounted") unmount();
  else rerender({ enabled: false });
  await act(async () => { second.reject(new ApiError("旧页面刷新失败", 503)); await refresh; });
  expect(onError).not.toHaveBeenCalled();
});
