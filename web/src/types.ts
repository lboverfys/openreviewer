export type ExecutionStatus =
  | "queued"
  | "ci"
  | "planning"
  | "agent_batches"
  | "aggregating"
  | "awaiting_approval"
  | "approved"
  | "rejected"
  | "awaiting_publish"
  | "publishing"
  | "paused"
  | "waiting_for_ci"
  | "running"
  | "ready_for_review"
  | "completed"
  | "failed"
  | "timed_out"
  | "cancelled"
  | "superseded";

export type WorkerStatus = "starting" | "idle" | "busy" | "stopping";

export interface AuthUser {
  authenticated: true;
  username: string;
  expires_at: string;
}

export interface ReviewItem {
  review_run_id: string;
  review_task_id: string;
  repository: string;
  pull_request_number: number;
  head_sha: string;
  pr_title: string | null;
  pr_author_login: string | null;
  pr_html_url: string | null;
  head_repository: string | null;
  head_ref: string | null;
  base_repository: string | null;
  base_ref: string | null;
  execution_status: ExecutionStatus;
  workflow_status: ExecutionStatus;
  attempt_count: number;
  max_attempts: number;
  last_error: string | null;
  last_error_code?: string | null;
  last_error_retryable?: boolean | null;
  last_error_details?: Record<string, unknown> | null;
  review_conclusion: string | null;
  coverage_status: string;
  model_review_completed_at: string | null;
  finding_count: number;
  unverified_finding_count: number;
  model_attempt_count: number;
  created_at: string;
  updated_at: string;
}

export type ReviewAction =
  | "start"
  | "pause"
  | "resume"
  | "retry_stage"
  | "approve"
  | "reject"
  | "publish"
  | "expedite"
  | "retry"
  | "cancel"
  | "rerun";
export type FindingDecision = "verified" | "rejected";

export interface ReviewStage {
  key: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  detail_code: string | null;
}

export interface ReviewEvent {
  id: string;
  event_type: string;
  payload: Record<string, unknown>;
  occurred_at: string;
}

export interface ReviewCiCheck {
  name: string;
  kind: string;
  status: string;
  conclusion: string | null;
  observed_at: string;
}

export interface ReviewFinding {
  id: string;
  severity: string;
  category: string;
  title: string;
  evidence: string;
  impact: string;
  suggestion: string;
  required_test: string | null;
  confidence: number;
  verification_status: string;
  location_file: string | null;
  location_start_line: number | null;
  location_end_line: number | null;
  location_side: string | null;
  location_in_diff: boolean;
  location_symbol: string | null;
  rule_reference: string | null;
  reviewed_at: string | null;
  reviewed_by: string | null;
  created_at: string;
}

export interface ReviewDetails {
  review_run_id: string;
  review_task_id: string;
  review_version_key: string;
  installation_id: number;
  repository_id: number;
  repository: string;
  pull_request_number: number;
  head_sha: string;
  execution_status: ExecutionStatus;
  workflow_status: ExecutionStatus;
  review_conclusion: string | null;
  coverage_status: string;
  priority: number;
  attempt_count: number;
  model_attempt_count: number;
  max_attempts: number;
  ci_poll_count: number;
  available_at: string;
  claimed_from_status: string | null;
  lease_owner: string | null;
  lease_expires_at: string | null;
  last_error: string | null;
  last_error_code: string | null;
  last_error_retryable: boolean | null;
  last_error_details: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  pr_title: string | null;
  pr_author_login: string | null;
  pr_html_url: string | null;
  head_repository: string | null;
  head_ref: string | null;
  base_repository: string | null;
  base_ref: string | null;
  pr_state: string | null;
  pr_is_draft: boolean | null;
  changed_files_count: number | null;
  files_complete: boolean | null;
  diff_complete: boolean | null;
  context_fetched_at: string | null;
  ci_state: string | null;
  ci_checks_complete: boolean | null;
  ci_checked_at: string | null;
  review_plan_id: string | null;
  plan_created_at: string | null;
  plan_file_count: number | null;
  plan_unit_count: number | null;
  plan_rule_count: number | null;
  plan_input_bytes: number | null;
  plan_rules_complete: boolean | null;
  plan_file_decisions: Record<string, number>;
  model_review_completed_at: string | null;
  model_call_id: string | null;
  model_provider: string | null;
  model_protocol: string | null;
  model_name: string | null;
  model_status: string | null;
  model_response_status: number | null;
  model_duration_ms: number | null;
  model_input_tokens: number | null;
  model_output_tokens: number | null;
  model_cache_read_tokens: number | null;
  model_cache_write_tokens: number | null;
  model_reasoning_tokens: number | null;
  model_cost_microusd: number | null;
  model_finding_count: number | null;
  model_created_at: string | null;
  current_stage: string;
  phase: string;
  stages: ReviewStage[];
  available_actions: ReviewAction[];
  verified_finding_count: number;
  rejected_finding_count: number;
  unverified_finding_count: number;
  findings: ReviewFinding[];
  ci_checks: ReviewCiCheck[];
  events: ReviewEvent[];
}

