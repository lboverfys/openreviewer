// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { api, clearReadCache } from "./api";
import ReviewDetailPage from "./ReviewDetailPage";
import fixture from "./fixtures/review-details.json";
import type { AuthUser, ReviewDetails } from "./types";

interface PendingRequest {
  url: URL;
  signal: AbortSignal;
  resolve: (response: Response) => void;
}

const original = fixture as unknown as ReviewDetails;
const reviewer = { username: "reviewer", role: "adjudicator", permissions: ["reviews:view", "findings:adjudicate"] } as AuthUser;
const finding = { ...original.findings[0], adjudication_status: "unreviewed" as const, reviewed_at: null, reviewed_by: null };
const reviewedAt = "2026-09-22T07:44:48Z";
let current: ReviewDetails;
let reads: PendingRequest[];
let writes: PendingRequest[];
let delayedDetails: PendingRequest[];
let delayReads: boolean;
let delayDetails: boolean;
let hasSecondPage: boolean;

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { "Content-Type": "application/json" },
});

function pending(url: URL, signal: AbortSignal, requests: PendingRequest[]) {
  return new Promise<Response>((resolve, reject) => {
    requests.push({ url, signal, resolve });
    signal.addEventListener("abort", () => reject(signal.reason), { once: true });
  });
}
function findingsResponse(url: URL) {
  const items = url.searchParams.get("adjudication_status") === "unreviewed"
    ? current.findings.filter(item => item.adjudication_status === "unreviewed") : current.findings;
  return json({ items, next_cursor: hasSecondPage && !url.searchParams.has("cursor") ? "page-2" : null });
}

beforeEach(() => {
  clearReadCache();
  reads = []; writes = []; delayedDetails = [];
  delayReads = false; delayDetails = false; hasSecondPage = false;
  current = { ...original, change_token: "before-decision", findings: [finding], finding_total_count: 1,
    unreviewed_finding_count: 1, valid_finding_count: 0, finding_next_cursor: null };
  vi.stubGlobal("fetch", vi.fn(async (input: string, init: RequestInit) => {
    const url = new URL(input, "http://localhost");
    if (init.method === "POST") return pending(url, init.signal!, writes);
    if (url.pathname.endsWith("/findings")) {
      if (delayReads) return pending(url, init.signal!, reads);
      return findingsResponse(url);
    }
    if (url.pathname.endsWith("/retrieval")) return json([]);
    if (url.pathname.endsWith("/events")) return json({ items: [], next_cursor: null });
    if (url.pathname.endsWith(current.review_run_id)) {
      if (delayDetails) return pending(url, init.signal!, delayedDetails);
      return json(current);
    }
    throw new Error(`未预期的请求：${input}`);
  }));
});
afterEach(() => {
  cleanup(); clearReadCache(); vi.useRealTimers(); vi.unstubAllGlobals();
});

async function openFindings() {
  render(<ReviewDetailPage user={reviewer} reviewRunId={current.review_run_id}
    onBack={vi.fn()} onOpenReview={vi.fn()} onSignedOut={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: /^问题与结论/ }));
  await screen.findByRole("button", { name: "有效问题" });
  await waitFor(() => expect(screen.queryByRole("status")).not.toBeInTheDocument());
}
async function saveDecision() {
  fireEvent.click(screen.getByRole("button", { name: "有效问题" }));
  expect(screen.getByRole("button", { name: "有效问题" })).toBeDisabled();
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(JSON.parse(vi.mocked(fetch).mock.calls.find(([, init]) => init?.method === "POST")![1]!.body as string))
    .toEqual({ decision: "valid" });
  current = { ...current, change_token: "after-decision", unreviewed_finding_count: 0, valid_finding_count: 1,
    findings: [{ ...finding, adjudication_status: "valid", reviewed_by: "reviewer", reviewed_at: reviewedAt }] };
  await act(async () => { writes[0].resolve(json(current)); });
}
async function finishRead(index: number) {
  await act(async () => { reads[index].resolve(findingsResponse(reads[index].url)); });
}

