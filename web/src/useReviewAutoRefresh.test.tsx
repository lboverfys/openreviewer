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
    const onChanged = vi.fn().mockResolvedValue(true);

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
});
