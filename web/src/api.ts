import type { CursorPage } from "./useCursorPage";
import type {
  CodeIndexView, RetrievalSettingsView, RetrievalSettings, RetrievalTrace, RetrievalEvaluationReport, RetrievalSearchQuery,
  AuthUser,
  AiProvider,
  AiProviderUpdate,
  AiAgentSettingsResponse,
  AiAgentUpdate,
  ReviewAgent,
  AiSettings,
  ConfigurationAuditList,
  DashboardSnapshot,
  ReviewListPage,
  FindingDecision,
  ReviewAccepted,
  ReviewAction,
  ReviewChangeToken,
  ReviewDetails,
  ReviewRequest,
  ReviewPolicyUpdate,
  KnowledgeDocument,
  KnowledgeLibrary,
  KnowledgeMutation,
  KnowledgeSearchResult,
} from "./types";

import { cachedGet, request, mutation, SETTINGS_CACHE_TTL_MS, DASHBOARD_CACHE_TTL_MS, CONNECTION_TEST_TIMEOUT_MS, reviewListKey, clearSettingsCache } from "./http";
export { ApiError, ApiTimeoutError, DASHBOARD_CACHE_TTL_MS, reviewListKey, peekReadCache, subscribeReadCache, primeReadCache, clearReadCache, clearSettingsCache } from "./http";

