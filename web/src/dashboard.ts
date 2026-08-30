import type { DashboardSnapshot, ReviewItem, ReviewListPage } from "./types";

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
