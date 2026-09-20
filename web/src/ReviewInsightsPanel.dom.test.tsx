// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import ReviewInsightsPanel from "./ReviewInsightsPanel";
import type { DiagnosticReport } from "./types";

afterEach(cleanup);
const data: NonNullable<DiagnosticReport["insights"]> = {
  requests: {request_count:3, estimated_cost_microusd:100, known_count:1, unknown_count:2, duration_sample_count:2,http_2xx_count:1,http_non_2xx_count:1,http_unknown_count:1,reserved_count:1,uncertain_count:1,settled_count:1,settled_priced_count:1,settled_reservation_microusd:150,settled_cost_microusd:100},
  completed_cost: {completed_runs:2, priced_runs:1, missing_ledger_runs:1, incomplete_cost_runs:0, mean_estimated_cost_microusd:100, mean_turnaround_ms:null},
  batches: {total:3,pending:0,running:0,succeeded:2,failed:1,claimed:2,reclaimed:1,terminal_claimed:2,single_claim_succeeded:1,reused_batches:1,estimated_avoided_input_tokens:42,reused_input_unknown_batches:0},
  batch_errors:[], batch_errors_truncated:false, evidence_reasons_truncated:false, evidence:{total:4,matched:1,unmatched:1,infrastructure:1,not_covered:0,unclassified:1,automatic_coverage:0.5,eligible_pass_rate:0.5},
  evidence_reasons:[{status:"unverified",reason:"source_blob_unavailable",category:"infrastructure",count:1}],
  retrieval_cache:{groups:3,query_recorded_groups:2,query_all_hit_groups:1,rerank_recorded_groups:0,rerank_all_hit_groups:0},
  index_reuse:{indexes:1,parsed_files:2,reused_files:3,embedded_vectors:5,reused_vectors:7},
};

it("保留覆盖率分母、跨月均价条件与组级复用说明", () => {
  render(<ReviewInsightsPanel data={data} />);
  expect(screen.getByText(/核验覆盖率 50.0%（可匹配 2 \/ 全部 4）/)).toBeInTheDocument();
  expect(screen.getByText(/缺请求账本 1 次/)).toBeInTheDocument();
  expect(screen.getByText(/查询缓存 1\/2 组全命中/)).toBeInTheDocument();
  expect(screen.getByText(/不能直接表示输出 Token、耗时或实际账单节省/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", {name:/引用核验原因/}));
  expect(screen.getByText("来源或基础设施异常")).toBeInTheDocument();
});

it("缺少完成样本的费用不会被显示为零", () => {
  render(<ReviewInsightsPanel data={{...data, completed_cost:{...data.completed_cost, priced_runs:0, mean_estimated_cost_microusd:null}}} />);
  expect(screen.getByText(/平均估算费用 未记录/)).toBeInTheDocument();
});
