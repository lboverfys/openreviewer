// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import RetrievalPage from "./RetrievalPage";
import RetrievalTracePanel from "./RetrievalTracePanel";
import type { AuthUser, RetrievalSettingsView } from "./types";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {...actual, api: {...actual.api, retrievalSettings: vi.fn(), retrievalIndexes: vi.fn(), retrievalEvaluations: vi.fn(), updateRetrievalSettings: vi.fn()}};
});
const user: AuthUser = { authenticated: true, username: "admin", role: "administrator", permissions: ["knowledge:manage"], expires_at: "2030-01-01T00:00:00Z" };
const settings: RetrievalSettingsView = {revision: 1, key_configured: true, tested: true, external_calls_paused: false, settings: {
  enabled: true, api_host: "https://sample.cn-beijing.maas.aliyuncs.com", embedding_model: "qwen3.7-text-embedding",
  rerank_model: "qwen3.7-text-rerank", dimensions: 1024, strategy: "reranked", candidate_k: 20, context_k: 8, timeout_seconds: 60, max_new_vectors_per_index: 100,
}};
beforeEach(() => {
  vi.mocked(api.retrievalSettings).mockResolvedValue(settings);
  vi.mocked(api.retrievalIndexes).mockResolvedValue([]);
  vi.mocked(api.retrievalEvaluations).mockResolvedValue([]);
});
afterEach(() => {cleanup(); vi.clearAllMocks();});

describe("代码检索页面", () => {
  it("无索引和无评测时明确展示未就绪状态", async () => {
    render(<RetrievalPage user={user} onBack={vi.fn()} onSignedOut={vi.fn()} />);
    await screen.findByText("建立索引后即可检索对应提交的代码。");
    expect(screen.getByRole("button", {name: "执行检索"})).toBeDisabled();
    fireEvent.click(screen.getByRole("button", {name: "评测对比"}));
    expect(screen.getByText(/尚未运行评测/)).toBeInTheDocument();
    expect(screen.queryByText("100%")).not.toBeInTheDocument();
  });
  it("保存后清空输入密钥，保留后端配置状态", async () => {
    vi.mocked(api.updateRetrievalSettings).mockResolvedValue({...settings, revision: 2, tested: false});
    render(<RetrievalPage user={user} onBack={vi.fn()} onSignedOut={vi.fn()} />);
    await screen.findByText("建立索引后即可检索对应提交的代码。");
    fireEvent.click(screen.getByRole("button", {name: "模型配置"}));
    const key = screen.getByLabelText("API Key");
    fireEvent.change(key, {target: {value: "test-new-key"}});
    fireEvent.click(screen.getByRole("button", {name: "保存配置"}));
    await waitFor(() => expect(key).toHaveValue(""));
    expect(api.updateRetrievalSettings).toHaveBeenCalledWith(settings.settings, 1, "test-new-key");
    expect(screen.getByText("检索配置已保存")).toBeInTheDocument();
  });
  it("未发生检索时不伪造 Token 或召回记录", () => {
    render(<RetrievalTracePanel traces={[]} />);
    expect(screen.getByText(/暂无检索记录/)).toBeInTheDocument();
  });
});


it("暂停时禁止排队索引和连接测试", async () => {
  vi.mocked(api.retrievalSettings).mockResolvedValue({...settings, external_calls_paused: true});
  render(<RetrievalPage user={user} onBack={vi.fn()} onSignedOut={vi.fn()} initialReviewRunId="offline-review" />);
  await screen.findByText(/服务器已暂停真实向量与精排请求/);
  expect(screen.getByRole("button", {name: "为此提交建立索引"})).toBeDisabled();
  fireEvent.click(screen.getByRole("button", {name: "模型配置"}));
  expect(screen.getByRole("button", {name: "测试已保存的连接"})).toBeDisabled();
});
