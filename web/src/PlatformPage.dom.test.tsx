// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { clearReadCache } from "./api";
import PlatformPage from "./PlatformPage";
import type { AuthUser, UsageRequest } from "./types";

const now = "2026-09-13T01:00:00Z";
const admin = { username: "admin", role: "administrator", permissions: ["reviews:view", "settings:manage", "findings:adjudicate", "reviews:approve", "knowledge:manage"], expires_at: now } as AuthUser;
const item = { id: "work-1", source_run_id: "run-1", source_finding_id: "finding-1", repository: "example/project", pull_request_number: 10, title: "资源归属校验缺失", severity: "high", status: "open", assignee: "admin", due_at: null, note: "", fix_pull_request_number: null, revision: 1, created_at: now, updated_at: now };
let calls: { path: string; method: string; body: Record<string, unknown> | null }[];
let conflict: boolean;
let usageRequests: UsageRequest[];

beforeEach(() => {
  clearReadCache(); calls = []; conflict = false; usageRequests = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input), method = init?.method ?? "GET";
    calls.push({ path, method, body: init?.body ? JSON.parse(String(init.body)) : null });
    if (method === "PUT") return conflict ? new Response(JSON.stringify({ detail: "工作项已被其他成员修改" }), { status: 409 }) : new Response(JSON.stringify({ ...item, revision: 2 }));
    if (method === "POST") return new Response(JSON.stringify(item));
    if (path.includes("/work-items")) return new Response(JSON.stringify({ items: [item] }));
    if (path.includes("/approvals")) return new Response(JSON.stringify({ items: [] }));
    if (path.includes("/breakdown")) return new Response(JSON.stringify([{agent:path.includes("group_by=agent") ? "logic" : null, model:"fixture-model", purpose:"review", request_count:2, estimated_cost_microusd:100, unknown_count:1, known_count:1, known_cost_share:1, settled_priced_count:1, settled_reservation_microusd:150, settled_cost_microusd:100, missing_price_count:1}]));
    if (path.includes("/requests")) return new Response(JSON.stringify({ items: usageRequests }));
    if (path.includes("/usage?")) return new Response(JSON.stringify({ items: [{ id: "month-1", repository: "example/project", installation_id: 1, month: "2026-09", request_count: 2, estimated_cost_microusd: 100, reserved_cost_microusd: 60, input_tokens: 10, output_tokens: 5, unknown_count: 1, uncertain_count: 1, budget_microusd: 200, warning_percent: 80, warning: true, created_at: now }] }));
    if (path.includes("/diagnostics")) return new Response(JSON.stringify({ repositories: [], failures: [], provider_channels: [], since: now, until: now }));
    return new Response(JSON.stringify({ items: [] }));
  }));
});
afterEach(() => { cleanup(); clearReadCache(); vi.unstubAllGlobals(); });

it("只读成员只能查看待办，不能进入费用与配置页面", async () => {
  render(<PlatformPage user={{ ...admin, role: "viewer", permissions: ["reviews:view"] }} onSignedOut={vi.fn()} />);
  await screen.findByText("资源归属校验缺失");
  expect(screen.queryByRole("button", { name: "用量与预算" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "处理" })).not.toBeInTheDocument();
  expect(screen.queryByText("加入我的待办")).not.toBeInTheDocument();
});

it("从审查来源创建待办时，负责人使用当前登录成员", async () => {
  render(<PlatformPage user={admin} findingId="finding-2" onSignedOut={vi.fn()} />);
  await screen.findByText("资源归属校验缺失");
  fireEvent.click(screen.getByRole("button", { name: "加入我的待办" }));
  await waitFor(() => expect(calls.some(call => call.method === "POST")).toBe(true));
  expect(calls.find(call => call.method === "POST")?.body).toEqual({ finding_id: "finding-2", assignee: "admin" });
});

it("工作项冲突保留处理说明和原版本，避免覆盖别人修改", async () => {
  conflict = true;
  render(<PlatformPage user={admin} onSignedOut={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "处理" }));
  fireEvent.change(screen.getByLabelText("处理说明"), { target: { value: "正在核查事务边界" } });
  fireEvent.click(screen.getByRole("button", { name: "保存处理结果" }));
  await screen.findByText("工作项已被其他成员修改");
  expect(screen.getByLabelText("处理说明")).toHaveValue("正在核查事务边界");
  expect(calls.find(call => call.method === "PUT")?.body).toMatchObject({ expected_revision: 1, note: "正在核查事务边界" });
});

it("费用页面区分已估算费用、保留预占和未知请求", async () => {
  render(<PlatformPage user={admin} initialTab="usage" onSignedOut={vi.fn()} />);
  await screen.findByText("已达到预算提醒线");
  expect(screen.getByText("$0.000100")).toBeInTheDocument();
  expect(screen.getByText("费用未知 1 · 请求不确定 1")).toBeInTheDocument();
  expect(screen.getByText("待确认预占")).toBeInTheDocument();
});

