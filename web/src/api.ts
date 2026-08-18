import type {
  AuthUser,
  DashboardSnapshot,
  ReviewAccepted,
  ReviewRequest,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) message = body.detail;
    } catch {
      // Keep the safe HTTP fallback when a proxy returns a non-JSON error page.
    }
    throw new ApiError(message, response.status);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  me: () => request<AuthUser>("/api/v1/auth/me"),
  login: (username: string, password: string) =>
    request<AuthUser>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () =>
    request<void>("/api/v1/auth/logout", {
      method: "POST",
    }),
  dashboard: () => request<DashboardSnapshot>("/api/v1/dashboard"),
  createReview: (payload: ReviewRequest, idempotencyKey: string) =>
    request<ReviewAccepted>("/api/v1/reviews", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(payload),
    }),
};
