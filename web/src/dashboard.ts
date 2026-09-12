import type {
  DashboardSnapshot,
  ExecutionStatus,
} from "./types";

export type DashboardStreamState = "connecting" | "live" | "reconnecting";

export interface DashboardRefreshOptions {
  signal?: AbortSignal;
  force?: boolean;
  preserveLiveSnapshot?: boolean;
}

export const DASHBOARD_INITIAL_FALLBACK_MS = 0;
export const DASHBOARD_FALLBACK_REFRESH_MS = 15_000;

export const DASHBOARD_STATUS_ORDER: ExecutionStatus[] = [
  "queued",
  "running",
  "waiting_for_ci",
  "ready_for_review",
  "completed",
  "failed",
];

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

  return incoming;
}
