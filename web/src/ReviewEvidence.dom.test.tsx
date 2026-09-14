// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { clearReadCache } from "./api";
import ProfileActivationPanel from "./ProfileActivationPanel";
import StaticAnalysisPanel from "./StaticAnalysisPanel";

afterEach(() => { cleanup(); clearReadCache(); vi.unstubAllGlobals(); });

it("未验证方案需要理由，启用冲突后保留输入和证据版本", async () => {
  const onError = vi.fn(), onDone = vi.fn();
  const posts: Record<string, unknown>[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") { posts.push(JSON.parse(String(init.body))); return new Response(JSON.stringify({ detail: "评测已变化" }), { status: 409 }); }
    if (String(input).includes("/quality")) return new Response(JSON.stringify({ profile_id: "p1", baseline_profile_id: null, status: "unverified", evidence_token: "a".repeat(64), reasons: ["待人工复核"], report: null }));
    return new Response(JSON.stringify({ items: [], next_cursor: null }));
  }));
  render(<ProfileActivationPanel id="p1" repository="owner/repo" revision={2} onDone={onDone} onError={onError} onCancel={vi.fn()} />);
  await screen.findByText("质量状态：未验证");
  const reason = screen.getByLabelText("人工启用理由");
  expect(reason).toBeRequired();
  fireEvent.change(reason, { target: { value: "先在指定仓库试用" } });
  fireEvent.click(screen.getByRole("button", { name: "确认启用方案" }));
  await waitFor(() => expect(onError).toHaveBeenCalled());
  expect(reason).toHaveValue("先在指定仓库试用");
  expect(posts[0]).toMatchObject({ reason: "先在指定仓库试用", evidence_token: "a".repeat(64), expected_repository_revision: 2 });
  expect(onDone).not.toHaveBeenCalled();
});

it("静态报告按需读取，只读成员看不到导入按钮", async () => {
  const fetcher = vi.fn(async () => new Response("null"));
  vi.stubGlobal("fetch", fetcher);
  render(<StaticAnalysisPanel runId="run1" headSha={"a".repeat(40)} editable={false} onError={vi.fn()} />);
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", {name:/扫描工具报告（可选）/}));
  await screen.findByText("尚未导入静态报告。");
  expect(screen.queryByRole("button", { name: "导入静态报告" })).not.toBeInTheDocument();
});

it("没有基线的静态线索保持待判断，同位置 AI 只展示为线索", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(
    String(input).includes("static-findings") ? { items: [{ id: "s1", rule_id: "sql-rule", file: "src/Mapper.xml", start_line: 2, end_line: 2, message: "检查参数", baseline_state: "unknown", overlapping_ai_count: 1 }], next_cursor: null }
      : { id: "report", tool: "Semgrep", tool_version: "1.0", new_count: 0, existing_count: 0, unknown_count: 1, imported_by: "reviewer", report_hash: "a".repeat(64) }
  ))));
  render(<StaticAnalysisPanel runId="run2" headSha={"a".repeat(40)} editable onError={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", {name:/扫描工具报告（可选）/}));
  await screen.findByText(/新增状态待判断/);
  expect(screen.getByText(/同位置 AI 问题 1 条/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "导入静态报告" })).not.toBeInTheDocument();
});
