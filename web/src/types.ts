import type { components } from "./generated/openapi";

type Schemas = components["schemas"];

export type ExecutionStatus = Schemas["ExecutionStatus"];
export type WorkerStatus = Schemas["WorkerStatus"];
export type AccessRole = Schemas["AccessRole"];
export type Permission = Schemas["Permission"];
export type AuthUser = Schemas["AuthResponse"];

export type ReviewItem = Schemas["ReviewItemResponse"];
export type ReviewAction = Schemas["ReviewAction"];
export type FindingDecision = Schemas["FindingDecision"];
export type ReviewStage = Schemas["ReviewStageResponse"];
export type ReviewEvent = Schemas["ReviewEventResponse"];
export type ReviewCiCheck = Schemas["ReviewCiCheckResponse"];
export type ReviewFinding = Schemas["ReviewFindingResponse"];
export type ReviewEvaluationGate = Schemas["ReviewEvaluationGateResponse"];
export type ReviewDetails = Schemas["ReviewDetailsResponse"];
export type ReviewChangeToken = Schemas["ReviewChangeTokenResponse"];
export type WorkerSnapshot = Schemas["WorkerResponse"];
export type DashboardSnapshot = Schemas["DashboardResponse"];
export type ReviewListPage = Schemas["ReviewListResponse"];
export type ReviewRequest = Schemas["ReviewRequest"];
export type ReviewAccepted = Schemas["ReviewAcceptedResponse"];

export type AiProvider = Schemas["ModelProvider"];
export type AiApiProtocol = Schemas["ModelApiProtocol"];
export type AiReasoningEffort = Schemas["ModelReasoningEffort"];
export type AiProviderSettings = Schemas["AiProviderResponse"];
export type AiTestStatus = AiProviderSettings["test_status"];
export type AiSettings = Schemas["AiSettingsResponse"];
export type ReviewAgent = Schemas["ReviewAgent"];
export type AiAgentSettings = Schemas["AiAgentResponse"];
export type AiAgentSettingsResponse = Schemas["AiAgentSettingsResponse"];
export type AiProviderUpdate = Schemas["AiProviderUpdateRequest"];
export type ReviewPolicyUpdate = Schemas["ReviewPolicyUpdateRequest"];
export type ConfigurationAudit = Schemas["ConfigurationAuditResponse"];
export type ConfigurationAuditList = Schemas["ConfigurationAuditListResponse"];

export type KnowledgeVersion = Schemas["KnowledgeVersionResponse"];
export type KnowledgeDocumentSummary = Schemas["KnowledgeDocumentSummaryResponse"];
export type KnowledgeDocument = Schemas["KnowledgeDocumentResponse"];
export type KnowledgeLibrary = Schemas["KnowledgeDocumentListResponse"];
export type KnowledgeMutation = Schemas["KnowledgeMutationResponse"];
export type KnowledgeCitation = Schemas["KnowledgeCitationResponse"];
export type KnowledgeSearchResult = Schemas["KnowledgeSearchResponse"];
