// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import RetrievalPage from "./RetrievalPage";
import RetrievalSettingsPanel from "./RetrievalSettingsPanel";
import RetrievalTracePanel from "./RetrievalTracePanel";
import type { RetrievalSettingsView } from "./types";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {...actual, api: {...actual.api, retrievalSettings: vi.fn(), retrievalIndexes: vi.fn(), retrievalEvaluations: vi.fn(), updateRetrievalSettings: vi.fn(), retrievalTargets: vi.fn(), retrievalOperations: vi.fn()}};
});
const settings: RetrievalSettingsView = {revision: 1, key_configured: true, tested: true, external_calls_paused: false, settings: {
  enabled: true, api_host: "https://sample.cn-beijing.maas.aliyuncs.com", embedding_model: "qwen3.7-text-embedding",
  rerank_model: "qwen3.7-text-rerank", dimensions: 1024, strategy: "reranked", candidate_k: 20, context_k: 8, timeout_seconds: 60, max_new_vectors_per_index: 100, max_requests_per_operation: 12,
}};
beforeEach(() => {
  vi.mocked(api.retrievalTargets).mockResolvedValue({items: [], next_cursor: null});
  vi.mocked(api.retrievalOperations).mockResolvedValue({pending_indexes: 0, available_indexes: 0, oldest_pending_seconds: 0, partial_indexes: 0, provider_busy: false, circuit_open: false});
  vi.mocked(api.retrievalSettings).mockResolvedValue(settings);
  vi.mocked(api.retrievalIndexes).mockResolvedValue({items: [], next_cursor: null});
  vi.mocked(api.retrievalEvaluations).mockResolvedValue({items: [], next_cursor: null});
});
afterEach(() => {cleanup(); vi.clearAllMocks();});

describe("代码检索页面", () => {
  it("慢配置和未打开的评测不阻塞索引区域，评测只在点击标签时读取", async () => {
    vi.mocked(api.retrievalSettings).mockReturnValue(new Promise(() => undefined));
    render(<RetrievalPage onSignedOut={vi.fn()} />);
    expect(screen.getByRole("button", {name: "执行检索"})).toBeInTheDocument();
    expect(api.retrievalEvaluations).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", {name: "检索效果（高级）"}));
    await waitFor(() => expect(api.retrievalEvaluations).toHaveBeenCalledTimes(1));
  });
  it("无索引和无评测时明确展示未就绪状态", async () => {
    render(<RetrievalPage onSignedOut={vi.fn()} />);
    await screen.findByText("建立索引后即可检索对应提交的代码。");
    expect(screen.getByRole("button", {name: "执行检索"})).toBeDisabled();
    fireEvent.click(screen.getByRole("button", {name: "检索效果（高级）"}));
    expect(screen.getByText(/尚未运行评测/)).toBeInTheDocument();
    expect(screen.queryByText("100%")).not.toBeInTheDocument();
  });
  it("保存后清空输入密钥，保留后端配置状态", async () => {
    vi.mocked(api.updateRetrievalSettings).mockResolvedValue({...settings, revision: 2, tested: false});
    render(<RetrievalSettingsPanel onError={vi.fn()} />);
    const key = await screen.findByLabelText("百炼密钥");
    fireEvent.change(key, {target: {value: "test-new-key"}});
    fireEvent.click(screen.getByRole("button", {name: "保存检索配置"}));
    await waitFor(() => expect(key).toHaveValue(""));
    expect(api.updateRetrievalSettings).toHaveBeenCalledWith({...settings.settings, external_calls_enabled: true}, 1, "test-new-key");
    expect(screen.getByText("已保存，下一次检索操作使用新配置。")).toBeInTheDocument();
  });
  it("未发生检索时不伪造 Token 或召回记录", () => {
    render(<RetrievalTracePanel traces={[]} />);
    expect(screen.getByText(/暂无检索记录/)).toBeInTheDocument();
  });
});


it("暂停时可建立基础索引，连接测试仍被禁止", async () => {
  vi.mocked(api.retrievalSettings).mockResolvedValue({...settings, external_calls_paused: true});
  render(<RetrievalPage onSignedOut={vi.fn()} initialReviewRunId="offline-review" />);
  await screen.findByText(/向量与精排调用已关闭/);
  expect(screen.getByRole("button", {name: "建立基础索引"})).toBeEnabled();
  expect(screen.getByRole("link", {name: "到模型配置开启 →"})).toHaveAttribute("href", "#settings?section=retrieval");
  cleanup();
  render(<RetrievalSettingsPanel onError={vi.fn()} />);
  await screen.findByRole("switch");
  expect(screen.getByRole("button", {name: "测试已保存的连接"})).toBeDisabled();
});

it("页面开关保存明确的开启值，不再依赖服务器默认暂停", async () => {
  vi.mocked(api.retrievalSettings).mockResolvedValue({...settings, external_calls_paused: true});
  vi.mocked(api.updateRetrievalSettings).mockResolvedValue({...settings, revision: 2,
    settings: {...settings.settings, external_calls_enabled: true}});
  render(<RetrievalSettingsPanel onError={vi.fn()} />);
  const toggle = await screen.findByRole("switch");
  expect(toggle).not.toBeChecked();
  fireEvent.click(toggle);
  fireEvent.click(screen.getByRole("button", {name: "保存检索配置"}));
  await waitFor(() => expect(api.updateRetrievalSettings).toHaveBeenCalledWith(
    expect.objectContaining({external_calls_enabled: true}), 1, undefined));
  await screen.findByText("向量与精排：允许调用 · 连接已验证");
});
