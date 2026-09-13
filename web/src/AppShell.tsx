import { lazy, Suspense, useCallback, useEffect, useState, type ReactNode } from "react";

import { api } from "./api";
import type { AppView } from "./App";
import { hasPermission, roleLabels } from "./rbac";
import { preloadPage } from "./page-loaders";
import type { AuthUser } from "./types";
import { WorkspaceIcon } from "./Workspace";
import "./styles/enterprise.css";

const UserManual = lazy(() => import("./UserManual"));
type NavKey = "dashboard" | "work" | "settings" | "knowledge" | "team" | "evaluations" | "usage" | "diagnostics";
interface NavItem {
  key: NavKey; label: string; hash: string;
  page: "dashboard" | "platform" | "settings" | "knowledge" | "team" | "evaluations";
  permission?: "knowledge:manage" | "settings:manage";
  icon: "grid" | "team" | "review" | "chart" | "file" | "check";
}
const NAV_GROUPS: ReadonlyArray<{ label: string; items: NavItem[] }> = [
  { label: "审查工作", items: [
    { key: "dashboard", label: "审查任务", hash: "", page: "dashboard", icon: "review" },
    { key: "work", label: "问题处理", hash: "platform?tab=work", page: "platform", icon: "check" },
  ] },
  { label: "项目配置", items: [
    { key: "settings", label: "模型与审查设置", hash: "settings", page: "settings", permission: "settings:manage", icon: "grid" },
    { key: "knowledge", label: "规则文档", hash: "knowledge", page: "knowledge", permission: "knowledge:manage", icon: "file" },
    { key: "team", label: "项目与成员", hash: "team", page: "team", permission: "settings:manage", icon: "team" },
  ] },
  { label: "分析与维护", items: [
    { key: "evaluations", label: "效果评测", hash: "evaluations", page: "evaluations", icon: "chart" },
    { key: "usage", label: "用量与费用", hash: "platform?tab=usage", page: "platform", permission: "settings:manage", icon: "chart" },
    { key: "diagnostics", label: "运行状态", hash: "platform?tab=diagnostics", page: "platform", permission: "settings:manage", icon: "grid" },
  ] },
];

function activeKey(view: AppView): NavKey {
  if (view.kind === "platform") return view.tab === "profiles" ? "settings" : view.tab === "usage" || view.tab === "diagnostics" ? view.tab : "work";
  if (view.kind === "retrieval" || view.kind === "knowledge") return "knowledge";
  if (view.kind === "team" || view.kind === "settings" || view.kind === "evaluations") return view.kind;
  return "dashboard";
}

export default function AppShell({ user, view, onSignedOut, children }: {
  user: AuthUser; view: AppView; onSignedOut: (message?: string) => void; children: ReactNode;
}) {
  const current = activeKey(view);
  const [menuOpen, setMenuOpen] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const closeManual = useCallback(() => setManualOpen(false), []);
  useEffect(() => {
    const closeMenu = (event: KeyboardEvent) => { if (event.key === "Escape") setMenuOpen(false); };
    window.addEventListener("keydown", closeMenu);
    return () => window.removeEventListener("keydown", closeMenu);
  }, []);
  const logout = useCallback(async () => {
    try { await api.logout(); } finally { onSignedOut(); }
  }, [onSignedOut]);

  return <div className="app-shell enterprise-shell">
    <header className="enterprise-topbar">
      <div className="enterprise-brand">
        <button type="button" className="enterprise-menu-toggle" aria-label="展开导航菜单" aria-expanded={menuOpen}
          aria-controls="primary-navigation" onClick={() => setMenuOpen(value => !value)}>☰</button>
        <a href="#" onClick={() => setMenuOpen(false)}><strong>OpenReviewer</strong><span>代码审查平台</span></a>
      </div>
      <div className="enterprise-account">
        <button type="button" className="enterprise-help-button" onClick={() => { setMenuOpen(false); setManualOpen(true); }}>使用手册</button>
        <span className="enterprise-user"><strong>{user.username}</strong><small>{roleLabels[user.role]}</small></span>
        <button type="button" className="enterprise-logout" onClick={() => void logout()}>退出登录</button>
      </div>
    </header>
    <div className="enterprise-body">
      {menuOpen && <button className="enterprise-menu-backdrop" aria-label="关闭导航菜单" onClick={() => setMenuOpen(false)} />}
      <aside id="primary-navigation" className={"enterprise-sidebar" + (menuOpen ? " is-open" : "")}>
        <nav aria-label="工作区导航">{NAV_GROUPS.map(group => {
          const items = group.items.filter(item => !item.permission || hasPermission(user, item.permission));
          return items.length > 0 && <section className="enterprise-nav-group" key={group.label}>
            <h2>{group.label}</h2>
            {items.map(item => <a key={item.key} href={"#" + item.hash} aria-current={current === item.key ? "page" : undefined}
              onPointerEnter={() => preloadPage(item.page)} onFocus={() => preloadPage(item.page)}
              onClick={() => { preloadPage(item.page); setMenuOpen(false); }}>
              <WorkspaceIcon kind={item.icon} /><span>{item.label}</span>
            </a>)}
          </section>;
        })}<div className="enterprise-sidebar-account"><strong>{user.username}</strong><small>{roleLabels[user.role]}</small><button type="button" onClick={() => void logout()}>退出当前账号</button></div></nav>
      </aside>
      <div className="enterprise-content">{children}</div>
    </div>
    {manualOpen && <Suspense fallback={<div className="manual-loading" role="status">正在打开使用手册…</div>}>
      <UserManual initialTopic={view.kind === "review" ? "review" : current} onClose={closeManual} />
    </Suspense>}
  </div>;
}
