import { Button } from "./components/ui/button";
import { ArrowUpRight, BookOpen, LogOut, Menu, X } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { api } from "./api";
import type { AppView } from "./App";
import { hasPermission, roleLabels } from "./rbac";
import { preloadPage } from "./page-loaders";
import type { AuthUser } from "./types";
import { WorkspaceIcon } from "./Workspace";
import BrandMark from "./BrandMark";

const UserManual = lazy(() => import("./UserManual"));
type NavKey = "dashboard" | "work" | "settings" | "knowledge" | "retrieval" | "team" | "evaluations" | "usage" | "diagnostics";
interface NavItem {
  key: NavKey; label: string; hash: string;
  page: "dashboard" | "platform" | "settings" | "knowledge" | "retrieval" | "team" | "evaluations";
  permission?: "knowledge:manage" | "settings:manage";
  icon: "grid" | "team" | "review" | "chart" | "file" | "check" | "search";
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
    { key: "retrieval", label: "代码检索", hash: "retrieval", page: "retrieval", permission: "knowledge:manage", icon: "search" },
    { key: "evaluations", label: "效果评测", hash: "evaluations", page: "evaluations", icon: "chart" },
    { key: "usage", label: "用量与费用", hash: "platform?tab=usage", page: "platform", permission: "settings:manage", icon: "chart" },
    { key: "diagnostics", label: "运行状态", hash: "platform?tab=diagnostics", page: "platform", permission: "settings:manage", icon: "grid" },
  ] },
];

function activeKey(view: AppView): NavKey {
  if (view.kind === "platform") return view.tab === "profiles" ? "settings" : view.tab === "usage" || view.tab === "diagnostics" ? view.tab : "work";
  if (view.kind === "retrieval" || view.kind === "knowledge") return view.kind;
  if (view.kind === "team" || view.kind === "settings" || view.kind === "evaluations") return view.kind;
  return "dashboard";
}

export default function AppShell({ user, view, onSignedOut, children }: {
  user: AuthUser; view: AppView; onSignedOut: (message?: string) => void; children: ReactNode;
}) {
  const current = activeKey(view);
  const [menuOpen, setMenuOpen] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const sidebarRef = useRef<HTMLElement>(null);
  const closeManual = useCallback(() => setManualOpen(false), []);
  useEffect(() => {setMenuOpen(false); setManualOpen(false);}, [view]);
  useEffect(() => {
    if (!menuOpen) return;
    const previousFocus = document.activeElement as HTMLElement;
    const previousOverflow = document.body.style.overflow;
    const desktop = window.matchMedia("(min-width: 901px)");
    const closeOnDesktop = () => { if (desktop.matches) setMenuOpen(false); };
    document.body.style.overflow = "hidden";
    sidebarRef.current?.querySelector<HTMLElement>("button, a[href]")?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
      if (event.key !== "Tab") return;
      const targets = [...(sidebarRef.current?.querySelectorAll<HTMLElement>("a[href], button:not(:disabled)") ?? [])].filter(element => element.getClientRects().length > 0);
      const first = targets[0];
      const last = targets[targets.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    window.addEventListener("keydown", handleKey);
    desktop.addEventListener("change", closeOnDesktop);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", handleKey);
      desktop.removeEventListener("change", closeOnDesktop);
      previousFocus?.focus();
    };
  }, [menuOpen]);
  const logout = useCallback(async () => {
    try { await api.logout(); } finally { onSignedOut(); }
  }, [onSignedOut]);

  return <div className="app-shell enterprise-shell">
    <a className="skip-to-content" href="#workspace-content" onClick={event => { event.preventDefault(); document.getElementById("workspace-content")?.focus(); }}>跳至主要内容</a>
    <header className="enterprise-topbar">
      <div className="enterprise-brand">
        <Button variant="ghost" size="icon" type="button" className="enterprise-menu-toggle" aria-label="展开导航菜单" aria-expanded={menuOpen}
          aria-controls="primary-navigation" onClick={() => setMenuOpen(value => !value)}><Menu aria-hidden="true" /></Button>
        <a href="#" onClick={() => setMenuOpen(false)}><BrandMark className="enterprise-brand-mark" /><strong>OpenReviewer<span className="enterprise-brand-period">.</span></strong></a>
      </div>
      <div className="enterprise-location"><span>工作空间</span><span aria-hidden="true">/</span><strong>{view.kind === "review" ? "审查详情" : NAV_GROUPS.flatMap(group => group.items).find(item => item.key === current)?.label}</strong></div>
      <div className="enterprise-account">
        <Button variant="ghost" type="button" className="enterprise-help-button" onClick={() => { setMenuOpen(false); setManualOpen(true); }}><BookOpen aria-hidden="true" />使用手册</Button>
        <span className="enterprise-avatar" aria-hidden="true">{user.username.slice(0, 1).toUpperCase()}</span><span className="enterprise-user"><strong>{user.username}</strong><small>{roleLabels[user.role]}</small></span>
        <Button variant="ghost" type="button" className="enterprise-logout" onClick={() => void logout()}><LogOut aria-hidden="true" />退出登录</Button>
      </div>
    </header>
    <div className="enterprise-body">
      {menuOpen && <Button variant="outline" className="enterprise-menu-backdrop" aria-label="关闭导航菜单" onClick={() => setMenuOpen(false)} />}
      <aside ref={sidebarRef} id="primary-navigation" className={"enterprise-sidebar" + (menuOpen ? " is-open" : "")} role={menuOpen ? "dialog" : undefined} aria-modal={menuOpen || undefined} aria-label={menuOpen ? "导航菜单" : undefined}>
        <nav aria-label="工作区导航"><div className="enterprise-sidebar-heading"><span>REVIEW WORKSPACE</span><Button variant="ghost" size="icon" className="enterprise-sidebar-close" aria-label="收起导航菜单" onClick={() => setMenuOpen(false)}><X /></Button></div>{NAV_GROUPS.map((group, groupIndex) => {
          const items = group.items.filter(item => !item.permission || hasPermission(user, item.permission));
          return items.length > 0 && <section className="enterprise-nav-group" key={group.label}>
            <h2><span>{group.label}</span><span aria-hidden="true">0{groupIndex + 1}</span></h2>
            {items.map(item => <a key={item.key} href={"#" + item.hash} aria-current={current === item.key ? "page" : undefined}
              onPointerEnter={() => preloadPage(item.page)} onFocus={() => preloadPage(item.page)}
              onClick={() => { preloadPage(item.page); setMenuOpen(false); }}>
              <WorkspaceIcon kind={item.icon} /><span>{item.label}</span>
            </a>)}
          </section>;
        })}<button className="enterprise-guide" type="button" onClick={() => { setMenuOpen(false); setManualOpen(true); }}><BookOpen aria-hidden="true" /><strong>把每一步，理清楚。<ArrowUpRight aria-hidden="true" /></strong><span>查看平台使用手册</span></button><div className="enterprise-sidebar-account"><strong>{user.username}</strong><small>{roleLabels[user.role]}</small><Button variant="outline" type="button" onClick={() => void logout()}>退出当前账号</Button></div><div className="enterprise-sidebar-footer"><BrandMark /><span>CONTEXT. EVIDENCE. CLARITY.</span></div></nav>
      </aside>
      <div id="workspace-content" tabIndex={-1} inert={menuOpen} className="enterprise-content">{children}</div>
    </div>
    {manualOpen && <Suspense fallback={<div className="manual-loading" role="status">正在打开使用手册…</div>}>
      <UserManual initialTopic={view.kind === "review" ? "review" : current} onClose={closeManual} />
    </Suspense>}
  </div>;
}
