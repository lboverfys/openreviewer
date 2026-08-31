// @vitest-environment jsdom

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import {
  REVIEW_CHANGE_POLL_MS,
  useReviewAutoRefresh,
} from "./useReviewAutoRefresh";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      reviewChangeToken: vi.fn(),
    },
  };
});

afterEach(() => {
  vi.useRealTimers();
  vi.mocked(api.reviewChangeToken).mockReset();
});

describe("审查详情轻量自动刷新", () => {
  it("令牌不变时不加载详情，变化后只触发一次刷新", async () => {
    vi.useFakeTimers();
    vi.mocked(api.reviewChangeToken)
      .mockResolvedValueOnce({ change_token: "token-a" })
      .mockResolvedValueOnce({ change_token: "token-b" });
    const onChanged = vi.fn().mockResolvedValue("token-b");

    const { unmount } = renderHook(() => useReviewAutoRefresh({
      enabled: true,
      reviewRunId: "run-1",
      changeToken: "token-a",
      onChanged,
      onSignedOut: vi.fn(),
      onError: vi.fn(),
    }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(REVIEW_CHANGE_POLL_MS);
    });
    expect(onChanged).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(REVIEW_CHANGE_POLL_MS);
    });
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(api.reviewChangeToken).toHaveBeenCalledTimes(2);
    unmount();
  });

  it("组件卸载后忽略迟到的令牌响应", async () => {
    vi.useFakeTimers();
    let resolveToken!: (value: { change_token: string }) => void;
    vi.mocked(api.reviewChangeToken).mockImplementationOnce(
      () => new Promise((resolve) => { resolveToken = resolve; }),
    );
    const onChanged = vi.fn().mockResolvedValue("token-b");
    const { unmount } = renderHook(() => useReviewAutoRefresh({
      enabled: true,
      reviewRunId: "run-late",
      changeToken: "token-a",
      onChanged,
      onSignedOut: vi.fn(),
      onError: vi.fn(),
    }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(REVIEW_CHANGE_POLL_MS);
    });
    expect(api.reviewChangeToken).toHaveBeenCalledTimes(1);

    unmount();
    resolveToken({ change_token: "token-b" });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("请求尚未结束时不会启动第二个轮询", async () => {
    vi.useFakeTimers();
    const resolvers: Array<(value: { change_token: string }) => void> = [];
    vi.mocked(api.reviewChangeToken).mockImplementation(
      () => new Promise((resolve) => { resolvers.push(resolve); }),
    );
    const { unmount } = renderHook(() => useReviewAutoRefresh({
      enabled: true,
      reviewRunId: "run-overlap",
      changeToken: "token-a",
      onChanged: vi.fn().mockResolvedValue("token-a"),
      onSignedOut: vi.fn(),
      onError: vi.fn(),
    }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(REVIEW_CHANGE_POLL_MS);
      await vi.advanceTimersByTimeAsync(REVIEW_CHANGE_POLL_MS * 3);
    });
    expect(api.reviewChangeToken).toHaveBeenCalledTimes(1);

    resolvers[0]!({ change_token: "token-a" });
    await act(async () => {
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(REVIEW_CHANGE_POLL_MS);
    });
    expect(api.reviewChangeToken).toHaveBeenCalledTimes(2);
    resolvers[1]!({ change_token: "token-a" });
    unmount();
  });

  it("详情仍是旧快照时不会吞掉变更令牌", async () => {
    vi.useFakeTimers();
    vi.mocked(api.reviewChangeToken).mockResolvedValue({
      change_token: "token-b",
    });
    const onChanged = vi.fn()
      .mockResolvedValueOnce("token-a")
      .mockResolvedValueOnce("token-b");
    const { unmount } = renderHook(() => useReviewAutoRefresh({
      enabled: true,
      reviewRunId: "run-eventual-consistency",
      changeToken: "token-a",
      onChanged,
      onSignedOut: vi.fn(),
      onError: vi.fn(),
    }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(REVIEW_CHANGE_POLL_MS);
    });
    expect(onChanged).toHaveBeenCalledTimes(1);

    // 首次详情读取仍返回 token-a，因此下一轮必须再次读取详情，直到它
    // 真正追上轮询接口已经观察到的 token-b。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(REVIEW_CHANGE_POLL_MS);
    });
    expect(onChanged).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(REVIEW_CHANGE_POLL_MS);
    });
    expect(onChanged).toHaveBeenCalledTimes(2);
    expect(api.reviewChangeToken).toHaveBeenCalledTimes(3);
    unmount();
  });
});
