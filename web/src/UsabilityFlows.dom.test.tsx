// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { clearReadCache } from "./api";
import RepositoryConnectPanel from "./RepositoryConnectPanel";
import RetrievalHistoryPanel from "./RetrievalHistoryPanel";
import EvaluationOverviewPanel from "./EvaluationOverviewPanel";

afterEach(() => {cleanup(); clearReadCache(); vi.unstubAllGlobals();});

it("已授权全部仓库时直接检查接入，不再要求重复授权", async () => {
  const saved = vi.fn(), writes: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (init?.method === "POST") {writes.push(JSON.parse(String(init.body))); return Response.json({connected_at: "2026-09-14T00:00:00Z"});}
    if (path.includes("/repositories")) return Response.json({items: [{id: 42, repository: "owner/project"}], selection: "all", manage_url: "https://github.com/settings/installations/10", has_more: false});
    return Response.json({app_name: "已有 App", items: [{id: 10, account: "owner", selection: "all"}], authorize_url: "https://github.com/apps/existing/installations/new", has_more: false});
  }));
  render(<RepositoryConnectPanel onSaved={saved} onCancel={vi.fn()} onError={vi.fn()}/>);
  await screen.findByText("该账号的所有仓库已经授权，直接选择即可。");
  expect(screen.queryByRole("link", {name: "到 GitHub 补充仓库授权"})).not.toBeInTheDocument();
  expect(screen.getByRole("button", {name: "检查并启用审查"})).toBeDisabled();
  fireEvent.change(screen.getByLabelText("已授权的仓库"), {target: {value: "owner/project"}});
  fireEvent.click(screen.getByRole("button", {name: "检查并启用审查"}));
  await waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(writes[0]).toMatchObject({installation_id: 10, repository: "owner/project", policy: {enabled: true}});
});

it("搜索记录离开后可重新载入，并按查询与策略读取", async () => {
  const paths: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    paths.push(String(input));
    return Response.json({items: [{id: "history-1", index_id: "index-1", repository: "owner/project", head_sha: "a".repeat(40), query: "用户权限", strategy: "bm25", requested_strategy: "reranked", duration_ms: 13, created_at: "2026-09-14T00:00:00Z"}], next_cursor: null});
  }));
  const onError = vi.fn();
  const first = render(<RetrievalHistoryPanel indexId="index-1" onError={onError}/>);
  await screen.findByText("用户权限"); first.unmount();
  render(<RetrievalHistoryPanel indexId="index-1" onError={onError}/>);
  await screen.findByText("用户权限");
  fireEvent.change(screen.getByLabelText("查询原文（精确匹配）"), {target: {value: "用户权限"}});
  fireEvent.change(screen.getByLabelText("请求的策略"), {target: {value: "reranked"}});
  await waitFor(() => expect(paths.some(path => path.includes("strategy=reranked"))).toBe(true));
  expect(screen.getByRole("cell", {name: "关键词搜索"})).toBeInTheDocument();
});

it("单人统计明确展示不确定和未知费用，不虚构准确率", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({case_count: 1, observation_count: 1, reviewed_observations: 1,
    valid_findings: 1, false_positive_findings: 1, unreviewed_findings: 0, uncertain_findings: 1,
    missing_reference_cases: 1, model_duration_ms: 1200, estimated_cost_microusd: null, unpriced_observations: 1})));
  render(<EvaluationOverviewPanel datasetId="set-1" version={1} onError={vi.fn()}/>);
  await screen.findByText("1/1");
  expect(screen.getByText("暂不确定")).toBeInTheDocument();
  expect(screen.getByText("未记录")).toBeInTheDocument();
  expect(screen.queryByText("100%")).not.toBeInTheDocument();
  expect(screen.getByText(/来源：登录账号单人核对/)).toBeInTheDocument();
});
