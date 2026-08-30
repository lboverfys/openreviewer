import { describe, expect, it } from "vitest";

import { appendReviewPage, applyLiveDashboardSnapshot } from "./dashboard";
import type {
  DashboardSnapshot,
  ExecutionStatus,
  ReviewItem,
} from "./types";

function review(id: string, status: ExecutionStatus = "queued"): ReviewItem {
  return {
    review_run_id: id,
    review_task_id: `task-${id}`,
    repository: "example/repository",
    pull_request_number: 1,
    head_sha: id.padEnd(40, "0"),
    execution_status: status,
    workflow_status: status,
    attempt_count: 0,
    max_attempts: 3,
    last_error: null,
    last_error_code: null,
    last_error_retryable: null,
    last_error_details: null,
    review_conclusion: null,
    coverage_status: "pending",
    model_review_completed_at: null,
    finding_count: 0,
    unreviewed_finding_count: 0,
    model_attempt_count: 0,
    created_at: "2026-08-28T00:00:00Z",
    updated_at: "2026-08-28T00:00:00Z",
    pr_title: null,
    pr_author_login: null,
    pr_html_url: null,
    head_repository: null,
    head_ref: null,
    base_repository: null,
    base_ref: null,
  };
}

function dashboard(
  reviews: ReviewItem[],
  total: number,
  nextCursor: string | null,
): DashboardSnapshot {
  return {
    generated_at: "2026-08-28T00:00:00Z",
    total_reviews: total,
    status_counts: {
      queued: 0,
      ci: 0,
      planning: 0,
      agent_batches: 0,
      aggregating: 0,
      awaiting_approval: 0,
      approved: 0,
      rejected: 0,
      awaiting_publish: 0,
      publishing: 0,
      paused: 0,
      waiting_for_ci: 0,
      running: 0,
      ready_for_review: 0,
      completed: 0,
      failed: 0,
      timed_out: 0,
      cancelled: 0,
      superseded: 0,
    },
    worker: {
      configured: false,
      online: false,
      worker_id: null,
      status: null,
      current_task_id: null,
      started_at: null,
      last_seen_at: null,
    },
    workers: [],
    recent_reviews: reviews,
    next_cursor: nextCursor,
  };
}

describe("dashboard pagination merging", () => {
  it("appends a page without duplicating its boundary item", () => {
    const current = dashboard([review("run-3"), review("run-2")], 3, "page-2");

    const merged = appendReviewPage(current, {
      total: 3,
      items: [review("run-2"), review("run-1")],
      next_cursor: null,
    });

    expect(merged.recent_reviews.map((item) => item.review_run_id)).toEqual([
      "run-3",
      "run-2",
      "run-1",
    ]);
    expect(merged.next_cursor).toBeNull();
  });

  it("keeps loaded older pages while live data replaces current rows", () => {
    const current = dashboard(
      [review("run-3"), review("run-2"), review("run-1")],
      4,
      "page-3",
    );
    const incoming = dashboard(
      [review("run-4"), review("run-3", "running")],
      4,
      "page-2",
    );

    const merged = applyLiveDashboardSnapshot(current, incoming);

    expect(merged.recent_reviews.map((item) => item.review_run_id)).toEqual([
      "run-4",
      "run-3",
      "run-2",
      "run-1",
    ]);
    expect(merged.recent_reviews[1].execution_status).toBe("running");
    expect(merged.next_cursor).toBeNull();
  });

  it("preserves the deepest loaded cursor when more rows remain", () => {
    const current = dashboard([review("run-3"), review("run-2")], 5, "page-3");
    const incoming = dashboard([review("run-4"), review("run-3")], 5, "page-2");

    const merged = applyLiveDashboardSnapshot(current, incoming);

    expect(merged.next_cursor).toBe("page-3");
  });
});
