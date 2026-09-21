import { Notice } from "./Feedback";
import { useCallback, useState } from "react";

import { ApiError } from "./api";
import { hasPermission } from "./rbac";
import type { AuthUser } from "./types";
import UsagePanel from "./UsagePanel";
import WorkItemsPanel from "./WorkItemsPanel";
import ReviewProfilesPanel from "./ReviewProfilesPanel";
import DiagnosticsPanel from "./DiagnosticsPanel";
import { WorkspaceHeader } from "./Workspace";

export type PlatformTab = "work" | "usage" | "profiles" | "diagnostics";
export interface PlatformPanelProps { onError: (error: unknown) => void; }

export default function PlatformPage({ user, findingId, initialTab, onSignedOut }: {
  user: AuthUser; findingId?: string; initialTab?: PlatformTab; onSignedOut: (message?: string) => void;
}) {
  const manager = hasPermission(user, "settings:manage");
  const tab: PlatformTab = manager ? initialTab ?? "work" : "work";
  const [error, setError] = useState("");
  const onError = useCallback((failure: unknown) => {
    if (failure instanceof ApiError && failure.status === 401) { onSignedOut("登录已失效，请重新登录"); return; }
    setError(failure instanceof Error ? failure.message : "操作暂时无法完成");
  }, [onSignedOut]);
  const headings = {
    work: ["问题处理", "跟进需要修复的问题，处理等待核对与批准的审查结果。"],
    usage: ["用量与费用", "按项目查看模型请求和费用估算，核对预算与待确认支出。"],
    profiles: ["配置版本", "保存一套固定的模型与规则，用于重复验证或对比；日常审查可沿用当前设置。"],
    diagnostics: ["运行状态", "任务不动或调用失败时，在这里检查后台服务、请求与错误。"],
  };
  return <main className="workspace-page platform-page">
    <WorkspaceHeader title={headings[tab][0]} icon="review" description={headings[tab][1]} />
    {tab === "profiles" && <p className="workspace-purpose"><a href="#settings">返回模型与审查设置 →</a></p>}
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    {tab === "work" && <WorkItemsPanel user={user} findingId={findingId} onError={onError} />}
    {tab === "usage" && manager && <UsagePanel onError={onError} />}
    {tab === "profiles" && manager && <ReviewProfilesPanel onError={onError} />}
    {tab === "diagnostics" && manager && <DiagnosticsPanel onError={onError} />}
  </main>;
}
