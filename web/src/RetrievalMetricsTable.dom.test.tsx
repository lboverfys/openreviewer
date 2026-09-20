// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import RetrievalMetricsTable from "./RetrievalMetricsTable";
import type { RetrievalEvaluationReport } from "./types";

afterEach(cleanup);

const report: RetrievalEvaluationReport = {id:"report",index_id:"index",dataset_version:"fixture-v1",annotation_source:"synthetic_contract",
  embedding_model:"unused",rerank_model:"unused",generated_at:"2026-09-21T00:00:00Z",query_cache_mode:"not_used",lexical_cache_mode:"shared_warm",vector_search_mode:"not_used",real_review_accuracy:null,
  strategies:[{strategy:"bm25",sample_count:4,recall_at_k:0.75,mrr:0.75,k:8,median_duration_ms:2,p95_duration_ms:3,cases:[],
  splits:[{split:"development",sample_count:1,recall_at_k:1,mrr:1,k:8,median_duration_ms:1,p95_duration_ms:1},
    {split:"validation",sample_count:3,recall_at_k:2/3,mrr:2/3,k:8,median_duration_ms:2,p95_duration_ms:3}],model_requests:0,estimated_cost_microusd:0}]};

it("只显示选中划分的质量，保留全样本外部请求口径",()=>{
  const {rerender} = render(<RetrievalMetricsTable report={report} split="validation"/>);
  expect(screen.getByText("66.7%")).toBeInTheDocument();
  expect(screen.queryByText("75.0%")).not.toBeInTheDocument();
  expect(screen.getByText("全样本请求 / 估算费")).toBeInTheDocument();
  rerender(<RetrievalMetricsTable report={report} split="development"/>);
  expect(screen.getByText("100.0%")).toBeInTheDocument();
});

it("历史未分层和空分层不显示伪造的零分或满分",()=>{
  const empty = {...report,strategies:[{...report.strategies[0],splits:[{split:"validation" as const,sample_count:0,recall_at_k:null,mrr:null,k:8,median_duration_ms:null,p95_duration_ms:null}]}]};
  const {rerender} = render(<RetrievalMetricsTable report={empty} split="validation"/>);
  expect(screen.queryByText("0.0%")).not.toBeInTheDocument();
  expect(screen.queryByText("100.0%")).not.toBeInTheDocument();
  rerender(<RetrievalMetricsTable report={{...report,strategies:[{...report.strategies[0],splits:[]}]}} split="validation"/>);
  expect(screen.getByText("未记录分层")).toBeInTheDocument();
});