export interface WorkerSnapshot {
  configured: boolean;
  online: boolean;
  worker_id: string | null;
  status: WorkerStatus | null;
  current_task_id: string | null;
  started_at: string | null;
  last_seen_at: string | null;
}

export interface DashboardSnapshot {
  generated_at: string;
  total_reviews: number;
  status_counts: Record<ExecutionStatus, number>;
  worker: WorkerSnapshot;
  recent_reviews: ReviewItem[];
}

export interface ReviewRequest {
  installation_id: number;
  repository_id: number;
  repository: string;
  pull_request_number: number;
  head_sha: string;
}

export interface ReviewAccepted {
  review_run_id: string;
  review_task_id: string;
  review_version_key: string;
  execution_status: ExecutionStatus;
  accepted_at: string;
  created: boolean;
}

export type AiProvider = "openai" | "anthropic";
export type AiApiProtocol = "responses" | "chat_completions" | "messages";
export type AiTestStatus = "untested" | "succeeded" | "failed";
export type AiReasoningEffort = "none" | "low" | "medium" | "high" | "max";

export interface AiProviderSettings {
  provider: AiProvider;
  configured: boolean;
  active: boolean;
  model: string;
  api_protocol: AiApiProtocol;
  api_base_url: string | null;
  reasoning_effort: AiReasoningEffort;
  api_key_configured: boolean;
  api_key_mask: string | null;
  context_window_tokens: number;
  max_output_tokens: number;
  max_batch_input_tokens: number;
  connect_timeout_seconds: number;
  read_timeout_seconds: number;
  write_timeout_seconds: number;
  pool_timeout_seconds: number;
  max_request_bytes: number;
  max_response_bytes: number;
  input_usd_per_million: string | null;
  output_usd_per_million: string | null;
  cache_read_usd_per_million: string | null;
  cache_write_usd_per_million: string | null;
  test_status: AiTestStatus;
  tested_at: string | null;
  updated_at: string | null;
}

export interface AiSettings {
  revision: number;
  active_provider: AiProvider | null;
  max_units: number;
  max_scope_depth: number;
  max_unit_input_bytes: number;
  max_total_input_bytes: number;
  updated_at: string | null;
  updated_by: string | null;
  providers: AiProviderSettings[];
}

export type ReviewAgent = "security" | "convention" | "logic" | "summary";

export interface AiAgentSettings {
  agent: ReviewAgent;
  configured: boolean;
  enabled: boolean;
  provider: AiProvider;
  model: string;
  api_protocol: AiApiProtocol;
  api_base_url: string | null;
  reasoning_effort: AiReasoningEffort;
  api_key_configured: boolean;
  api_key_mask: string | null;
  context_window_tokens: number;
  max_output_tokens: number;
  max_batch_input_tokens: number;
  connect_timeout_seconds: number;
  read_timeout_seconds: number;
  write_timeout_seconds: number;
  pool_timeout_seconds: number;
  max_retries: number;
  test_status: AiTestStatus;
  tested_at: string | null;
  updated_at: string | null;
}

export interface AiAgentSettingsResponse {
  revision: number;
  agents: AiAgentSettings[];
}

export interface AiProviderUpdate {
  expected_revision: number;
  model: string;
  api_protocol: AiApiProtocol;
  api_base_url: string | null;
  api_key: string | null;
  clear_api_key: boolean;
  reasoning_effort: AiReasoningEffort;
  context_window_tokens: number;
  max_output_tokens: number;
  max_batch_input_tokens: number;
  connect_timeout_seconds: number;
  read_timeout_seconds: number;
  write_timeout_seconds: number;
  pool_timeout_seconds: number;
  max_request_bytes: number;
  max_response_bytes: number;
  input_usd_per_million: string | null;
  output_usd_per_million: string | null;
  cache_read_usd_per_million: string | null;
  cache_write_usd_per_million: string | null;
}

export interface ReviewPolicyUpdate {
  expected_revision: number;
  max_units: number;
  max_scope_depth: number;
  max_unit_input_bytes: number;
  max_total_input_bytes: number;
}

export interface ConfigurationAudit {
  revision: number;
  actor: string;
  action: string;
  changed_fields: string[];
  created_at: string;
}

export interface ConfigurationAuditList {
  items: ConfigurationAudit[];
}

export interface KnowledgeVersion {
  version: number;
  content_sha256: string;
  byte_size: number;
  created_by: string;
  created_at: string;
}

export interface KnowledgeDocumentSummary {
  id: string;
  source: string;
  title: string;
  enabled: boolean;
  archived: boolean;
  current_version: number;
  content_sha256: string;
  byte_size: number;
  created_by: string;
  updated_by: string;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeDocument extends KnowledgeDocumentSummary {
  content: string;
  versions: KnowledgeVersion[];
}

export interface KnowledgeLibrary {
  revision: number;
  total: number;
  enabled_count: number;
  total_enabled_bytes: number;
  items: KnowledgeDocumentSummary[];
}

export interface KnowledgeMutation {
  revision: number;
  document: KnowledgeDocument;
}

export interface KnowledgeCitation {
  source: string;
  heading: string;
  score: number;
  excerpt: string;
  version: string;
}

export interface KnowledgeSearchResult {
  query: string;
  items: KnowledgeCitation[];
}
