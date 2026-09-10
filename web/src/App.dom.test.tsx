// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { lazy } from "react";
import { afterEach, expect, it, vi } from "vitest";

import { api, ApiError } from "./api";
import App from "./App";
import PageBoundary from "./PageBoundary";
import type { AuthUser } from "./types";

const loaded = vi.hoisted(() => ({ settings: vi.fn(), dashboard: vi.fn(), knowledge: vi.fn(), retrieval: vi.fn(), review: vi.fn() }));
vi.mock("./DashboardPage", () => {
  loaded.dashboard();
  return { default: () => <h1>测试仪表盘</h1> };
});
vi.mock("./SettingsPage", () => {
  loaded.settings();
  return { default: () => <h1>测试设置</h1> };
});
vi.mock("./KnowledgePage", () => {
  loaded.knowledge();
  return { default: () => <h1>测试知识库</h1> };
});
vi.mock("./RetrievalPage", () => {
  loaded.retrieval();
  return { default: ({ initialReviewRunId }: { initialReviewRunId?: string }) => <h1>测试检索 {initialReviewRunId}</h1> };
});
vi.mock("./ReviewDetailPage", () => {
  loaded.review();
  return { default: ({ reviewRunId }: { reviewRunId: string }) => <h1>测试审查 {reviewRunId}</h1> };
});
vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, api: { ...actual.api, me: vi.fn(), login: vi.fn() } };
});
vi.mock("./credentials", () => ({ loadSavedCredentials: async () => null, saveCredentials: vi.fn() }));

const user: AuthUser = { authenticated: true, username: "viewer", role: "viewer", permissions: ["reviews:view"], expires_at: "2030-01-01T00:00:00Z" };

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
  vi.restoreAllMocks();
});

it("登录与权限判断后才加载页面，并保留深链接导航", async () => {
  window.history.replaceState(null, "", "#settings");
  vi.mocked(api.me).mockRejectedValue(new ApiError("未登录", 401));
  vi.mocked(api.login).mockResolvedValue(user);
  render(<App />);
  await screen.findByLabelText("安全密码");
  expect(Object.values(loaded).every(loader => loader.mock.calls.length === 0)).toBe(true);
  fireEvent.change(screen.getByPlaceholderText("输入管理员账号"), { target: { value: "viewer" } });
  fireEvent.change(screen.getByLabelText("安全密码"), { target: { value: "test-password" } });
  fireEvent.click(screen.getByRole("button", { name: "进入审查控制台" }));
  await screen.findByRole("heading", { name: "测试仪表盘" });
  expect(loaded.settings).not.toHaveBeenCalled();
  expect(loaded.dashboard).toHaveBeenCalledTimes(1);

  act(() => { window.location.hash = "review/run%2F42"; });
  await screen.findByRole("heading", { name: "测试审查 run/42" });
  act(() => { window.location.hash = ""; });
  await screen.findByRole("heading", { name: "测试仪表盘" });
  expect(loaded.dashboard).toHaveBeenCalledTimes(1);
  expect(loaded.knowledge).not.toHaveBeenCalled();
  expect(loaded.retrieval).not.toHaveBeenCalled();
});

it("有权限的检索深链接直接加载目标页面", async () => {
  window.history.replaceState(null, "", "#retrieval/run%2F42");
  vi.mocked(api.me).mockResolvedValue({ ...user, permissions: ["knowledge:manage"] });
  render(<App />);
  await screen.findByRole("heading", { name: "测试检索 run/42" });
  expect(loaded.retrieval).toHaveBeenCalledTimes(1);
  expect(loaded.knowledge).not.toHaveBeenCalled();
});

it("页面资源加载失败后展示刷新入口，不自动反复请求", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  let reject!: (reason: Error) => void;
  const loader = vi.fn(() => new Promise<{ default: () => null }>((_, fail) => { reject = fail; }));
  const Page = lazy(loader);
  render(<PageBoundary><Page /></PageBoundary>);
  expect(screen.getByText(/正在唤醒/)).toBeInTheDocument();
  await act(async () => { reject(new Error("Chunk unavailable")); });
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("页面加载失败"));
  expect(screen.getByRole("button", { name: "刷新页面" })).toBeEnabled();
  expect(loader).toHaveBeenCalledTimes(1);
});
