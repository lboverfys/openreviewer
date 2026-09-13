// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { clearReadCache } from "./api";
import CreateReviewForm from "./CreateReviewForm";

afterEach(() => { cleanup(); clearReadCache(); vi.unstubAllGlobals(); });

it("相同手动请求失败重试复用标识，并保留输入", async () => {
  const keys: (string | null)[] = [];
  vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    keys.push(new Headers(init?.headers).get("Idempotency-Key"));
    return keys.length === 1 ? new Response(JSON.stringify({ detail: "请稍后重试" }), { status: 503 })
      : new Response(JSON.stringify({ review_task_id: "task-123456789" }));
  }));
  const created = vi.fn();
  render(<CreateReviewForm onCreated={created} onUnauthorized={vi.fn()} onCancel={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("Installation ID"), { target: { value: "123" } });
  fireEvent.change(screen.getByLabelText("Repository ID"), { target: { value: "456" } });
  fireEvent.change(screen.getByLabelText("Pull Request 编号"), { target: { value: "7" } });
  fireEvent.change(screen.getByLabelText("Head Commit SHA"), { target: { value: "a".repeat(40) } });
  fireEvent.click(screen.getByRole("button", { name: "提交审查任务" }));
  await screen.findByRole("alert");
  expect(screen.getByLabelText("Head Commit SHA")).toHaveValue("a".repeat(40));
  fireEvent.click(screen.getByRole("button", { name: "提交审查任务" }));
  await waitFor(() => expect(created).toHaveBeenCalled());
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBeTruthy();
  expect(keys[0]).toBe(keys[1]);
});
