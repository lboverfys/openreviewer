import { describe, expect, it } from "vitest";

import {
  agentProgress,
  appendFindingPage,
  applyRefreshedFindingPage,
  eventDetail,
  isErrorEvent,
  latestBatchPlanEvent,
  retryDetail,
} from "./review-details";
import type { ReviewDetails, ReviewEvent, ReviewFinding } from "./types";

function finding(id: string, title = id): ReviewFinding {
  return { id, title } as ReviewFinding;
}

function details(
  ids: string[],
  total: number,
  nextCursor: string | null,
): ReviewDetails {
  return {
    review_run_id: "run-1",
    findings: ids.map((id) => finding(id)),
    finding_total_count: total,
    finding_next_cursor: nextCursor,
  } as ReviewDetails;
}

describe("Finding 详情分页合并", () => {
  it("自动刷新第一页时保留已加载的后续页和最深游标", () => {
    const current = details(["1", "2", "3", "4"], 6, "after-4");
    const refreshed = details(["1", "2"], 6, "after-2");
    refreshed.findings[0] = finding("1", "已刷新");

    const merged = applyRefreshedFindingPage(current, refreshed);

    expect(merged.findings.map((item) => item.id)).toEqual(["1", "2", "3", "4"]);
    expect(merged.findings[0].title).toBe("已刷新");
    expect(merged.finding_next_cursor).toBe("after-4");
  });

  it("追加下一页时去重并采用服务端返回的新游标", () => {
    const current = details(["1", "2"], 4, "after-2");
    const next = details(["2", "3", "4"], 4, null);

    const merged = appendFindingPage(current, next);

    expect(merged.findings.map((item) => item.id)).toEqual(["1", "2", "3", "4"]);
    expect(merged.finding_next_cursor).toBeNull();
  });
});

function event(
  id: string,
  eventType: string,
  payload: Record<string, unknown> = {},
): ReviewEvent {
  return {
    id,
    event_type: eventType,
    payload,
    occurred_at: "2026-08-28T00:00:00Z",
  };
}

describe("审查详情事件解释", () => {
  it("选择模型尝试次数最大的批次计划", () => {
    const oldPlan = event("old", "review.model.batches_planned", {
      model_attempt_count: 1,
    });
    const newPlan = event("new", "review.model.batches_planned", {
      model_attempt_count: 3,
    });

    expect(latestBatchPlanEvent([newPlan, oldPlan])).toBe(newPlan);
  });

  it("聚合最新 Agent 尝试，忽略旧尝试并去重引用", () => {
    const events = [
      event("old", "review.model.agent_completed", {
        agent: "security",
        model_attempt_count: 1,
        finding_count: 99,
      }),
      event("plan", "review.model.batches_planned", {
        agent: "security",
        model_attempt_count: 2,
        batch_count: 1,
      }),
      event("started", "review.model.batch_started", {
        agent: "security",
        model_attempt_count: 2,
        batch_number: 1,
      }),
      event("completed", "review.model.batch_completed", {
        agent: "security",
        model_attempt_count: 2,
        batch_number: 1,
        finding_count: 2,
        input_tokens: 100,
        output_tokens: 20,
        references: ["rule-a"],
      }),
      event("terminal", "review.model.agent_completed", {
        agent: "security",
        model_attempt_count: 2,
        finding_count: 2,
        verdict: "issues_found",
        summary: "发现两个问题",
        checked_areas: ["authentication"],
        references: ["rule-a", "rule-b"],
      }),
    ];

    const progress = agentProgress(events, "security");

    expect(progress.status).toBe("completed");
    expect(progress.findingCount).toBe(2);
    expect(progress.inputTokens).toBe(100);
    expect(progress.references).toEqual(["rule-a", "rule-b"]);
    expect(progress.hasStructuredConclusion).toBe(true);
    expect(progress.events.some((item) => item.id === "old")).toBe(false);
  });

  it("以批次失败终态标记 Agent，并保留可展示的错误信息", () => {
    const progress = agentProgress([
      event("plan", "review.model.batches_planned", {
        agent: "logic",
        model_attempt_count: 1,
        batch_count: 1,
      }),
      event("failed", "review.model.batch_failed", {
        agent: "logic",
        model_attempt_count: 1,
        batch_number: 1,
        error_code: "MODEL_TIMEOUT",
        error_message: "模型请求超时",
      }),
    ], "logic");

    expect(progress.status).toBe("failed");
    expect(progress.errorCode).toBe("MODEL_TIMEOUT");
    expect(progress.errorMessage).toBe("模型请求超时");
  });

  it("用固定当前时间生成稳定的重试提示", () => {
    const retry = event("retry", "review.task.retry_scheduled", {
      retry_at: "2026-08-28T00:01:30Z",
    });

    expect(retryDetail(retry, Date.parse("2026-08-28T00:00:00Z")))
      .toContain("约 2 分钟后自动重试");
    expect(retryDetail(retry, Date.parse("2026-08-28T00:02:00Z")))
      .toBe("已到重试时间，等待 Worker 领取");
  });

  it("识别携带错误信息的事件并生成批次失败详情", () => {
    const failed = event("failed", "review.model.batch_failed", {
      batch_number: 2,
      batch_count: 3,
      status_code: 504,
      duration_ms: 1_500,
      error_code: "UPSTREAM_TIMEOUT",
      error_retryable: true,
    });

    expect(isErrorEvent(failed)).toBe(true);
    expect(eventDetail(failed)).toContain("HTTP 504");
    expect(eventDetail(failed)).toContain("可自动重试");
  });
});
