// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import AppShell from "./AppShell";
import { StageTimeline } from "./ReviewProgressPanels";
import type { AuthUser, ReviewDetails } from "./types";

const user: AuthUser = { authenticated: true, username: "admin", role: "administrator",
  permissions: ["reviews:view", "settings:manage", "knowledge:manage"], expires_at: "2030-01-01T00:00:00Z" };
beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function(this: HTMLDialogElement) { this.open = true; } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function(this: HTMLDialogElement) { this.open = false; } });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal"); Reflect.deleteProperty(HTMLDialogElement.prototype, "close"); });

it("费用、问题和运行状态有独立入口，右上角打开完整手册", async () => {
  render(<AppShell user={user} view={{kind:"dashboard"}} onSignedOut={vi.fn()}><p>任务列表</p></AppShell>);
  const navigation = within(screen.getByRole("navigation", { name: "工作区导航" }));
  expect(navigation.getByRole("link", { name: "问题处理" })).toHaveAttribute("href", "#platform?tab=work");
  expect(navigation.getByRole("link", { name: "用量与费用" })).toHaveAttribute("href", "#platform?tab=usage");
  expect(navigation.getByRole("link", { name: "运行状态" })).toHaveAttribute("href", "#platform?tab=diagnostics");
  fireEvent.click(screen.getByRole("button", { name: "使用手册" }));
  const manual = await screen.findByRole("dialog", { name: "使用手册" });
  expect(within(manual).getByRole("heading", { name: "第一次使用" })).toBeInTheDocument();
  fireEvent.click(within(manual).getByRole("button", { name: "维护规则文档" }));
  expect(within(manual).getByText(/撤销移出/)).toBeInTheDocument();
  fireEvent.click(within(manual).getByRole("button", { name: "关闭" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("只读成员的导航不暴露管理入口", () => {
  render(<AppShell user={{...user,role:"viewer",permissions:["reviews:view"]}} view={{kind:"dashboard"}} onSignedOut={vi.fn()}><p>任务列表</p></AppShell>);
  expect(screen.queryByRole("link", { name: "项目与成员" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "用量与费用" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "使用手册" })).toBeInTheDocument();
});

it("取消任务不会把旧缓存中的当前步骤继续画成进行中", () => {
  render(<StageTimeline details={{phase:"cancelled",current_stage:"intake",stages:[
    {key:"intake",status:"current",started_at:null,completed_at:null,detail_code:"cancelled"},
    {key:"model",status:"pending",started_at:null,completed_at:null},
  ]} as ReviewDetails} />);
  expect(screen.queryByText("进行中")).not.toBeInTheDocument();
  expect(screen.getByText("任务已取消")).toBeInTheDocument();
});
