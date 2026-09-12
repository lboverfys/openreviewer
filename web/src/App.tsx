import { lazy, Suspense, useCallback, useEffect, useState, type ReactNode } from "react";

import { api, ApiError } from "./api";
import AppShell from "./AppShell";
import { LoadingScreen, Login } from "./Auth";
import PageBoundary from "./PageBoundary";
import { pageLoaders } from "./page-loaders";
import { hasPermission } from "./rbac";
import type { AuthUser } from "./types";
import "./styles/workspace-polish.css";

const DashboardPage = lazy(pageLoaders.dashboard);
const KnowledgePage = lazy(pageLoaders.knowledge);
const ReviewDetailPage = lazy(pageLoaders.review);
const SettingsPage = lazy(pageLoaders.settings);
const RetrievalPage = lazy(pageLoaders.retrieval);
const TeamPage = lazy(pageLoaders.team);
const EvaluationPage = lazy(pageLoaders.evaluations);
const PlatformPage = lazy(pageLoaders.platform);

type SessionState =
  | { phase: "checking" }
  | { phase: "guest"; message?: string }
  | { phase: "authenticated"; user: AuthUser };

export type AppView =
  | { kind: "dashboard" }
  | { kind: "settings" }
  | { kind: "team" }
  | { kind: "platform"; findingId?: string; tab?: import("./PlatformPage").PlatformTab }
  | { kind: "evaluations"; datasetId?: string; caseId?: string; reviewRunId?: string }
  | { kind: "knowledge" }
  | { kind: "retrieval"; reviewRunId?: string }
  | { kind: "review"; reviewRunId: string };

export function readAppView(hash = window.location.hash): AppView {
  if (hash === "#platform" || hash.startsWith("#platform?")) {
    const params = new URLSearchParams(hash.split("?", 2)[1]);
    const tab = params.get("tab");
    return { kind: "platform", findingId: params.get("finding") || undefined,
      tab: tab === "usage" || tab === "profiles" || tab === "diagnostics" ? tab : "work" };
  }
  if (hash === "#evaluations" || hash.startsWith("#evaluations/") || hash.startsWith("#evaluations?")) {
    try {
      const [path, query] = hash.split("?", 2);
      const params = new URLSearchParams(query);
      return {kind:"evaluations", datasetId: path.startsWith("#evaluations/") ? decodeURIComponent(path.slice(13)) || undefined : undefined,
        caseId:params.get("case") || undefined, reviewRunId:params.get("review") || undefined};
    } catch { return {kind:"dashboard"}; }
  }
  if (hash === "#team") return { kind: "team" };
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
      ((view.kind === "settings" || view.kind === "team") && !hasPermission(session.user, "settings:manage"))
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

  let page: ReactNode;
  if (view.kind === "platform") {
    page = <PlatformPage key={`${view.tab}:${view.findingId}`} user={session.user} findingId={view.findingId} initialTab={view.tab} onSignedOut={onSignedOut} />;
  } else if (view.kind === "evaluations") {
    page = <EvaluationPage user={session.user} datasetId={view.datasetId} caseId={view.caseId} reviewRunId={view.reviewRunId} onSignedOut={onSignedOut} />;
  } else if (view.kind === "team" && hasPermission(session.user, "settings:manage")) {
    page = <TeamPage onSignedOut={onSignedOut} />;
  } else if (view.kind === "settings" && hasPermission(session.user, "settings:manage")) {
    page = <SettingsPage onSignedOut={onSignedOut} />;
  } else if (view.kind === "knowledge" && hasPermission(session.user, "knowledge:manage")) {
    page = <KnowledgePage onSignedOut={onSignedOut} />;
  } else if (view.kind === "retrieval" && hasPermission(session.user, "knowledge:manage")) {
    page = <RetrievalPage onSignedOut={onSignedOut} initialReviewRunId={view.reviewRunId} />;
  } else if (view.kind === "review") {
    page = (
      <ReviewDetailPage
        key={view.reviewRunId}
        user={session.user}
        reviewRunId={view.reviewRunId}
        onBack={onBack}
        onOpenReview={onOpenReview}
        onSignedOut={onSignedOut}
      />
    );
  } else {
    page = (
      <DashboardPage
        user={session.user}
        onSignedOut={onSignedOut}
        onOpenReview={onOpenReview}
      />
    );
  }
  return (
    <AppShell user={session.user} view={view} onSignedOut={onSignedOut}>
      <Suspense fallback={<main className="page-loading" role="status">正在加载页面…</main>}>
        {page}
      </Suspense>
    </AppShell>
  );
}
