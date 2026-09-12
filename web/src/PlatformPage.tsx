import { useCallback, useState } from "react";

import { ApiError } from "./api";
import { hasPermission } from "./rbac";
import type { AuthUser } from "./types";
import UsagePanel from "./UsagePanel";
import WorkItemsPanel from "./WorkItemsPanel";
import ReviewProfilesPanel from "./ReviewProfilesPanel";
import DiagnosticsPanel from "./DiagnosticsPanel";
import "./styles/platform.css";

export type PlatformTab = "work" | "usage" | "profiles" | "diagnostics";
export interface PlatformPanelProps { onError: (error: unknown) => void; }

export default function PlatformPage({ user, findingId, initialTab, onSignedOut }: {
  user: AuthUser; findingId?: string; initialTab?: PlatformTab; onSignedOut: (message?: string) => void;
}) {
  const manager = hasPermission(user, "settings:manage");
  const [tab, setTab] = useState<PlatformTab>(manager ? initialTab ?? "work" : "work");
  const [error, setError] = useState("");
  const onError = useCallback((failure: unknown) => {
    if (failure instanceof ApiError && failure.status === 401) { onSignedOut("登录已失效，请重新登录"); return; }
    setError(failure instanceof Error ? failure.message : "操作暂时无法完成");
  }, [onSignedOut]);
  return <main className="platform-page">
    <header><p className="team-eyebrow">TEAM OPERATIONS</p><h1>协作与运营</h1><p>处理审查待办，管理仓库用量、审查方案和运行情况。</p></header>
    <nav className="team-tabs" aria-label="协作与运营分类">
      {([["work", "审查待办"], ["usage", "用量与预算"], ["profiles", "审查方案"], ["diagnostics", "运行诊断"]] as const).filter(([key]) => manager || key === "work").map(([key, label]) =>
        <button key={key} aria-pressed={tab === key} onClick={() => { setTab(key); setError(""); }}>{label}</button>)}
    </nav>
    {error && <p role="alert" className="team-error">{error}<button onClick={() => setError("")}>收起提示</button></p>}
    {tab === "work" && <WorkItemsPanel user={user} findingId={findingId} onError={onError} />}
    {tab === "usage" && manager && <UsagePanel onError={onError} />}
    {tab === "profiles" && manager && <ReviewProfilesPanel onError={onError} />}
    {tab === "diagnostics" && manager && <DiagnosticsPanel onError={onError} />}
  </main>;
}
