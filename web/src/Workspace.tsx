import { Button } from "./components/ui/button";
import { Badge } from "./components/ui/badge";
import { Card, CardContent } from "./components/ui/card";
import type { ReactNode } from "react";

export function WorkspaceIcon({ kind = "grid" }: { kind?: "grid" | "team" | "review" | "chart" | "file" | "check" | "search" }) {
  const paths = {
    grid: <><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></>,
    team: <><circle cx="9" cy="8" r="3" /><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6M18 15a5 5 0 0 1 3 4" /></>,
    review: <><rect x="5" y="3" width="14" height="18" rx="2" /><path d="m8 9 2 2 4-4M8 15h8M8 18h5" /></>,
    chart: <path d="M4 4v16h17M8 16v-4m5 4V7m5 9v-6" />,
    file: <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 13h8M8 17h5" />,
    check: <><circle cx="12" cy="12" r="9" /><path d="m8 12 3 3 5-6" /></>,
    search: <><circle cx="10.5" cy="10.5" r="7.5" /><path d="m16 16 5 5" /></>,
  };
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[kind]}</svg>;
}

export function WorkspaceHeader({ title, description, icon, actions }: {
  title: string; description: string; icon?: Parameters<typeof WorkspaceIcon>[0]["kind"]; actions?: ReactNode;
}) {
  return <header className="ws-page-header"><div className="ws-page-heading"><span className="ws-page-icon"><WorkspaceIcon kind={icon} /></span><div><h1>{title}</h1><p>{description}</p></div></div>{actions && <div className="ws-actions">{actions}</div>}</header>;
}

export function WorkspaceBadge({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "accent" | "success" | "warning" | "danger" }) {
  const colors = { neutral: "bg-slate-100 text-slate-700", accent: "bg-indigo-50 text-indigo-700", success: "bg-emerald-50 text-emerald-700", warning: "bg-amber-50 text-amber-800", danger: "bg-red-50 text-red-700" };
  return <Badge variant="outline" className={`ws-badge is-${tone} ${colors[tone]}`}><span className="ws-badge-dot" />{children}</Badge>;
}

export function WorkspaceEmpty({ title, description, loading = false }: { title: string; description?: string; loading?: boolean }) {
  return <div className={`ws-empty${loading ? " is-loading" : ""}`} role={loading ? "status" : undefined}><span className="ws-empty-icon"><WorkspaceIcon kind={loading ? "grid" : "file"} /></span><strong>{title}</strong>{description && <p>{description}</p>}</div>;
}

export function WorkspaceSection({ title, description, children }: { title: string; description?: string; children: ReactNode }) {
  return <Card className="ws-form-section grid gap-5 p-5 shadow-none"><div className="ws-section-copy"><h3>{title}</h3>{description && <p>{description}</p>}</div><CardContent className="ws-section-fields min-w-0 p-0">{children}</CardContent></Card>;
}

export function WorkspaceBack({ onClick, children = "返回列表" }: { onClick: () => void; children?: ReactNode }) {
  return <Button variant="outline" type="button" className="ws-back" onClick={onClick}><span aria-hidden="true">←</span>{children}</Button>;
}
