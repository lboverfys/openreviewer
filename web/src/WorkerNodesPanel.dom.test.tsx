// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { clearReadCache } from "./api";
import WorkerNodesPanel from "./WorkerNodesPanel";

afterEach(() => { cleanup(); clearReadCache(); vi.unstubAllGlobals(); });

it("节点历史使用服务端分页，下一页替换当前页并显示保留策略", async () => {
  const requests: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://testserver"); requests.push(url.search);
    const offline = url.searchParams.get("state") === "offline";
    const second = url.searchParams.has("cursor");
    const first = second ? 11 : 1;
    const items = Array.from({ length: offline ? 10 : 1 }, (_, index) => ({
      worker_id: `${offline ? "old" : "live"}-${first + index}`, status: offline ? "stopping" : "idle", online: !offline,
      current_task_id: null, current_review_run_id: null, started_at: "2026-09-13T00:00:00Z", last_seen_at: "2026-09-13T00:00:00Z",
    }));
    return new Response(JSON.stringify({ items, next_cursor: offline && !second ? "next-workers" : null,
      generated_at: "2026-09-13T01:00:00Z", retention_days: 14, online_window_seconds: 15 }));
  }));
  render(<WorkerNodesPanel onError={vi.fn()} />);
  await screen.findByText("live-1");
  fireEvent.change(screen.getByLabelText("节点范围"), { target: { value: "offline" } });
  await screen.findByText("old-1");
  expect(document.querySelectorAll("tbody tr")).toHaveLength(10);
  fireEvent.click(within(screen.getByRole("navigation", { name: "Worker 节点分页" })).getByRole("button", { name: "下一页" }));
  await screen.findByText("old-11");
  expect(screen.queryByText("old-1")).not.toBeInTheDocument();
  expect(document.querySelectorAll("tbody tr")).toHaveLength(10);
  expect(requests.some(value => value.includes("cursor=next-workers") && value.includes("state=offline"))).toBe(true);
  expect(screen.getByText(/14 天保留策略/)).toBeInTheDocument();
});
