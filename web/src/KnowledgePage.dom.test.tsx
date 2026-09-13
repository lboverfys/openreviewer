// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { clearReadCache } from "./api";
import KnowledgePage from "./KnowledgePage";
import type { KnowledgeDocument } from "./types";

const now = "2026-09-14T00:00:00Z";
function doc(id: string, title: string, archived = false): KnowledgeDocument {
  return { id, title, source: id + ".md", content: "# " + title + "\n\n正文规则。", enabled: !archived,
    archived, current_version: 2, content_sha256: "a".repeat(64), byte_size: 64, repository_scope: null,
    created_by: "owner", updated_by: "owner", created_at: now, updated_at: now,
    versions: [{ version: 2, content_sha256: "a".repeat(64), byte_size: 64, created_by: "owner", created_at: now }] };
}
let documents: KnowledgeDocument[];
let revision: number;
let conflict: boolean;
let calls: { path: string; method: string; body: Record<string, unknown> | null }[];

beforeEach(() => {
  clearReadCache();
  documents = [doc("security", "安全规则"), doc("database", "数据库规则"), doc("retired", "旧规则", true)];
  revision = 5; conflict = false; calls = [];
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const path = url.pathname, method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ path: url.pathname + url.search, method, body });
    if (path === "/api/v1/knowledge/documents" && method === "GET") {
      const removedOnly = url.searchParams.get("archived_only") === "true";
      const includeRemoved = url.searchParams.get("include_archived") === "true";
      const items = documents.filter(item => removedOnly ? item.archived : includeRemoved || !item.archived);
      return Response.json({ revision, items, total: items.length, offset: 0, has_more: false,
        enabled_count: documents.filter(item => item.enabled).length, total_enabled_bytes: 128 });
    }
    const selected = documents.find(item => path.includes("/documents/" + item.id));
    if (!selected) return Response.json({ detail: "未找到文档" }, { status: 404 });
    if (method === "GET") return Response.json(selected);
    if (conflict || body.expected_revision !== revision) return Response.json({ detail: "文档已经被别人修改，请核对后再保存。" }, { status: 409 });
    if (path.endsWith("/archive")) { selected.archived = true; selected.enabled = false; }
    else if (path.endsWith("/restore")) { selected.archived = false; selected.enabled = body.enabled ?? false; }
    else Object.assign(selected, body);
    revision += 1;
    return Response.json({ revision, document: selected });
  }));
});
afterEach(() => { cleanup(); clearReadCache(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function page() {
  render(<KnowledgePage onSignedOut={vi.fn()} />);
  await screen.findByRole("button", { name: "暂停使用" });
  await waitFor(() => expect(screen.getByRole("button", { name: "移出列表" })).toBeEnabled());
}

it("移出后保留撤销入口，一次操作恢复原来的使用状态", async () => {
  await page();
  fireEvent.click(screen.getByRole("button", { name: "移出列表" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "撤销移出" })).toBeEnabled());
  expect(window.confirm).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "撤销移出" }));
  await screen.findByText("文档已恢复并开启使用。");
  expect(documents[0].archived).toBe(false);
  expect(documents[0].enabled).toBe(true);
  expect(calls.find(call => call.path.endsWith("/restore"))?.body).toMatchObject({ enabled: true, expected_revision: 6 });
});

it("已移出文档独立展示，恢复并使用后自动回到文档列表", async () => {
  await page();
  fireEvent.click(screen.getByRole("tab", { name: "已移出文档" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "恢复并使用" })).toBeEnabled());
  const list = within(screen.getByRole("complementary", { name: "知识文档列表" }));
  expect(list.queryByText("安全规则")).not.toBeInTheDocument();
  expect(list.getByText("旧规则")).toBeInTheDocument();
  expect(calls.some(call => call.path.includes("archived_only=true"))).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "恢复并使用" }));
  await screen.findByText("文档已恢复并开启使用。");
  expect(screen.getByRole("tab", { name: "文档列表" })).toHaveAttribute("aria-selected", "true");
  expect(calls.filter(call => call.method === "POST")).toHaveLength(1);
  expect(calls.some(call => call.method === "PUT")).toBe(false);
});

it("暂停和开启直接保存，日常操作不需要找额外保存按钮", async () => {
  await page();
  fireEvent.click(screen.getByRole("button", { name: "暂停使用" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "开启使用" })).toBeEnabled());
  expect(documents[0].enabled).toBe(false);
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "保存新版本" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "开启使用" }));
  await screen.findByText("已开启使用，AI 可以按相关性检索这份规则。");
  expect(documents[0].enabled).toBe(true);
});

it("仅修改仓库范围也能保存，冲突时保留未保存的编辑", async () => {
  await page();
  fireEvent.click(screen.getByRole("button", { name: "编辑文档" }));
  fireEvent.change(screen.getByLabelText("适用仓库"), { target: { value: "lboverfys/NiuMa" } });
  expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
  conflict = true;
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
  await screen.findByRole("alert");
  expect(screen.getByLabelText("适用仓库")).toHaveValue("lboverfys/NiuMa");
  expect(calls.find(call => call.method === "PUT")?.body?.repository_scope).toBe("lboverfys/NiuMa");
});

it("历史和检索默认收起，切换文档不会静默丢掉草稿", async () => {
  await page();
  expect(screen.queryByRole("textbox", { name: "规则关键词" })).not.toBeInTheDocument();
  expect(screen.queryByText("当前正文")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "编辑文档" }));
  fireEvent.change(screen.getByLabelText("规则正文"), { target: { value: "不要丢掉这份修改" } });
  vi.mocked(window.confirm).mockReturnValue(false);
  fireEvent.click(screen.getByRole("button", { name: /数据库规则/ }));
  expect(screen.getByLabelText("规则正文")).toHaveValue("不要丢掉这份修改");
  expect(window.confirm).toHaveBeenCalledOnce();
});
