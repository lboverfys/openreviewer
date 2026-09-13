// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { api, clearSettingsCache } from "./api";
import fixture from "./fixtures/review-details.json";
import ReviewDetailPage from "./ReviewDetailPage";
import type { AuthUser, ReviewDetails } from "./types";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {...actual, api: {...actual.api, reviewDetails: vi.fn(), reviewRetrieval: vi.fn(), findingPage: vi.fn(), eventPage: vi.fn()}};
});
const details = fixture as unknown as ReviewDetails;
const user = {username: "preview", role: "viewer", permissions: ["reviews:view"]} as AuthUser;

beforeEach(() => {
  clearSettingsCache();
  vi.mocked(api.reviewDetails).mockResolvedValue(details);
  vi.mocked(api.reviewRetrieval).mockResolvedValue([]);
  vi.mocked(api.findingPage).mockResolvedValue({items: details.findings, next_cursor: null});
  vi.mocked(api.eventPage).mockImplementation(async (_id, cursor) => ({
    items: [{id: cursor ?? "first-event", event_type: "review.workflow.advance", payload: {}, occurred_at: "2026-09-12T00:00:00Z"}],
    next_cursor: cursor ? null : "second-page",
  }));
});
afterEach(() => {cleanup(); clearSettingsCache(); vi.clearAllMocks();});

it("概览显示全量问题数，未打开的日志、问题和批次不会提前渲染或请求", async () => {
  render(<ReviewDetailPage user={user} reviewRunId={details.review_run_id} onBack={vi.fn()} onOpenReview={vi.fn()} onSignedOut={vi.fn()} />);
  await screen.findByRole("heading", {name: "审查流程"});
  expect(within(screen.getByRole("region", {name: "任务关键指标"})).getByText("25")).toBeInTheDocument();
  expect(document.querySelector(".review-log-panel")).toBeNull();
  expect(document.querySelector(".review-result-panel")).toBeNull();
  expect(document.querySelector(".review-batch-panel")).toBeNull();
  expect(api.eventPage).not.toHaveBeenCalled();
  expect(api.findingPage).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", {name: /问题与结论\s*25/}));
  await waitFor(() => expect(api.findingPage).toHaveBeenCalledTimes(1));
});

it("刷新详情后重新读取当前日志页，保持第二页位置", async () => {
  render(<ReviewDetailPage user={user} reviewRunId={details.review_run_id} onBack={vi.fn()} onOpenReview={vi.fn()} onSignedOut={vi.fn()} />);
  await screen.findByRole("heading", {name: "审查流程"});
  fireEvent.click(screen.getByRole("button", {name: /运行日志/}));
  const pagination = await screen.findByRole("navigation", {name: "运行日志分页"});
  await waitFor(() => expect(within(pagination).getByRole("button", {name: "下一页"})).toBeEnabled());
  fireEvent.click(within(pagination).getByRole("button", {name: "下一页"}));
  await screen.findByText(/第 2 页/);
  await waitFor(() => expect(api.eventPage).toHaveBeenCalledTimes(2));
  vi.mocked(api.reviewDetails).mockResolvedValue({...details, change_token: "new-token"});
  fireEvent.click(screen.getByRole("button", {name: "↻ 刷新"}));
  await waitFor(() => expect(api.eventPage).toHaveBeenCalledTimes(3));
  expect(vi.mocked(api.eventPage).mock.calls.at(-1)?.[1]).toBe("second-page");
  expect(screen.getByText(/第 2 页/)).toBeInTheDocument();
});