export const api = {
  evaluationOverview: (id: string, signal?: AbortSignal, caseId?: string, variant?: import("./types").EvaluationVariant) => request<import("./types").EvaluationOverview>(`/api/v1/evaluations/datasets/${encodeURIComponent(id)}/overview?` + new URLSearchParams({...caseId ? {case_id:caseId} : {}, ...variant ? {variant} : {}}), {signal}),
  evaluationDatasets: (includeArchived = false, cursor?: string, signal?: AbortSignal, force = false, archivedOnly = false) =>
    cachedGet("evaluation-datasets:" + (archivedOnly ? "removed" : includeArchived) + ":" + (cursor ?? "first"), (cacheSignal) =>
      request<CursorPage<import("./types").EvaluationDataset>>("/api/v1/evaluations/datasets?" + new URLSearchParams({limit: "10", include_archived: String(includeArchived), ...(archivedOnly ? {archived_only: "true"} : {}), ...(cursor ? {cursor} : {})}), {signal: cacheSignal}), signal, SETTINGS_CACHE_TTL_MS, force),
  evaluationSources: (datasetId?: string, caseId?: string, cursor?: string, signal?: AbortSignal, force = false) =>
    cachedGet("evaluation-sources:" + (datasetId ?? "all") + ":" + (caseId ?? "all") + ":" + (cursor ?? "first"), (cacheSignal) =>
      request<CursorPage<import("./types").EvaluationRunOption>>("/api/v1/evaluations/sources?" + new URLSearchParams({limit: "10", ...(datasetId ? {dataset_id:datasetId} : {}), ...(caseId ? {case_id:caseId} : {}), ...(cursor ? {cursor} : {})}), {signal: cacheSignal}), signal, SETTINGS_CACHE_TTL_MS, force),
  createEvaluationDataset: (body: import("./types").EvaluationDatasetCreate, key: string) =>
    mutation(() => request<import("./types").EvaluationDataset>("/api/v1/evaluations/datasets", {method:"POST", headers:{"Idempotency-Key":key}, body:JSON.stringify(body)})),
  evaluationDataset: (id: string, signal?: AbortSignal) =>
    request<import("./types").EvaluationDataset>("/api/v1/evaluations/datasets/" + encodeURIComponent(id), {signal}),
  archiveEvaluationDataset: (id: string, revision: number, archived: boolean) =>
    mutation(() => request<import("./types").EvaluationDataset>("/api/v1/evaluations/datasets/" + encodeURIComponent(id) + "/archive", {method:"POST", body:JSON.stringify({expected_revision:revision, archived})})),
  evaluationCases: (id: string, split?: import("./types").EvaluationSplit, cursor?: string, signal?: AbortSignal, force = false) =>
    cachedGet("evaluation-cases:" + id + ":" + (split ?? "all") + ":" + (cursor ?? "first"), (cacheSignal) =>
      request<CursorPage<import("./types").EvaluationCase>>("/api/v1/evaluations/datasets/" + encodeURIComponent(id) + "/cases?" + new URLSearchParams({limit:"10", ...(split ? {split} : {}), ...(cursor ? {cursor} : {})}), {signal:cacheSignal}), signal, DASHBOARD_CACHE_TTL_MS, force),
  evaluationAudits: (id: string, cursor?: string, signal?: AbortSignal, force = false) =>
    cachedGet("evaluation-audits:" + id + ":" + (cursor ?? "first"), (cacheSignal) =>
      request<CursorPage<import("./types").EvaluationAudit>>("/api/v1/evaluations/datasets/" + encodeURIComponent(id) + "/audits?" + new URLSearchParams({limit:"10", ...(cursor ? {cursor} : {})}), {signal:cacheSignal}), signal, DASHBOARD_CACHE_TTL_MS, force),
  importEvaluationObservations: (id: string, body: import("./types").EvaluationImport) =>
    mutation(() => request<import("./types").EvaluationImportResult>("/api/v1/evaluations/datasets/" + encodeURIComponent(id) + "/observations", {method:"POST", body:JSON.stringify(body)})),
  evaluationCase: (id: string, signal?: AbortSignal) =>
    request<import("./types").EvaluationCaseDetail>("/api/v1/evaluations/cases/" + encodeURIComponent(id), {signal}),
  updateEvaluationReference: (id: string, body: import("./types").EvaluationReferenceUpdate) =>
    mutation(() => request<import("./types").EvaluationCaseDetail>("/api/v1/evaluations/cases/" + encodeURIComponent(id) + "/reference", {method:"PUT", body:JSON.stringify(body)})),
  reviewEvaluationReference: (id: string, revision: number, agrees: boolean, note: string) =>
    mutation(() => request<import("./types").EvaluationCaseDetail>("/api/v1/evaluations/cases/" + encodeURIComponent(id) + "/reference/reviews", {method:"POST", body:JSON.stringify({expected_revision:revision, agrees, note})})),
  evaluationObservation: (caseId: string, variant: import("./types").EvaluationVariant, signal?: AbortSignal) =>
    request<import("./types").EvaluationObservationDetail>("/api/v1/evaluations/cases/" + encodeURIComponent(caseId) + "/observations/" + variant, {signal}),
  evaluationFindings: (caseId: string, variant: import("./types").EvaluationVariant, snapshot: string, cursor?: string, signal?: AbortSignal, force = false) =>
    cachedGet("evaluation-findings:" + caseId + ":" + variant + ":" + snapshot + ":" + (cursor ?? "first"), (cacheSignal) =>
      request<CursorPage<import("./types").EvaluationFinding>>("/api/v1/evaluations/cases/" + encodeURIComponent(caseId) + "/observations/" + variant + "/findings?" + new URLSearchParams({limit:"10", ...(cursor ? {cursor} : {})}), {signal:cacheSignal}), signal, DASHBOARD_CACHE_TTL_MS, force),
  evaluationChanges: (caseId: string, variant: import("./types").EvaluationVariant, snapshot: string, cursor?: string, signal?: AbortSignal, force = false) =>
    cachedGet("evaluation-changes:" + caseId + ":" + variant + ":" + snapshot + ":" + (cursor ?? "first"), (cacheSignal) =>
      request<CursorPage<import("./types").EvaluationChange>>("/api/v1/evaluations/cases/" + encodeURIComponent(caseId) + "/observations/" + variant + "/changes?" + new URLSearchParams({limit:"10", ...(cursor ? {cursor} : {})}), {signal:cacheSignal}), signal, DASHBOARD_CACHE_TTL_MS, force),
  saveEvaluationFindingReview: (caseId: string, variant: import("./types").EvaluationVariant, findingId: string, revision: number, decision: import("./types").EvaluationDecision) =>
    mutation(() => request<import("./types").EvaluationObservationDetail>("/api/v1/evaluations/cases/" + encodeURIComponent(caseId) + "/observations/" + variant + "/findings/" + encodeURIComponent(findingId) + "/review", {method:"PUT",body:JSON.stringify({expected_revision:revision, decision})})),
  submitEvaluationReview: (caseId: string, variant: import("./types").EvaluationVariant, revision: number) =>
    mutation(() => request<import("./types").EvaluationObservationDetail>("/api/v1/evaluations/cases/" + encodeURIComponent(caseId) + "/observations/" + variant + "/submit", {method:"POST",body:JSON.stringify({expected_revision:revision})})),
  replaceEvaluationObservation: (caseId: string, variant: import("./types").EvaluationVariant, runId: string, revision: number, resetReviews: boolean) =>
    mutation(() => request<import("./types").EvaluationObservationDetail>("/api/v1/evaluations/cases/" + encodeURIComponent(caseId) + "/observations/" + variant + "/source", {method:"PUT",body:JSON.stringify({expected_revision:revision,review_run_id:runId,reset_reviews:resetReviews})})),
  evaluationReport: (id: string, split: import("./types").EvaluationSplit, signal?: AbortSignal) =>
    request<import("./types").EvaluationReport>("/api/v1/evaluations/datasets/" + encodeURIComponent(id) + "/report?split=" + split, {signal}),
  teamMembers: (cursor?: string, signal?: AbortSignal, force = false) =>
    cachedGet("team-members:" + (cursor ?? "first"), (cacheSignal) =>
      request<import("./types").TeamMemberPage>("/api/v1/team/members?" + new URLSearchParams({limit: "10", ...(cursor ? {cursor} : {})}), {signal: cacheSignal}), signal, SETTINGS_CACHE_TTL_MS, force),
  saveTeamMember: (username: string, body: import("./types").TeamMemberWrite) =>
    mutation(() => request<import("./types").TeamMember>("/api/v1/team/members/" + encodeURIComponent(username), {method: "PUT", body: JSON.stringify(body)})),
  githubInstallations: (page: number, signal?: AbortSignal) => request<import("./RepositoryConnectPanel").AppInstallations>(`/api/v1/team/github/installations?page=${page}`, {signal}),
  githubAuthorizedRepositories: (installation: number, page: number, signal?: AbortSignal) => request<import("./RepositoryConnectPanel").AuthorizedRepositories>(`/api/v1/team/github/installations/${installation}/repositories?page=${page}`, {signal}),
  teamRepositories: (cursor?: string, signal?: AbortSignal, force = false) =>
    cachedGet("team-repositories:" + (cursor ?? "first"), (cacheSignal) =>
      request<CursorPage<import("./types").TeamRepository>>("/api/v1/team/repositories?" + new URLSearchParams({limit: "10", ...(cursor ? {cursor} : {})}), {signal: cacheSignal}), signal, SETTINGS_CACHE_TTL_MS, force),
  saveTeamRepository: (body: import("./types").TeamRepositoryWrite, id?: string) =>
    mutation(() => request<import("./types").TeamRepository>("/api/v1/team/repositories" + (id ? "/" + encodeURIComponent(id) : ""), {method: id ? "PUT" : "POST", body: JSON.stringify(body)})),
  checkRepositoryConnection: (id: string) => mutation(() => request<import("./types").TeamRepository>(`/api/v1/team/repositories/${encodeURIComponent(id)}/check`, {method: "POST"})),
  teamRepositoryByName: (repository: string, signal?: AbortSignal) => request<CursorPage<import("./types").TeamRepository>>("/api/v1/team/repositories?" + new URLSearchParams({repository, limit: "1"}), {signal}),
  teamAudits: (cursor?: string, signal?: AbortSignal, force = false) =>
    cachedGet("team-audits:" + (cursor ?? "first"), (cacheSignal) =>
      request<CursorPage<import("./types").TeamAudit>>("/api/v1/team/audits?" + new URLSearchParams({limit: "10", ...(cursor ? {cursor} : {})}), {signal: cacheSignal}), signal, SETTINGS_CACHE_TTL_MS, force),
  retrievalTargets: (signal?: AbortSignal, cursor?: string, force = false) =>
    cachedGet(`retrieval-targets:${cursor ?? "first"}`, (cacheSignal) => request<CursorPage<import("./types").IndexTarget>>(`/api/v1/retrieval/targets?${new URLSearchParams({limit: "10", ...(cursor ? {cursor} : {})})}`, {signal: cacheSignal}), signal, SETTINGS_CACHE_TTL_MS, force),
  retrievalOperations: (signal?: AbortSignal, force = false) =>
    cachedGet("retrieval-operations", (cacheSignal) => request<import("./types").RetrievalOperations>("/api/v1/retrieval/operations", {signal: cacheSignal}), signal, DASHBOARD_CACHE_TTL_MS, force),
  enrichCodeIndex: (indexId: string) => mutation(() => request<{status: string}>(`/api/v1/retrieval/indexes/${encodeURIComponent(indexId)}/enrich`, {method: "POST"})),
  retrievalSettings: (signal?: AbortSignal, force = false) =>
    cachedGet("retrieval-settings", (cacheSignal) => request<RetrievalSettingsView>("/api/v1/retrieval/settings", {signal: cacheSignal}), signal, SETTINGS_CACHE_TTL_MS, force),
  updateRetrievalSettings: (settings: RetrievalSettings, revision: number, apiKey?: string) => mutation(() =>
    request<RetrievalSettingsView>("/api/v1/retrieval/settings", {method: "PUT", body: JSON.stringify({settings, expected_revision: revision, api_key: apiKey})})),
  testRetrievalSettings: () => mutation(() => request<RetrievalSettingsView>("/api/v1/retrieval/settings/test", {method: "POST"}, CONNECTION_TEST_TIMEOUT_MS)),
  retrievalIndexes: (signal?: AbortSignal, cursor?: string, force = false) =>
    cachedGet(`retrieval-indexes:${cursor ?? "first"}`, (cacheSignal) => request<CursorPage<CodeIndexView>>(`/api/v1/retrieval/indexes?${new URLSearchParams({limit: "10", ...(cursor ? {cursor} : {})})}`, {signal: cacheSignal}), signal, DASHBOARD_CACHE_TTL_MS, force),
  codeIndex: (id: string, signal?: AbortSignal) => request<CodeIndexView>(`/api/v1/retrieval/indexes/${encodeURIComponent(id)}`, {signal}),
  createCodeIndex: (reviewRunId: string) => mutation(() => request<CodeIndexView>("/api/v1/retrieval/indexes", {method: "POST", body: JSON.stringify({review_run_id: reviewRunId})})),
  retryCodeIndex: (indexId: string) => mutation(() => request<{status: string}>(`/api/v1/retrieval/indexes/${encodeURIComponent(indexId)}/retry`, {method: "POST"})),
  searchCodeIndex: (indexId: string, query: RetrievalSearchQuery, signal?: AbortSignal) =>
    request<RetrievalTrace>(`/api/v1/retrieval/indexes/${encodeURIComponent(indexId)}/search`, {method: "POST", body: JSON.stringify(query), signal}, 150_000),
  reviewRetrieval: (reviewRunId: string, signal?: AbortSignal, force = false) =>
    cachedGet(`review-retrieval:${reviewRunId}`, (cacheSignal) => request<RetrievalTrace[]>(`/api/v1/reviews/${encodeURIComponent(reviewRunId)}/retrieval`, {signal: cacheSignal}), signal, DASHBOARD_CACHE_TTL_MS, force),
  retrievalHistory: (id: string, query: string, strategy: string, cursor?: string, signal?: AbortSignal) =>
    request<CursorPage<import("./types").SearchHistoryItem>>(`/api/v1/retrieval/indexes/${encodeURIComponent(id)}/history?${new URLSearchParams({query, strategy, ...(cursor ? {cursor} : {})})}`, {signal}),
  retrievalHistoryDetail: (id: string) => request<RetrievalTrace>(`/api/v1/retrieval/history/${encodeURIComponent(id)}`),
  compareRetrieval: (id: string, body: import("./types").RetrievalComparisonRequest) => mutation(() => request<RetrievalEvaluationReport>(`/api/v1/retrieval/indexes/${encodeURIComponent(id)}/compare`, {method: "POST", body: JSON.stringify(body)}, 180_000)),
  retrievalEvaluations: (signal?: AbortSignal, cursor?: string, force = false, indexId?: string) =>
    cachedGet(`retrieval-evaluations:${indexId ?? "all"}:${cursor ?? "first"}`, (cacheSignal) => request<CursorPage<RetrievalEvaluationReport>>(`/api/v1/retrieval/evaluations?${new URLSearchParams({limit: "10", ...(cursor ? {cursor} : {}), ...(indexId ? {index_id:indexId} : {})})}`, {signal: cacheSignal}), signal, SETTINGS_CACHE_TTL_MS, force),

  /**
   * 读取当前管理员会话。
   *
   * 返回用户名和会话到期时间；未登录、过期或签名无效时抛出状态码为 401 的
   * `ApiError`，由根组件切换到登录页。请求没有副作用，也不会刷新会话期限。
   */
  me: (signal?: AbortSignal) =>
    request<AuthUser>("/api/v1/auth/me", { signal }),
  /**
   * 提交管理员凭据并接收服务端设置的 HttpOnly 会话 Cookie。
   *
   * 参数：
   * - `username`：管理员账号文本。
   * - `password`：本次登录使用的明文密码，只交给 HTTPS API，不在模块中保存。
   *
   * 返回登录后的公开用户信息；密码错误通常得到 401，限流得到 429，配置或网络
   * 故障则由统一请求函数转换/传播。Cookie 由浏览器管理，返回值不包含 Token。
   */
  login: async (username: string, password: string) => {
    // 登录可能切换浏览器中的管理员身份，不能沿用上一身份的设置快照。
    clearSettingsCache();
    return request<AuthUser>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
  },
  /**
   * 请求服务端删除当前会话 Cookie。
   *
   * 该操作对已经没有 Cookie 的客户端也是幂等的；成功时返回 204。网络失败时
   * 调用方仍可清空本地页面状态，因为应用没有保存会话 Token；浏览器密码库中若有
   * 用户主动记住的账号密码，不会被注销接口删除。
   */
  logout: async () => {
    try {
      return await request<void>("/api/v1/auth/logout", {
        method: "POST",
      });
    } finally {
      clearSettingsCache();
    }
  },
  /**
   * 读取一次完整 Dashboard 快照。
   *
   * 首屏加载和用户点击“立即刷新”都会调用它；返回状态计数、Worker 心跳和最近
   * 任务。数据库暂时不可用时抛出 `ApiError(503)`，不会伪造空快照覆盖旧数据。
   */
  dashboard: (
    cursor?: string,
    limit = 10,
    signal?: AbortSignal,
    force = false,
  ) => {
    const query = new URLSearchParams({ limit: String(limit) });
    if (cursor) query.set("cursor", cursor);
    const path = `/api/v1/dashboard?${query.toString()}`;
    // 用户明确刷新时绕过该键的短时缓存，并把新快照重新写回缓存。
    return cachedGet(
      `dashboard:${cursor ?? "first"}:${limit}`,
      (cacheSignal) => request<DashboardSnapshot>(path, {
        cache: "no-store",
        signal: cacheSignal,
      }),
      signal,
      DASHBOARD_CACHE_TTL_MS,
      force,
    );
  },
  reviews: (cursor?: string, limit = 10, signal?: AbortSignal, status = "all", search = "", force = false) => {
    const query = new URLSearchParams({ limit: String(limit) });
    if (cursor) query.set("cursor", cursor);
    if (status !== "all") query.set("execution_status", status);
    if (search) query.set("q", search);
    return cachedGet<ReviewListPage>(
      `${reviewListKey(status, search, limit)}:${cursor ?? "first"}`,
      async (cacheSignal) => {
        if (!cursor && status === "all" && !search) {
          const snapshot = await api.dashboard(undefined, limit, cacheSignal, force);
          return {items: snapshot.recent_reviews, total: snapshot.total_reviews, next_cursor: snapshot.next_cursor};
        }
        return request<ReviewListPage>(`/api/v1/reviews?${query}`, {signal: cacheSignal});
      }, signal, DASHBOARD_CACHE_TTL_MS, force,
    );
  },

  /**
   * 创建一个幂等的审查任务。
   *
   * 参数：
   * - `payload`：安装、仓库、PR 编号和完整 head SHA。
   * - `idempotencyKey`：一次逻辑提交的稳定键；相同键重试不会重复创建任务。
   *
   * 返回 202 接受结果；`created` 可区分首次创建和幂等重试。输入不合法时为 422，
   * 同一键对应不同内容时为 409，认证失效时为 401。该方法不等待 Worker 完成审查。
   */
  createReview: (payload: ReviewRequest, idempotencyKey: string) =>
    mutation(() => request<ReviewAccepted>("/api/v1/reviews", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(payload),
    })),
  reviewDetails: (
    reviewRunId: string,
    findingCursor?: string,
    findingLimit = 10,
    signal?: AbortSignal,
    force = false,
    view = "full",
  ) => {
    const query = new URLSearchParams({ finding_limit: String(findingLimit) });
    if (view !== "full") query.set("view", view);
    if (findingCursor) query.set("finding_cursor", findingCursor);
    return cachedGet(`review-details:${reviewRunId}:${findingCursor ?? "first"}:${findingLimit}${view === "full" ? "" : `:${view}`}`,
      (cacheSignal) => request<ReviewDetails>(`/api/v1/reviews/${encodeURIComponent(reviewRunId)}?${query}`, {signal: cacheSignal}),
      signal, DASHBOARD_CACHE_TTL_MS, force);
  },
  findingPage: (reviewRunId: string, cursor?: string, signal?: AbortSignal, force = false, severity = "all", status = "all", search = "") => {
    const query = new URLSearchParams({limit: "10"});
    if (cursor) query.set("cursor", cursor);
    if (severity !== "all") query.set("severity", severity);
    if (status !== "all") query.set("adjudication_status", status);
    if (search) query.set("q", search);
    return cachedGet(`findings:${reviewRunId}:${severity}:${status}:${search}:${cursor ?? "first"}`,
      (cacheSignal) => request<CursorPage<import("./types").ReviewFinding>>(`/api/v1/reviews/${encodeURIComponent(reviewRunId)}/findings?${query}`, {signal: cacheSignal}), signal, DASHBOARD_CACHE_TTL_MS, force);
  },
  batchPage: (reviewRunId: string, agent: string, after: number, signal?: AbortSignal) =>
    request<CursorPage<import("./types").BatchSnapshot>>(`/api/v1/reviews/${encodeURIComponent(reviewRunId)}/batches?${new URLSearchParams({agent, after: String(after), limit: "10"})}`, {signal}),
  eventPage: (reviewRunId: string, cursor?: string, signal?: AbortSignal, force = false, filter = "all") => {
    const query = new URLSearchParams({limit: "10", event_filter: filter, ...(cursor ? {cursor} : {})});
    return cachedGet(`events:${reviewRunId}:${filter}:${cursor ?? "first"}`,
      (cacheSignal) => request<CursorPage<import("./types").ReviewEvent>>(`/api/v1/reviews/${encodeURIComponent(reviewRunId)}/events?${query}`, {signal: cacheSignal}), signal, DASHBOARD_CACHE_TTL_MS, force);
  },
  reviewChangeToken: (reviewRunId: string, signal?: AbortSignal) =>
    request<ReviewChangeToken>(
      `/api/v1/reviews/${encodeURIComponent(reviewRunId)}/change-token`,
      { signal },
    ),
  syncReviewIdentity: (reviewRunId: string, idempotencyKey: string) =>
    mutation(() => request<ReviewDetails>(
      `/api/v1/reviews/${encodeURIComponent(reviewRunId)}/identity/sync`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
      },
    )),
  reviewAction: (
    reviewRunId: string,
    action: ReviewAction,
    idempotencyKey: string,
    targetStage?: string,
    options?: {
      retryScope?: "failed_node" | "stage" | "new_review";
      agent?: string;
      batchNumber?: number;
      stateVersion?: string;
      headSha?: string;
    },
  ) =>
    mutation(() => request<{
      action: ReviewAction;
      review_run_id: string;
      review_task_id: string;
      execution_status: string;
      workflow_status?: string | null;
    }>(`/api/v1/reviews/${encodeURIComponent(reviewRunId)}/actions`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        action,
        ...(targetStage ? { target_stage: targetStage } : {}),
        ...(options?.retryScope ? { retry_scope: options.retryScope } : {}),
        ...(options?.agent ? { agent: options.agent } : {}),
        ...(options?.batchNumber ? { batch_number: options.batchNumber } : {}),
        ...(options?.stateVersion ? { state_version: options.stateVersion } : {}),
        ...(options?.headSha ? { head_sha: options.headSha } : {}),
      }),
    })),
  decideFinding: (
    reviewRunId: string,
    findingId: string,
    decision: FindingDecision,
    idempotencyKey: string,
  ) =>
    mutation(() => request<ReviewDetails>(
      `/api/v1/reviews/${encodeURIComponent(reviewRunId)}/findings/${encodeURIComponent(findingId)}`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify({ decision }),
      },
    )),
  aiSettings: (signal?: AbortSignal, force = false) =>
    cachedGet(
      "ai-settings",
      (cacheSignal) => request<AiSettings>("/api/v1/settings/ai", {
        cache: "no-store",
        signal: cacheSignal,
      }),
      signal,
      SETTINGS_CACHE_TTL_MS,
      force,
    ),
  updateAiProvider: (provider: AiProvider, payload: AiProviderUpdate) =>
    mutation(() => request<AiSettings>(
      `/api/v1/settings/ai/providers/${provider}`,
      {
        method: "PUT",
        body: JSON.stringify(payload),
      },
    )),
  testAiProvider: (provider: AiProvider, expectedRevision: number) =>
    mutation(() => request<AiSettings>(
      `/api/v1/settings/ai/providers/${provider}/test`,
      {
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision }),
      },
      CONNECTION_TEST_TIMEOUT_MS,
    )),
  activateAiProvider: (provider: AiProvider, expectedRevision: number) =>
    mutation(() => request<AiSettings>(
      `/api/v1/settings/ai/providers/${provider}/activate`,
      {
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision }),
      },
    )),
  updateReviewPolicy: (payload: ReviewPolicyUpdate) =>
    mutation(() => request<AiSettings>(
      "/api/v1/settings/ai/review-policy",
      {
        method: "PUT",
        body: JSON.stringify(payload),
      },
    )),
  configurationAudits: (signal?: AbortSignal, force = false, cursor?: string) =>
    cachedGet(
      `configuration-audits:${cursor ?? "first"}`,
      (cacheSignal) => request<ConfigurationAuditList>(
        `/api/v1/settings/audits?${new URLSearchParams({limit: "10", ...(cursor ? {cursor} : {})})}`,
        { cache: "no-store", signal: cacheSignal },
      ),
      signal,
      SETTINGS_CACHE_TTL_MS,
      force,
    ),
  agentSettings: (signal?: AbortSignal, force = false) =>
    cachedGet(
      "agent-settings",
      (cacheSignal) => request<AiAgentSettingsResponse>(
        "/api/v1/settings/ai/agents",
        { cache: "no-store", signal: cacheSignal },
      ),
      signal,
      SETTINGS_CACHE_TTL_MS,
      force,
    ),
  updateAgent: (agent: ReviewAgent, payload: AiAgentUpdate) =>
    mutation(() => request<AiAgentSettingsResponse>(
      `/api/v1/settings/ai/agents/${agent}`,
      {
        method: "PUT",
        body: JSON.stringify(payload),
      },
    )),
  testAgent: (agent: ReviewAgent, expectedRevision: number) =>
    mutation(() => request<AiAgentSettingsResponse>(
      `/api/v1/settings/ai/agents/${agent}/test`,
      {
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision }),
      },
      CONNECTION_TEST_TIMEOUT_MS,
    )),
  setAgentEnabled: (agent: ReviewAgent, enabled: boolean, expectedRevision: number) =>
    mutation(() => request<AiAgentSettingsResponse>(
      `/api/v1/settings/ai/agents/${agent}/enabled`,
      {
        method: "POST",
        body: JSON.stringify({ enabled, expected_revision: expectedRevision }),
      },
    )),
  searchKnowledge: (query: string, limit = 5, repository?: string) =>
    request<KnowledgeSearchResult>(
      `/api/v1/knowledge/search?q=${encodeURIComponent(query)}&limit=${limit}${repository ? `&repository=${encodeURIComponent(repository)}` : ""}`,
    ),
  knowledgeDocuments: (
    includeArchived = false,
    signal?: AbortSignal,
    force = false,
    offset = 0,
    query = "",
    archivedOnly = false,
  ) =>
    cachedGet(
      `knowledge-documents:${archivedOnly ? "archived-only" : includeArchived ? "archived" : "active"}:${offset}:${query}`,
      (cacheSignal) => request<KnowledgeLibrary>(
        `/api/v1/knowledge/documents?${new URLSearchParams({include_archived: String(includeArchived), ...(archivedOnly ? {archived_only: "true"} : {}), limit: "10", offset: String(offset), q: query})}`,
        { cache: "no-store", signal: cacheSignal },
      ),
      signal,
      SETTINGS_CACHE_TTL_MS,
      force,
    ),
  knowledgeDocument: (
    documentId: string,
    signal?: AbortSignal,
    force = false,
    versionCursor?: string,
  ) =>
    cachedGet(
      `knowledge-document:${documentId}${versionCursor ? `:versions:${versionCursor}` : ""}`,
      (cacheSignal) => request<KnowledgeDocument>(
        `/api/v1/knowledge/documents/${encodeURIComponent(documentId)}?${new URLSearchParams({version_limit: "10", ...(versionCursor ? {version_cursor: versionCursor} : {})})}`,
        { cache: "no-store", signal: cacheSignal },
      ),
      signal,
      SETTINGS_CACHE_TTL_MS,
      force,
    ),
  deleteKnowledgeDocument: (id: string, revision: number, version: number) => mutation(() => request<{revision: number}>(`/api/v1/knowledge/documents/${encodeURIComponent(id)}`, {
    method: "DELETE", body: JSON.stringify({expected_revision: revision, expected_document_version: version}),
  })),
  createKnowledgeDocument: (payload: {
    repository_scope?: string | null;
    expected_revision: number;
    source: string;
    content: string;
    enabled: boolean;
  }) => mutation(() => request<KnowledgeMutation>("/api/v1/knowledge/documents", {
    method: "POST",
    body: JSON.stringify(payload),
  })),
  updateKnowledgeDocument: (documentId: string, payload: {
    repository_scope?: string | null;
    expected_revision: number;
    expected_document_version: number;
    source: string;
    content: string;
    enabled: boolean;
  }) => mutation(() => request<KnowledgeMutation>(
    `/api/v1/knowledge/documents/${encodeURIComponent(documentId)}`,
    { method: "PUT", body: JSON.stringify(payload) },
  )),
  setKnowledgeDocumentArchived: (
    documentId: string,
    archived: boolean,
    expectedRevision: number,
    expectedDocumentVersion: number,
    restoreEnabled = false,
  ) => mutation(() => request<KnowledgeMutation>(
    `/api/v1/knowledge/documents/${encodeURIComponent(documentId)}/${archived ? "archive" : "restore"}`,
    {
      method: "POST",
      body: JSON.stringify({
        expected_revision: expectedRevision,
        expected_document_version: expectedDocumentVersion,
        ...(!archived ? { enabled: restoreEnabled } : {}),
      }),
    },
  )),
  restoreKnowledgeVersion: (
    documentId: string,
    version: number,
    expectedRevision: number,
    expectedDocumentVersion: number,
  ) => mutation(() => request<KnowledgeMutation>(
    `/api/v1/knowledge/documents/${encodeURIComponent(documentId)}/versions/${version}/restore`,
    {
      method: "POST",
      body: JSON.stringify({
        expected_revision: expectedRevision,
        expected_document_version: expectedDocumentVersion,
      }),
    },
  )),
};
