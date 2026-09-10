import { lazy, useCallback, useEffect, useState } from "react";

import { api, ApiError } from "./api";
import { LoadingScreen, Login } from "./Auth";
import PageBoundary from "./PageBoundary";
import { hasPermission } from "./rbac";
import type { AuthUser } from "./types";
import "./styles/workspace-polish.css";

const DashboardPage = lazy(() => import("./DashboardPage"));
const KnowledgePage = lazy(() => import("./KnowledgePage"));
const ReviewDetailPage = lazy(() => import("./ReviewDetailPage"));
const SettingsPage = lazy(() => import("./SettingsPage"));
const RetrievalPage = lazy(() => import("./RetrievalPage"));

type SessionState =
  | { phase: "checking" }
  | { phase: "guest"; message?: string }
  | { phase: "authenticated"; user: AuthUser };

export type AppView =
  | { kind: "dashboard" }
  | { kind: "settings" }
  | { kind: "knowledge" }
  | { kind: "retrieval"; reviewRunId?: string }
  | { kind: "review"; reviewRunId: string };

export function readAppView(hash = window.location.hash): AppView {
  if (hash === "#settings") return { kind: "settings" };
  if (hash === "#knowledge") return { kind: "knowledge" };
  if (hash === "#retrieval") return {kind: "retrieval"};
  if (hash.startsWith("#retrieval/")) {
    try { return {kind: "retrieval", reviewRunId: decodeURIComponent(hash.slice("#retrieval/".length))}; } catch { return {kind: "dashboard"}; }
  }
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
  return <PageBoundary><AppContent /></PageBoundary>;
}

function AppContent() {
  const [session, setSession] = useState<SessionState>({ phase: "checking" });
  const [view, setView] = useState<AppView>(readAppView);

  // 页面组件会把这些回调放进数据加载 effect 的依赖。保持引用稳定，
  // 避免一次无关的 App 重渲染就重新建立 SSE 或重复读取页面数据。
  const onSignedOut = useCallback((message?: string) => {
    window.location.hash = "";
    setSession({ phase: "guest", message });
  }, []);
  const onAuthenticated = useCallback((user: AuthUser) => {
    setSession({ phase: "authenticated", user });
  }, []);
  const onBack = useCallback(() => {
    window.location.hash = "";
  }, []);
  const onOpenSettings = useCallback(() => {
    window.location.hash = "settings";
  }, []);
  const onOpenKnowledge = useCallback(() => {
    window.location.hash = "knowledge";
  }, []);
  const onOpenRetrieval = useCallback(() => { window.location.hash = "retrieval"; }, []);
  const onOpenReview = useCallback((reviewRunId: string) => {
    window.location.hash = `review/${encodeURIComponent(reviewRunId)}`;
  }, []);

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
      || ((view.kind === "knowledge" || view.kind === "retrieval") && !hasPermission(session.user, "knowledge:manage"));
    if (denied) window.location.hash = "";
  }, [session, view]);

  if (session.phase === "checking") return <LoadingScreen />;
  if (session.phase === "guest") {
    return (
      <Login
        initialMessage={session.message}
        onAuthenticated={onAuthenticated}
      />
    );
  }
  if (view.kind === "settings" && hasPermission(session.user, "settings:manage")) {
    return (
      <SettingsPage
        user={session.user}
        onBack={onBack}
        onSignedOut={onSignedOut}
      />
    );
  }
  if (view.kind === "knowledge" && hasPermission(session.user, "knowledge:manage")) {
    return (
      <KnowledgePage
        user={session.user}
        onBack={onBack}
        onOpenSettings={onOpenSettings}
        onSignedOut={onSignedOut}
      />
    );
  }
  if (view.kind === "retrieval" && hasPermission(session.user, "knowledge:manage")) {
    return <RetrievalPage user={session.user} onBack={onBack} onSignedOut={onSignedOut} initialReviewRunId={view.reviewRunId} />;
  }
  if (view.kind === "review") {
    return (
      <ReviewDetailPage
        user={session.user}
        reviewRunId={view.reviewRunId}
        onBack={onBack}
        onOpenReview={onOpenReview}
        onSignedOut={onSignedOut}
      />
    );
  }
  return (
    <DashboardPage
      user={session.user}
      onSignedOut={onSignedOut}
      onOpenSettings={onOpenSettings}
      onOpenKnowledge={onOpenKnowledge}
      onOpenRetrieval={onOpenRetrieval}
      onOpenReview={onOpenReview}
    />
  );
}
