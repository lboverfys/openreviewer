// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { api, ApiError, clearSettingsCache } from "./api";
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

it.each(["paused", "cancelled"])("%s 状态不会沿用自动重试或实时 CI 提示", async phase => {
  vi.mocked(api.reviewDetails).mockResolvedValue({...details, phase,
    workflow_status: phase as ReviewDetails["workflow_status"], execution_status: "ready_for_review",
    model_attempt_count: 0, model_review_completed_at: null, ci_state: "pending", snapshot_review: false,
    events: [{id:"retry",event_type:"review.task.retry_scheduled",occurred_at:"2026-09-14T00:00:00Z",
      payload:{model_attempt_count:0,error_code:"retrieval_index_pending",retry_at:"2099-01-01T00:00:00Z"}}],
  });
  render(<ReviewDetailPage user={user} reviewRunId={details.review_run_id} onBack={vi.fn()} onOpenReview={vi.fn()} onSignedOut={vi.fn()} />);
  const metrics = within(await screen.findByRole("region", {name: "任务关键指标"}));
  expect(metrics.getByText(phase === "paused" ? "已暂停" : "已取消")).toBeInTheDocument();
  expect(metrics.getByText(phase === "paused" ? "已暂停跟进" : "已停止跟进")).toBeInTheDocument();
  expect(screen.queryByText(/等待自动重试/)).not.toBeInTheDocument();
  expect(screen.queryByText(/上一轮.*失败/)).not.toBeInTheDocument();
});

it("等待索引不会宣称模型调用失败", async () => {
  vi.mocked(api.reviewDetails).mockResolvedValue({...details, phase:"model_queued",
    workflow_status:"agent_batches", execution_status:"ready_for_review", model_attempt_count:0,
    model_review_completed_at:null,
    events:[{id:"retry",event_type:"review.task.retry_scheduled",occurred_at:"2026-09-14T00:00:00Z",
      payload:{model_attempt_count:0,error_code:"retrieval_index_pending",retry_at:"2099-01-01T00:00:00Z"}}],
  });
  render(<ReviewDetailPage user={user} reviewRunId={details.review_run_id} onBack={vi.fn()} onOpenReview={vi.fn()} onSignedOut={vi.fn()} />);
  await screen.findByText("正在准备关联代码索引，完成后自动继续 AI 检查");
  fireEvent.click(screen.getByRole("button", {name:/问题与结论/}));
  expect(await screen.findByText("正在准备关联代码")).toBeInTheDocument();
  expect(screen.queryByText(/AI 请求失败/)).not.toBeInTheDocument();
});

it("操作失败提示不会被随后成功的后台刷新清除", async () => {
  vi.mocked(api.reviewDetails).mockResolvedValue({...details, available_actions:["pause"]});
  const action = vi.spyOn(api, "reviewAction").mockRejectedValue(new ApiError("操作尚未成功，请重试", 409));
  render(<ReviewDetailPage user={{...user,permissions:["reviews:view","reviews:manage"]}} reviewRunId={details.review_run_id} onBack={vi.fn()} onOpenReview={vi.fn()} onSignedOut={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", {name:"暂停"}));
  await screen.findByText("操作尚未成功，请重试");
  fireEvent.click(screen.getByRole("button", {name:"↻ 刷新"}));
  await waitFor(() => expect(api.reviewDetails).toHaveBeenCalledTimes(2));
  expect(screen.getByText("操作尚未成功，请重试")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", {name:"关闭提示"}));
  expect(screen.queryByText("操作尚未成功，请重试")).not.toBeInTheDocument();
  action.mockRestore();
});

it("文件范围不足不会被显示成 Agent 未完成或汇总未执行", async () => {
  vi.mocked(api.reviewDetails).mockResolvedValue({...details, phase:"completed",coverage_status:"partial",
    model_review_completed_at:"2026-09-14T00:00:00Z",aggregation_status:"local",summary_status:"skipped",
    events:[{id:"summary",event_type:"review.model.summary_skipped",occurred_at:"2026-09-14T00:00:00Z",payload:{agent:"summary"}}],
  });
  render(<ReviewDetailPage user={user} reviewRunId={details.review_run_id} onBack={vi.fn()} onOpenReview={vi.fn()} onSignedOut={vi.fn()} />);
  await screen.findByText("部分文件未进入审查");
  fireEvent.click(screen.getByRole("button", {name:/AI 检查过程/}));
  expect(await screen.findByText("已完成程序汇总")).toBeInTheDocument();
  expect(screen.queryByText("上游 Agent 未完成，汇总未执行")).not.toBeInTheDocument();
});
