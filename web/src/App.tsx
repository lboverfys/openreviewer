import { useEffect, useState } from "react";

import { api, ApiError } from "./api";
import { LoadingScreen, Login } from "./Auth";
import DashboardPage from "./DashboardPage";
import KnowledgePage from "./KnowledgePage";
import { hasPermission } from "./rbac";
import ReviewDetailPage from "./ReviewDetailPage";
import SettingsPage from "./SettingsPage";
import type { AuthUser } from "./types";

type SessionState =
  | { phase: "checking" }
  | { phase: "guest"; message?: string }
  | { phase: "authenticated"; user: AuthUser };

export type AppView =
  | { kind: "dashboard" }
  | { kind: "settings" }
  | { kind: "knowledge" }
  | { kind: "review"; reviewRunId: string };

export function readAppView(hash = window.location.hash): AppView {
  if (hash === "#settings") return { kind: "settings" };
  if (hash === "#knowledge") return { kind: "knowledge" };
  if (hash.startsWith("#review/")) {
    try {
      const reviewRunId = decodeURIComponent(hash.slice("#review/".length));
      if (reviewRunId) return { kind: "review", reviewRunId };
    } catch {
      return { kind: "dashboard" };
    }
  }
  return { kind: "dashboard" };
}

export default function App() {
  const [session, setSession] = useState<SessionState>({ phase: "checking" });
  const [view, setView] = useState<AppView>(readAppView);

  useEffect(() => {
    function syncViewWithHash() {
      setView(readAppView());
    }
    window.addEventListener("hashchange", syncViewWithHash);
    return () => window.removeEventListener("hashchange", syncViewWithHash);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    api
      .me(controller.signal)
      .then((user) => {
        if (active) setSession({ phase: "authenticated", user });
      })
      .catch((error) => {
        if (!active || controller.signal.aborted) return;
        setSession({
          phase: "guest",
          message:
            error instanceof ApiError && error.status === 401
              ? undefined
              : "服务暂时不可用，请稍后重试",
        });
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, []);

  useEffect(() => {
    if (session.phase !== "authenticated") return;
    const denied =
      (view.kind === "settings" && !hasPermission(session.user, "settings:manage"))
      || (view.kind === "knowledge" && !hasPermission(session.user, "knowledge:manage"));
    if (denied) window.location.hash = "";
  }, [session, view]);

  if (session.phase === "checking") return <LoadingScreen />;
  if (session.phase === "guest") {
    return (
      <Login
        initialMessage={session.message}
        onAuthenticated={(user) => setSession({ phase: "authenticated", user })}
      />
    );
  }
  if (view.kind === "settings" && hasPermission(session.user, "settings:manage")) {
    return (
      <SettingsPage
        user={session.user}
        onBack={() => {
          window.location.hash = "";
        }}
        onSignedOut={(message) => {
          window.location.hash = "";
          setSession({ phase: "guest", message });
        }}
      />
    );
  }
  if (view.kind === "knowledge" && hasPermission(session.user, "knowledge:manage")) {
    return (
      <KnowledgePage
        user={session.user}
        onBack={() => {
          window.location.hash = "";
        }}
        onOpenSettings={() => {
          window.location.hash = "settings";
        }}
        onSignedOut={(message) => {
          window.location.hash = "";
          setSession({ phase: "guest", message });
        }}
      />
    );
  }
  if (view.kind === "review") {
    return (
      <ReviewDetailPage
        user={session.user}
        reviewRunId={view.reviewRunId}
        onBack={() => {
          window.location.hash = "";
        }}
        onOpenReview={(reviewRunId) => {
          window.location.hash = `review/${encodeURIComponent(reviewRunId)}`;
        }}
        onSignedOut={(message) => {
          window.location.hash = "";
          setSession({ phase: "guest", message });
        }}
      />
    );
  }
  return (
    <DashboardPage
      user={session.user}
      onSignedOut={(message) => setSession({ phase: "guest", message })}
      onOpenSettings={() => {
        window.location.hash = "settings";
      }}
      onOpenKnowledge={() => {
        window.location.hash = "knowledge";
      }}
      onOpenReview={(reviewRunId) => {
        window.location.hash = `review/${encodeURIComponent(reviewRunId)}`;
      }}
    />
  );
}