it("请求汇总可以切到角色并保留未知费用与同批预占口径", async () => {
  render(<PlatformPage user={admin} initialTab="usage" onSignedOut={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", {name:"查看请求"}));
  fireEvent.click(await screen.findByRole("button", {name:/按模型、用途与角色汇总/}));
  await screen.findByText("fixture-model");
  fireEvent.change(screen.getByLabelText("费用分组"), {target:{value:"agent"}});
  await screen.findByText("逻辑");
  expect(calls.some(call => call.path.includes("group_by=agent"))).toBe(true);
  expect(screen.getByText(/已知 1 次 \/ 未知 1 次/)).toBeInTheDocument();
  expect(screen.getByText(/预占 \$0.000150 \/ 估算 \$0.000100/)).toBeInTheDocument();
  expect(screen.getByText("费用待确认原因：缺少单价 1 次")).toBeInTheDocument();
  expect(screen.queryByText(/已结算/)).not.toBeInTheDocument();
});

const usageRequest: UsageRequest = {
  id: "request-1", review_run_id: "run-1", repository: "example/project", agent: "logic", purpose: "review",
  model: "same-model", status: "settled", estimated_cost_microusd: null, reserved_cost_microusd: 150,
  input_tokens: 100, output_tokens: 20, duration_ms: 100, response_status: 200, created_at: now,
  cost_reason: "cache_price_missing", usage_status: "recorded", cost_status: "unknown",
  pricing_snapshot: {input_usd_per_million: "2", output_usd_per_million: "10", cache_read_usd_per_million: null},
  usage_details: {input_tokens: 80, output_tokens: 20, cache_read_input_tokens: 20, cache_write_input_tokens: 0, reasoning_output_tokens: 0},
};

it("缺少单价和缺少用量分别展示，HTTP 成功不会冒充费用已确认", async () => {
  usageRequests = [usageRequest, {...usageRequest, id: "request-2", status: "uncertain", usage_status: "missing",
    input_tokens: null, output_tokens: null, usage_details: null, cost_reason: "usage_missing"}];
  render(<PlatformPage user={admin} initialTab="usage" onSignedOut={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", {name: "查看请求"}));
  const missingPrice = await screen.findByText("调用时未配置缓存单价");
  const row = within(missingPrice.closest("tr")!);
  expect(row.getByText("用量已记录")).toBeInTheDocument();
  expect(row.getByText("待确认")).toBeInTheDocument();
  const missingUsage = within(screen.getByText("未返回完整用量").closest("tr")!);
  expect(missingUsage.getByText("HTTP 200")).toBeInTheDocument();
  expect(missingUsage.getByText("用量缺失")).toBeInTheDocument();
  expect(screen.queryByText(/已结算/)).not.toBeInTheDocument();
  fireEvent.click(row.getByRole("button", {name: "计费依据"}));
  const details = within(screen.getByRole("dialog", {name: "计费依据"}));
  expect(details.getByText("普通输入单价：2")).toBeInTheDocument();
  expect(details.getByText("缓存读取单价：未配置")).toBeInTheDocument();
  expect(details.getByText(/普通输入 80 Token；缓存读取 20 Token/)).toBeInTheDocument();
});

it("历史记录保留未知原因，部分估算不会标为完整金额", async () => {
  usageRequests = [{...usageRequest, pricing_snapshot: null, usage_details: null, cost_reason: "legacy_unknown"},
    {...usageRequest, id: "request-2", status: "uncertain", usage_status: "partial", cost_status: "partial",
      estimated_cost_microusd: 100, cost_reason: "incomplete_response"}];
  render(<PlatformPage user={admin} initialTab="usage" onSignedOut={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", {name: "查看请求"}));
  const historical = within((await screen.findByText("历史记录未保存费用缺失原因")).closest("tr")!);
  expect(historical.getByText("待确认")).toBeInTheDocument();
  expect(historical.queryByRole("button", {name: "计费依据"})).not.toBeInTheDocument();
  const partial = within(screen.getByText("部分估算").closest("tr")!);
  expect(partial.getByText("$0.000100")).toBeInTheDocument();
  expect(partial.getByText("仅有部分用量")).toBeInTheDocument();
});

it("审批筛选只发出对应待办请求", async () => {
  render(<PlatformPage user={admin} onSignedOut={vi.fn()} />);
  await screen.findByText("资源归属校验缺失");
  fireEvent.change(screen.getByLabelText("类型"), { target: { value: "approvals" } });
  fireEvent.click(screen.getByLabelText("已超期"));
  await waitFor(() => expect(calls.some(call => call.path.includes("/approvals?") && call.path.includes("overdue=true"))).toBe(true));
});

it("退出独立处理视图后保留待办筛选", async () => {
  render(<PlatformPage user={admin} onSignedOut={vi.fn()} />);
  await screen.findByText("资源归属校验缺失");
  fireEvent.click(screen.getByLabelText("只看我的待办"));
  fireEvent.change(screen.getByLabelText("处理状态"), { target: { value: "in_progress" } });
  fireEvent.click(await screen.findByRole("button", { name: "处理" }));
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(screen.getByLabelText("处理状态")).toHaveValue("in_progress");
  expect(screen.getByLabelText("只看我的待办")).not.toBeChecked();
  expect(calls.some(call => call.method === "PUT")).toBe(false);
});
