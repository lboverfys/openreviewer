// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { clearReadCache } from "./api";
import TeamPage from "./TeamPage";

const timestamp = "2026-09-12T08:00:00Z";
const repository = {id: "repo-1", repository: "example/project", revision: 2,
  policy: {enabled: true, target_branches: ["main"], max_model_requests: 12},
  created_at: timestamp, updated_at: timestamp, updated_by: "admin"};
const member = {username: "reviewer", role: "viewer", enabled: true, revision: 3,
  scope: {repositories: ["example/project"], organizations: [], installation_ids: []},
  created_at: timestamp, updated_at: timestamp, updated_by: "admin"};
let saved: {path: string; body: Record<string, unknown>}[];
let failWrites: boolean;

beforeEach(() => {
  saved = []; failWrites = false; clearReadCache();
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (init?.method === "PUT" || init?.method === "POST") {
      saved.push({path, body: JSON.parse(String(init.body))});
      if (failWrites) return new Response(JSON.stringify({detail: "版本已变化，请刷新后重试"}), {status: 409});
      return new Response(JSON.stringify(path.includes("/members/") ? member : repository));
    }
    if (path.includes("/team/members")) {
      return new Response(JSON.stringify({configured_administrator: "admin", items: [member]}));
    }
    if (path.includes("/team/audits")) return new Response(JSON.stringify({items: []}));
    return new Response(JSON.stringify({items: [repository]}));
  }));
});
afterEach(() => {cleanup(); clearReadCache(); vi.unstubAllGlobals();});

it("编辑仓库时提交原版本与分支策略，保存后刷新列表", async () => {
  render(<TeamPage onSignedOut={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", {name: "编辑仓库 example/project"}));
  fireEvent.change(screen.getByLabelText(/目标分支（/), {target: {value: "main\nrelease/*"}});
  fireEvent.change(screen.getByLabelText(/单次审查最多模型请求数/), {target: {value: "8"}});
  fireEvent.click(screen.getByRole("button", {name: "保存仓库策略"}));
  await waitFor(() => expect(saved).toHaveLength(1));
  expect(saved[0].path).toBe("/api/v1/team/repositories/repo-1");
  expect(saved[0].body).toMatchObject({expected_revision: 2,
    policy: {target_branches: ["main", "release/*"], max_model_requests: 8, knowledge_sources: null}});
  await screen.findByText(/已保存。成员权限/);
});

it("停用成员携带当前版本，保留密码时不发送密码字段", async () => {
  render(<TeamPage onSignedOut={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", {name: "团队成员"}));
  fireEvent.click(await screen.findByRole("button", {name: "编辑成员 reviewer"}));
  fireEvent.click(screen.getByLabelText("启用账号"));
  fireEvent.click(screen.getByRole("button", {name: "保存成员"}));
  await waitFor(() => expect(saved).toHaveLength(1));
  expect(saved[0].body).toMatchObject({expected_revision: 3, enabled: false, role: "viewer"});
  expect(saved[0].body).not.toHaveProperty("password");
});

it("版本冲突时保留编辑内容并展示失败原因", async () => {
  failWrites = true;
  render(<TeamPage onSignedOut={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", {name: "编辑仓库 example/project"}));
  fireEvent.change(screen.getByLabelText(/目标分支（/), {target: {value: "release/*"}});
  fireEvent.click(screen.getByRole("button", {name: "保存仓库策略"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("版本已变化");
  expect(screen.getByLabelText(/目标分支（/)).toHaveValue("release/*");
});

it("普通成员登录过期时交还登录入口", async () => {
  vi.stubGlobal("fetch", vi.fn(async () =>
    new Response(JSON.stringify({detail: "authentication required"}), {status: 401})));
  const signedOut = vi.fn();
  render(<TeamPage onSignedOut={signedOut} />);
  await waitFor(() => expect(signedOut).toHaveBeenCalledWith("登录已失效，请重新登录"));
});
