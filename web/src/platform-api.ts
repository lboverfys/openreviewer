import { cachedGet, mutation, request, DASHBOARD_CACHE_TTL_MS } from "./http";
import type { CursorPage } from "./useCursorPage";
import type { ApprovalTodo, DiagnosticReport, PlatformAudit, ProfileCreate, ReviewProfile, UsageBreakdown, UsageMonth, UsageRequest, WorkItem, WorkItemCreate, WorkItemUpdate } from "./types";

const base = "/api/v1/platform";
function page<T>(path: string, key: string, query: Record<string, string>, cursor?: string, signal?: AbortSignal, force = false) {
  const params = new URLSearchParams({ limit: "10", ...query, ...(cursor ? { cursor } : {}) });
  return cachedGet<CursorPage<T>>(`${key}:${cursor ?? "first"}`, cacheSignal => request(`${base}${path}?${params}`, { signal: cacheSignal }), signal, DASHBOARD_CACHE_TTL_MS, force);
}

export const platformApi = {
  proposeKnowledge: (id: string, body: import("./types").KnowledgeProposalWrite) => mutation(() => request<import("./types").KnowledgeProposalView>(`${base}/work-items/${encodeURIComponent(id)}/knowledge`, { method: "POST", body: JSON.stringify(body) })),
  usage: (month: string, cursor?: string, signal?: AbortSignal, force = false) => page<UsageMonth>("/usage", `usage:${month}`, { month }, cursor, signal, force),
  requests: (id: string, cursor?: string, signal?: AbortSignal, force = false) => page<UsageRequest>(`/usage/${encodeURIComponent(id)}/requests`, `usage-requests:${id}`, {}, cursor, signal, force),
  breakdown: (id: string, signal?: AbortSignal) => request<UsageBreakdown[]>(`${base}/usage/${encodeURIComponent(id)}/breakdown`, { signal }),
  workItems: (mine: boolean, status: string, overdue: boolean, cursor?: string, signal?: AbortSignal, force = false) => page<WorkItem>("/work-items", `work:${mine}:${status}:${overdue}`, { mine: String(mine), overdue: String(overdue), ...(status ? { status } : {}) }, cursor, signal, force),
  createWork: (body: WorkItemCreate) => mutation(() => request<WorkItem>(`${base}/work-items`, { method: "POST", body: JSON.stringify(body) })),
  updateWork: (id: string, body: WorkItemUpdate) => mutation(() => request<WorkItem>(`${base}/work-items/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) })),
  approvals: (mine: boolean, overdue: boolean, cursor?: string, signal?: AbortSignal, force = false) => page<ApprovalTodo>("/approvals", `approvals:${mine}:${overdue}`, { mine: String(mine), overdue: String(overdue) }, cursor, signal, force),
  profiles: (repository: string, cursor?: string, signal?: AbortSignal, force = false) => page<ReviewProfile>("/profiles", `profiles:${repository}`, repository ? { repository } : {}, cursor, signal, force),
  createProfile: (body: ProfileCreate) => mutation(() => request<ReviewProfile>(`${base}/profiles`, { method: "POST", body: JSON.stringify(body) })),
  activateProfile: (id: string, revision: number) => mutation(() => request<{ revision: number }>(`${base}/profiles/${encodeURIComponent(id)}/activate`, { method: "POST", body: JSON.stringify({ expected_repository_revision: revision }) })),
  diagnostics: (days: number, signal?: AbortSignal) => request<DiagnosticReport>(`${base}/diagnostics?days=${days}`, { signal }),
  audits: (objectId?: string, cursor?: string, signal?: AbortSignal, force = false) => page<PlatformAudit>("/audits", `platform-audits:${objectId ?? "all"}`, objectId ? { object_id: objectId } : {}, cursor, signal, force),
};
