import { cachedGet, mutation, request, DASHBOARD_CACHE_TTL_MS } from "./http";
import type { CursorPage } from "./useCursorPage";
import type { ApprovalTodo, DiagnosticReport, PlatformAudit, ProfileCreate, ReviewProfile, UsageBreakdown, UsageMonth, UsageRequest, WorkItem, WorkItemCreate, WorkItemUpdate } from "./types";

const base = "/api/v1/platform";
function page<T, P extends CursorPage<T> = CursorPage<T>>(path: string, key: string, query: Record<string, string>, cursor?: string, signal?: AbortSignal, force = false) {
  const params = new URLSearchParams({ limit: "10", ...query, ...(cursor ? { cursor } : {}) });
  return cachedGet<P>(`${key}:${cursor ?? "first"}`, cacheSignal => request(`${base}${path}?${params}`, { signal: cacheSignal }), signal, DASHBOARD_CACHE_TTL_MS, force);
}

export const platformApi = {
  workers: (state: "online" | "offline" | "all", cursor?: string, signal?: AbortSignal, force = false) => page<import("./types").WorkerNode, import("./types").WorkerNodePage>("/workers", `workers:${state}`, { state }, cursor, signal, force),
  projectEvidence: (datasetId?: string) => request<import("./types").ProjectEvidence>(`${base}/evidence${datasetId ? `?dataset_id=${encodeURIComponent(datasetId)}` : ""}`),
  staticReport: (id: string, signal?: AbortSignal) => request<import("./types").StaticReport | null>(`${base}/reviews/${encodeURIComponent(id)}/static-report`, { signal }),
  importStaticReport: (id: string, body: import("./types").StaticReportUpload) => mutation(() => request<import("./types").StaticReport>(`${base}/reviews/${encodeURIComponent(id)}/static-report`, { method: "POST", body: JSON.stringify(body) })),
  staticFindings: (id: string, cursor?: string, signal?: AbortSignal, force = false) => page<import("./types").StaticFinding>(`/reviews/${encodeURIComponent(id)}/static-findings`, `static-findings:${id}`, {}, cursor, signal, force),
  profileQuality: (id: string, datasetId?: string, signal?: AbortSignal) => request<import("./types").ProfileQuality>(`${base}/profiles/${encodeURIComponent(id)}/quality${datasetId ? `?dataset_id=${encodeURIComponent(datasetId)}` : ""}`, { signal }),
  proposeKnowledge: (id: string, body: import("./types").KnowledgeProposalWrite) => mutation(() => request<import("./types").KnowledgeProposalView>(`${base}/work-items/${encodeURIComponent(id)}/knowledge`, { method: "POST", body: JSON.stringify(body) })),
  usage: (month: string, cursor?: string, signal?: AbortSignal, force = false) => page<UsageMonth>("/usage", `usage:${month}`, { month }, cursor, signal, force),
  requests: (id: string, cursor?: string, signal?: AbortSignal, force = false) => page<UsageRequest>(`/usage/${encodeURIComponent(id)}/requests`, `usage-requests:${id}`, {}, cursor, signal, force),
  breakdown: (id: string, signal?: AbortSignal, groupBy: "model" | "agent" = "model") => request<UsageBreakdown[]>(`${base}/usage/${encodeURIComponent(id)}/breakdown?group_by=${groupBy}`, { signal }),
  workItems: (mine: boolean, status: string, overdue: boolean, cursor?: string, signal?: AbortSignal, force = false) => page<WorkItem>("/work-items", `work:${mine}:${status}:${overdue}`, { mine: String(mine), overdue: String(overdue), ...(status ? { status } : {}) }, cursor, signal, force),
  createWork: (body: WorkItemCreate) => mutation(() => request<WorkItem>(`${base}/work-items`, { method: "POST", body: JSON.stringify(body) })),
  updateWork: (id: string, body: WorkItemUpdate) => mutation(() => request<WorkItem>(`${base}/work-items/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) })),
  approvals: (mine: boolean, overdue: boolean, cursor?: string, signal?: AbortSignal, force = false) => page<ApprovalTodo>("/approvals", `approvals:${mine}:${overdue}`, { mine: String(mine), overdue: String(overdue) }, cursor, signal, force),
  profiles: (repository: string, cursor?: string, signal?: AbortSignal, force = false) => page<ReviewProfile>("/profiles", `profiles:${repository}`, repository ? { repository } : {}, cursor, signal, force),
  createProfile: (body: ProfileCreate) => mutation(() => request<ReviewProfile>(`${base}/profiles`, { method: "POST", body: JSON.stringify(body) })),
  activateProfile: (id: string, revision: number, quality?: Omit<import("./types").ProfileActivate, "expected_repository_revision">) => mutation(() => request<{ revision: number }>(`${base}/profiles/${encodeURIComponent(id)}/activate`, { method: "POST", body: JSON.stringify({ expected_repository_revision: revision, ...quality }) })),
  diagnostics: (days: number, signal?: AbortSignal) => request<DiagnosticReport>(`${base}/diagnostics?days=${days}`, { signal }),
  audits: (objectId?: string, cursor?: string, signal?: AbortSignal, force = false) => page<PlatformAudit>("/audits", `platform-audits:${objectId ?? "all"}`, objectId ? { object_id: objectId } : {}, cursor, signal, force),
};
