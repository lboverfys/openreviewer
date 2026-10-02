// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api, ApiError, clearSettingsCache } from "./api";
import fixture from "./fixtures/review-details.json";
import ReviewDetailPage from "./ReviewDetailPage";
import ReviewRetryDialog from "./ReviewRetryDialog";
import { ModelBatchPanel } from "./ReviewProgressPanels";
import { agentProgress } from "./review-details";
import type { AuthUser, ReviewDetails } from "./types";

const details = {...fixture, phase: "failed", execution_status: "failed", workflow_status: "agent_batches",
  coverage_status: "partial", model_review_completed_at: null,
  available_actions: ["retry_failed_node", "retry_stage"],
  retry_impacts: {logic: {agent: "logic", model: "new-model", previous_models: ["old-model"], batch_count: 7, restart: true}},
} as unknown as ReviewDetails;
const user: AuthUser = {username: "tester", role: "administrator", authenticated: true,
  expires_at: "2099-01-01T00:00:00Z", permissions: ["reviews:view", "reviews:manage"]};
afterEach(() => {cleanup(); clearSettingsCache(); vi.restoreAllMocks();});

function page() {
  vi.spyOn(api, "reviewDetails").mockResolvedValue(details);
  vi.spyOn(api, "reviewRetrieval").mockResolvedValue([]);
  return render(<ReviewDetailPage user={user} reviewRunId={details.review_run_id} onBack={vi.fn()} onOpenReview={vi.fn()} onSignedOut={vi.fn()} />);
}

it("阶段重试在任务控制区打开紧凑弹窗，并把执行错误留在弹窗内", async () => {
  const action = vi.spyOn(api, "reviewAction").mockRejectedValue(new ApiError("当前配置暂不可用，请检查模型设置", 409));
  const confirm = vi.spyOn(window, "confirm");
  page();
  const trigger = await screen.findByRole("button", {name: "重新执行…"});
  expect(within(trigger.closest("section")!).getByText("任务控制")).toBeInTheDocument();
  fireEvent.click(trigger);
  const dialog = await screen.findByRole("dialog", {name: "重新执行审查"});
  expect(action).not.toHaveBeenCalled();
  fireEvent.change(within(dialog).getByRole("combobox", {name: "从哪一步开始"}), {target: {value: "aggregating"}});
  expect(within(dialog).getByText(/只重新生成汇总与问题列表/)).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", {name: "确认重新执行"}));
  await waitFor(() => expect(action).toHaveBeenCalledTimes(1));
  expect(action.mock.calls[0][1]).toBe("retry_stage");
  expect(action.mock.calls[0][3]).toBe("aggregating");
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("当前配置暂不可用");
  expect(confirm).not.toHaveBeenCalled();
  expect(document.querySelector(".app-notice")).toBeNull();
});

it("换模型时先展示整节点重跑范围，确认后才提交普通重试", async () => {
  const action = vi.spyOn(api, "reviewAction").mockRejectedValue(new ApiError("离线验证不启动模型", 409));
  page();
  fireEvent.click(await screen.findByRole("button", {name: /重试当前失败节点/}));
  const dialog = await screen.findByRole("dialog", {name: "重新执行失败节点"});
  expect(within(dialog).getByText("7 批全部重跑")).toBeInTheDocument();
  expect(within(dialog).getByText(/old-model → new-model/)).toBeInTheDocument();
  expect(action).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole("button", {name: "确认重新执行"}));
  await waitFor(() => expect(action).toHaveBeenCalledTimes(1));
  expect(action.mock.calls[0][4]).toMatchObject({retryScope: "failed_node", stateVersion: details.change_token});
});

it("请求进行中不能修改重审起点或重复提交", () => {
  const submit = vi.fn();
  render(<ReviewRetryDialog open onClose={vi.fn()} details={details} stage="agent_batches" onStageChange={vi.fn()}
    stageRetry busy error="" onConfirm={submit} />);
  expect(screen.getByRole("combobox")).toBeDisabled();
  expect(screen.getByRole("button", {name: "取消"})).toBeDisabled();
  fireEvent.click(screen.getByRole("button", {name: "正在提交…"}));
  expect(submit).not.toHaveBeenCalled();
});

it("七批完成后的 Agent 失败显示合并失败，并提供逻辑审查重跑入口", () => {
  const retry = vi.fn();
  render(<ModelBatchPanel details={{...details, events: [{id: "merge-failed", event_type: "review.model.agent_failed",
    occurred_at: "2026-10-02T04:25:47Z", payload: {agent: "logic", status: "failed", model_attempt_count: 2,
      error_code: "model_configuration_changed", error_message: "批次模型配置不一致"}}],
    batch_progress: {logic: {total: 7, completed: 7, failed: 0, running: 0}}}} onRetry={retry} />);
  expect(screen.getByText("结果合并失败")).toBeInTheDocument();
  expect(screen.getByText("批次已完成，结果合并失败")).toBeInTheDocument();
  expect(screen.queryByText("部分完成")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", {name: "重跑逻辑审查"}));
  expect(retry).toHaveBeenCalledWith("logic");
});

it("重试请求已接受时立即清除目标节点旧失败提示，保留其他节点完成状态", () => {
  const events: ReviewDetails["events"] = [
    {id:"security",event_type:"review.model.agent_completed",occurred_at:"2026-10-02T04:00:00Z",payload:{agent:"security",status:"completed",model_attempt_count:1}},
    {id:"logic",event_type:"review.model.agent_failed",occurred_at:"2026-10-02T04:00:01Z",payload:{agent:"logic",status:"failed",model_attempt_count:1,error:"旧错误"}},
    {id:"retry",event_type:"review.model.retry_requested",occurred_at:"2026-10-02T04:00:02Z",payload:{agent:null,target_agents:["logic"],model_attempt_count:2}},
  ];
  expect(agentProgress(events, "logic").status).toBe("planned");
  expect(agentProgress(events, "logic").errorMessage).toBeNull();
  expect(agentProgress(events, "security").status).toBe("completed");
});
