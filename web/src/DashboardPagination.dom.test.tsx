// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { clearSettingsCache } from "./api";
import DashboardPage from "./DashboardPage";
import type { AuthUser, DashboardSnapshot, ReviewItem } from "./types";

const user = {username: "test", role: "viewer", permissions: ["reviews:view"]} as AuthUser;
function review(number: number): ReviewItem {
  return {review_run_id: `run-${number}`, review_task_id: `task-${number}`, repository: "sample/repo",
    pull_request_number: number, head_sha: "a".repeat(40), execution_status: "queued", workflow_status: "queued",
    attempt_count: 0, max_attempts: 3, finding_count: 0, unreviewed_finding_count: 0,
    updated_at: "2026-09-12T00:00:00Z", created_at: "2026-09-12T00:00:00Z"} as ReviewItem;
}
const snapshot = {generated_at: "2026-09-12T00:00:00Z", total_reviews: 13,
  status_counts: {}, worker: {configured: false}, workers: [],
  recent_reviews: Array.from({length: 10}, (_, index) => review(index + 1)), next_cursor: "next-page"} as unknown as DashboardSnapshot;

class Stream {
  static current: Stream;
  listeners = new Map<string, (event: MessageEvent) => void>();
  onopen = null;
  onerror = null;
  constructor() {Stream.current = this;}
  addEventListener(name: string, listener: (event: MessageEvent) => void) {this.listeners.set(name, listener);}
  close() {}
  emit(value: DashboardSnapshot) {this.listeners.get("dashboard")?.(new MessageEvent("dashboard", {data: JSON.stringify(value)}));}
}

beforeEach(() => {
  clearSettingsCache();
  vi.stubGlobal("EventSource", Stream);
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    const url = new URL(input, "http://localhost");
    const result = url.pathname.endsWith("dashboard") ? snapshot
      : url.searchParams.get("q") ? {items: [review(99)], total: 1, next_cursor: null}
      : {items: [review(11), review(12), review(13)], total: 13, next_cursor: null};
    return new Response(JSON.stringify(result), {headers: {"Content-Type": "application/json"}});
  }));
});
afterEach(() => {cleanup(); clearSettingsCache(); vi.unstubAllGlobals();});

it("不等实时连接便读取10条，翻页替换内容，实时消息不把第二页挤回首页", async () => {
  render(<DashboardPage user={user} onSignedOut={vi.fn()} onOpenReview={vi.fn()} />);
  await screen.findByText("PR #1");
  expect(document.querySelectorAll("tbody tr")).toHaveLength(10);
  expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1);
  expect(vi.mocked(fetch).mock.calls[0][0]).toContain("limit=10");
  const pagination = within(screen.getByRole("navigation", {name: "审查任务分页"}));
  fireEvent.click(pagination.getByRole("button", {name: "下一页"}));
  await screen.findByText("PR #11");
  expect(screen.queryByText("PR #1")).not.toBeInTheDocument();
  expect(document.querySelectorAll("tbody tr")).toHaveLength(3);
  act(() => Stream.current.emit({...snapshot, generated_at: "2026-09-12T00:00:01Z", recent_reviews: [review(88)]}));
  expect(screen.getByText("PR #11")).toBeInTheDocument();
  expect(screen.queryByText("PR #88")).not.toBeInTheDocument();
  fireEvent.click(pagination.getByRole("button", {name: "上一页"}));
  await screen.findByText("PR #88");
});

it("在后续页搜索会返回首页并请求服务端筛选，能找到当前页之外的任务", async () => {
  render(<DashboardPage user={user} onSignedOut={vi.fn()} onOpenReview={vi.fn()} />);
  await screen.findByText("PR #1");
  fireEvent.click(within(screen.getByRole("navigation", {name: "审查任务分页"})).getByRole("button", {name: "下一页"}));
  await screen.findByText("PR #11");
  fireEvent.change(screen.getByPlaceholderText(/至少3字/), {target: {value: "target"}});
  await screen.findByText("PR #99");
  expect(screen.getByText(/第 1 页/)).toBeInTheDocument();
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("q=target"))).toBe(true));
});
