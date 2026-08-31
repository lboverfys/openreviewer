import type {
  DashboardSnapshot,
  ExecutionStatus,
  ReviewItem,
  ReviewListPage,
} from "./types";

export type DashboardStreamState = "connecting" | "live" | "reconnecting";

export interface DashboardRefreshOptions {
  signal?: AbortSignal;
  force?: boolean;
  preserveLiveSnapshot?: boolean;
}

export const DASHBOARD_INITIAL_FALLBACK_MS = 3_500;
export const DASHBOARD_FALLBACK_REFRESH_MS = 15_000;

export const DASHBOARD_STATUS_ORDER: ExecutionStatus[] = [
  "queued",
  "running",
  "waiting_for_ci",
  "ready_for_review",
  "completed",
  "failed",
];

function uniqueReviews(groups: readonly (readonly ReviewItem[])[]): ReviewItem[] {
  const seen = new Set<string>();
  const merged: ReviewItem[] = [];
  for (const group of groups) {
    for (const review of group) {
      if (seen.has(review.review_run_id)) continue;
      seen.add(review.review_run_id);
      merged.push(review);
    }
  }
  return merged;
}

export function applyLiveDashboardSnapshot(
  current: DashboardSnapshot | null,
  incoming: DashboardSnapshot,
): DashboardSnapshot {
  if (!current) return incoming;

  // HTTP refresh and SSE can complete out of order.  Both snapshots carry the
  // server generation time, so an event that was generated earlier must not
  // replace the state already rendered from a newer snapshot.
  const currentGeneratedAt = Date.parse(current.generated_at);
  const incomingGeneratedAt = Date.parse(incoming.generated_at);
  if (
    Number.isFinite(currentGeneratedAt)
    && Number.isFinite(incomingGeneratedAt)
    && incomingGeneratedAt < currentGeneratedAt
  ) {
    return current;
  }

  const recentReviews = uniqueReviews([
    incoming.recent_reviews,
    current.recent_reviews,
  ]);
  return {
    ...incoming,
    recent_reviews: recentReviews,
    next_cursor:
      recentReviews.length >= incoming.total_reviews
        ? null
        : current.next_cursor ?? incoming.next_cursor,
  };
}

export function appendReviewPage(
  current: DashboardSnapshot,
  page: ReviewListPage,
): DashboardSnapshot {
  return {
    ...current,
    total_reviews: page.total,
    recent_reviews: uniqueReviews([current.recent_reviews, page.items]),
    next_cursor: page.next_cursor,
  };
}
