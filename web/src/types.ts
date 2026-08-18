export type ExecutionStatus =
  | "queued"
  | "waiting_for_ci"
  | "running"
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
