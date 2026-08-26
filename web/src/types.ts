export type ExecutionStatus =
  | "queued"
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
  execution_status: ExecutionStatus;
  attempt_count: number;
  max_attempts: number;
  last_error: string | null;
  created_at: string;
  updated_at: string;
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

export interface AiProviderSettings {
  provider: AiProvider;
  configured: boolean;
  active: boolean;
  model: string;
  api_protocol: AiApiProtocol;
  api_base_url: string | null;
  api_key_configured: boolean;
  api_key_mask: string | null;
  max_output_tokens: number;
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

export interface AiProviderUpdate {
  expected_revision: number;
  model: string;
  api_protocol: AiApiProtocol;
  api_base_url: string | null;
  api_key: string | null;
  clear_api_key: boolean;
  max_output_tokens: number;
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
