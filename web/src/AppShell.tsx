import { useCallback, type ReactNode } from "react";

import { api } from "./api";
import type { AppView } from "./App";
import { Brand } from "./Auth";
import { hasPermission, roleLabels } from "./rbac";
import type { AuthUser } from "./types";

interface ShellNavItem {
  key: "dashboard" | "retrieval" | "knowledge" | "settings";
  label: string;
  hash: string;
  permission?: "knowledge:manage" | "settings:manage";
  icon: ReactNode;
}

const iconProps = {
  width: 13,
  height: 13,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2.2,
} as const;

const NAV_ITEMS: ReadonlyArray<ShellNavItem> = [
  {
    key: "dashboard",
    label: "审查控制台",
    hash: "",
    icon: (
      <svg {...iconProps}><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>
    ),
  },
  {
    key: "retrieval",
    label: "代码检索",
    hash: "retrieval",
    permission: "knowledge:manage",
    icon: (
      <svg {...iconProps}><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
    ),
  },
  {
    key: "knowledge",
    label: "知识库",
    hash: "knowledge",
    permission: "knowledge:manage",
    icon: (
      <svg {...iconProps}><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
    ),
  },
  {
    key: "settings",
    label: "AI 设置",
    hash: "settings",
    permission: "settings:manage",
    icon: (
      <svg {...iconProps}>
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56V21h-4v-.08A1.7 1.7 0 0 0 8.96 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.56-1.03H3v-4h.08A1.7 1.7 0 0 0 4.6 8.96a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 8.96 4.6 1.7 1.7 0 0 0 10 3.08V3h4v.08a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9c.14.6.67 1.02 1.29 1.03H21v4h-.31c-.62 0-1.15.42-1.29 1.03Z" />
      </svg>
    ),
  },
];

function activeNavKey(view: AppView): ShellNavItem["key"] {
  if (view.kind === "retrieval") return "retrieval";
  if (view.kind === "knowledge") return "knowledge";
  if (view.kind === "settings") return "settings";
  return "dashboard";
}

interface AppShellProps {
  user: AuthUser;
  view: AppView;
  onSignedOut: (message?: string) => void;
  children: ReactNode;
}

/**
 * 登录后的统一外壳：持久顶栏 + 全局导航。
 * 导航只改 hash，由 App 的 hashchange 监听切换内容区，
 * 顶栏本身不随页面切换重建。
 */
export default function AppShell({ user, view, onSignedOut, children }: AppShellProps) {
  const current = activeNavKey(view);
  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      onSignedOut();
    }
  }, [onSignedOut]);

  return (
    <div className="app-shell">
      <header className="console-topbar app-shell-topbar">
        <div className="app-shell-brand">
          <Brand />
        </div>

        <nav className="app-shell-nav" aria-label="工作区导航">
          {NAV_ITEMS.filter((item) => !item.permission || hasPermission(user, item.permission)).map((item) =>
            item.key === current ? (
              <span key={item.key} className="switch-item is-current" aria-current="page">
                {item.icon}
                {item.label}
              </span>
            ) : (
              <button
                key={item.key}
                type="button"
                className="switch-item"
                onClick={() => {
                  window.location.hash = item.hash;
                }}
              >
                {item.icon}
                {item.label}
              </button>
            ),
          )}
        </nav>

        <div className="app-shell-topbar-right">
          <div className="console-user-pill">
            <div className="user-avatar-sun">{user.username.slice(0, 1).toUpperCase()}</div>
            <span className="user-identity">
              <span className="user-username">{user.username}</span>
              <small>{roleLabels[user.role]}</small>
            </span>
          </div>
          <button type="button" className="console-icon-btn" onClick={() => void logout()} title="退出登录" aria-label="退出登录">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
              <polyline points="16 17 21 12 16 7"/>
              <line x1="21" y1="12" x2="9" y2="12"/>
            </svg>
          </button>
        </div>
      </header>
      {children}
    </div>
  );
}