it.each([false, true])("裁决成功只刷新当前列表一次并保留裁决、数量和更新时间，第二页=%s", async secondPage => {
  hasSecondPage = secondPage;
  await openFindings();
  if (secondPage) {
    const pagination = screen.getByRole("navigation", { name: "审查问题分页" });
    fireEvent.click(within(pagination).getByRole("button", { name: "下一页" }));
    await screen.findByText(/第 2 页/);
    await waitFor(() => expect(within(pagination).queryByRole("status")).not.toBeInTheDocument());
  }
  delayReads = true;
  await saveDecision();
  expect(reads).toHaveLength(1);
  expect(reads[0].url.searchParams.get("cursor")).toBe(secondPage ? "page-2" : null);
  await finishRead(0);
  expect(screen.getByRole("button", { name: "有效问题" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByText(/由 reviewer 于 .* 更新/)).toBeInTheDocument();
  expect(within(screen.getByRole("region", { name: "任务关键指标" })).getByText("0 条待裁决")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("筛选待裁决时，保存后从当前筛选结果移除已裁决问题", async () => {
  await openFindings();
  fireEvent.change(screen.getByRole("combobox", { name: "人工裁决" }), { target: { value: "unreviewed" } });
  await waitFor(() => expect(screen.queryByRole("status")).not.toBeInTheDocument());
  delayReads = true;
  await saveDecision();
  expect(reads).toHaveLength(1);
  expect(reads[0].url.searchParams.get("adjudication_status")).toBe("unreviewed");
  await finishRead(0);
  expect(screen.queryByRole("button", { name: "有效问题" })).not.toBeInTheDocument();
  expect(screen.getByText("没有符合筛选条件的问题")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("裁决后的慢刷新被手动刷新替代时不误报，且新请求完成前保持加载状态", async () => {
  await openFindings();
  delayReads = true;
  await saveDecision();
  fireEvent.click(screen.getByRole("button", { name: "↻ 刷新" }));
  await waitFor(() => expect(reads.length).toBeGreaterThan(1));
  expect(reads[0].signal.aborted).toBe(true);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(within(screen.getByRole("navigation", { name: "审查问题分页" })).getByRole("status")).toBeInTheDocument();
  await finishRead(reads.length - 1);
  expect(screen.getByRole("button", { name: "有效问题" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("其他调用方替代共享列表请求时，正常取消不显示为裁决错误", async () => {
  await openFindings();
  delayReads = true;
  await saveDecision();
  let replacement!: ReturnType<typeof api.findingPage>;
  await act(async () => {
    replacement = api.findingPage(current.review_run_id, undefined, undefined, true);
  });
  expect(reads).toHaveLength(2);
  expect(reads[0].signal.aborted).toBe(true);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await finishRead(1);
  await replacement;
  expect(screen.getByRole("button", { name: "有效问题" })).toHaveAttribute("aria-pressed", "true");
});

it("切换详情标签取消在途刷新时不误报，返回问题页仍显示已保存裁决", async () => {
  await openFindings();
  delayReads = true;
  await saveDecision();
  fireEvent.click(screen.getByRole("button", { name: /运行日志/ }));
  await screen.findByRole("navigation", { name: "运行日志分页" });
  await waitFor(() => expect(reads.every(read => read.signal.aborted)).toBe(true));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  delayReads = false;
  fireEvent.click(screen.getByRole("button", { name: /^问题与结论/ }));
  await waitFor(() => expect(screen.getByRole("button", { name: "有效问题" })).toHaveAttribute("aria-pressed", "true"));
});

it("旧详情在裁决保存后才返回时，不覆盖新的待裁决数量", async () => {
  await openFindings();
  const stale = structuredClone(current);
  delayDetails = true;
  fireEvent.click(screen.getByRole("button", { name: "↻ 刷新" }));
  await waitFor(() => expect(delayedDetails).toHaveLength(1));
  await saveDecision();
  await act(async () => { delayedDetails[0].resolve(json(stale)); });
  expect(within(screen.getByRole("region", { name: "任务关键指标" })).getByText("0 条待裁决")).toBeInTheDocument();
});

it("真实保存失败仍显示错误，且不把未保存的裁决显示为成功", async () => {
  await openFindings();
  fireEvent.click(screen.getByRole("button", { name: "有效问题" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  await act(async () => { writes[0].resolve(json({ detail: "裁决保存失败，请重试" }, 503)); });
  expect(screen.getByRole("alert")).toHaveTextContent("裁决保存失败，请重试");
  expect(screen.getByRole("button", { name: "有效问题" })).toHaveAttribute("aria-pressed", "false");
  expect(screen.getByRole("button", { name: "有效问题" })).toBeEnabled();
  expect(within(screen.getByRole("region", { name: "任务关键指标" })).getByText("1 条待裁决")).toBeInTheDocument();
});

it("保存请求超时仍显示明确超时提示，不当作正常取消隐藏", async () => {
  await openFindings();
  vi.useFakeTimers();
  fireEvent.click(screen.getByRole("button", { name: "有效问题" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(writes[0].signal.aborted).toBe(true);
  expect(screen.getByRole("alert")).toHaveTextContent("请求超过 30 秒仍未完成");
  expect(screen.getByRole("button", { name: "有效问题" })).toHaveAttribute("aria-pressed", "false");
  expect(screen.getByRole("button", { name: "有效问题" })).toBeEnabled();
});
