import { DetailDialog, Notice } from "./Feedback";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { api, ApiError } from "./api";
import RepositoryConnectPanel from "./RepositoryConnectPanel";
import Pagination from "./Pagination";
import { roleLabels } from "./rbac";
import type { AccessRole, TeamMember, TeamMemberWrite, TeamRepository, TeamRepositoryWrite } from "./types";
import { useCursorPage } from "./useCursorPage";
import { formatDate } from "./utils";
import { WorkspaceBack, WorkspaceBadge, WorkspaceEmpty, WorkspaceHeader, WorkspaceSection } from "./Workspace";
import "./styles/team.css";

type Tab = "repositories" | "members" | "audits";
const lines = (value: string) => [...new Set(value.split(/[\n,，]+/).map((item) => item.trim()).filter(Boolean))];

export default function TeamPage({ onSignedOut, initialRepository, initialSection }: { onSignedOut: (message?: string) => void; initialRepository?: string; initialSection?: string }) {
  const [connecting, setConnecting] = useState<TeamRepository | "new" | null>(null);
  const [tab, setTab] = useState<Tab>("repositories");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [administrator, setAdministrator] = useState("");
  const [member, setMember] = useState<TeamMember | "new" | null>(null);
  const [repository, setRepository] = useState<TeamRepository | "new" | null>(null);
  const onError = useCallback((failure: unknown) => {
    if (failure instanceof ApiError && failure.status === 401) {
      onSignedOut("登录已失效，请重新登录");
      return;
    }
    setError(failure instanceof Error ? failure.message : "操作失败，请稍后重试");
  }, [onSignedOut]);
  useEffect(() => {
    if (!initialRepository) return;
    const controller = new AbortController();
    api.teamRepositoryByName(initialRepository, controller.signal).then(page => {
      if (controller.signal.aborted) return;
      if (page.items[0]) setRepository(page.items[0]);
      else setError("该仓库尚未保存项目设置，请先接入仓库，再设置预算。");
    }).catch(error => {if (!controller.signal.aborted) onError(error);});
    return () => controller.abort();
  }, [initialRepository, onError]);
  const loadMembers = useCallback(async (cursor?: string, signal?: AbortSignal, force?: boolean) => {
    const result = await api.teamMembers(cursor, signal, force);
    if (!signal?.aborted) setAdministrator(result.configured_administrator);
    return result;
  }, []);
  const members = useCursorPage({cacheKey: "team-members", load: loadMembers, onError, enabled: tab === "members"});
  const repositories = useCursorPage({cacheKey: "team-repositories", load: api.teamRepositories, onError, enabled: tab === "repositories"});
  const audits = useCursorPage({cacheKey: "team-audits", load: api.teamAudits, onError, enabled: tab === "audits"});
  const page = tab === "members" ? members : tab === "repositories" ? repositories : audits;

  const changeTab = (next: Tab) => {
    setConnecting(null); setTab(next); setMember(null); setRepository(null); setError(""); setMessage("");
  };
  const save = async (operation: () => Promise<unknown>) => {
    setBusy(true); setError(""); setMessage("");
    try {
      await operation();
      setMember(null); setRepository(null);
      setMessage("已保存。成员权限立即更新，仓库策略用于新创建的审查任务。");
      await page.refresh();
    } catch (failure) {
      onError(failure);
    } finally {
      setBusy(false);
    }
  };

  return <main className="workspace-page team-page">
    <WorkspaceHeader title="项目与成员" icon="team" description="项目设置决定审查哪些代码，成员权限决定谁能查看、核对和发布结果。" actions={member || repository
      ? <WorkspaceBack onClick={() => { if (!busy) { setMember(null); setRepository(null); setError(""); } }} />
      : <button type="button" disabled={busy || page.loading} onClick={() => {setError(""); void page.refresh();}}>刷新列表</button>} />
    {!member && !repository && <nav className="team-tabs" aria-label="团队管理分类">
      {([["repositories", "项目设置"], ["members", "成员权限"], ["audits", "操作记录"]] as const).map(([key, label]) =>
        <button key={key} type="button" aria-pressed={tab === key} disabled={busy} onClick={() => changeTab(key)}>{label}</button>,
      )}
    </nav>}
    {error && <Notice kind="error" onDismiss={() => setError("")}>{error}</Notice>}
    {message && <Notice kind="success" onDismiss={() => setMessage("")}>{message}</Notice>}
    {connecting && <RepositoryConnectPanel existing={connecting === "new" ? undefined : connecting} onError={onError} onCancel={() => setConnecting(null)} onSaved={() => {setConnecting(null); setMessage("仓库已接通并启用，新 PR 会自动触发审查。"); void repositories.refresh();}}/>}
    {!member && !repository && !connecting && <section className="team-card">
      <div className="team-toolbar">
        <h2>{tab === "repositories" ? "已配置仓库" : tab === "members" ? "成员权限" : "最近变更"}</h2>
        {tab !== "audits" && <button type="button" className="team-primary" disabled={busy} onClick={() => {
          setError(""); setMessage("");
          if (tab === "members") setMember("new"); else setConnecting("new");
        }}>{tab === "members" ? "添加成员" : "接入仓库"}</button>}
      </div>
      {tab === "repositories" && <p className="team-hint">接入仓库会检查现有 GitHub App 的访问权限；保存策略与成功接通会分别显示。</p>}
      {tab === "members" && <p className="team-hint">初始管理员 {administrator || "正在读取…"} 已纳入下方成员列表，密码可在这里修改；该账号保留启用和管理权限，避免失去登录入口。修改成员角色、范围或密码后，该成员需要重新登录。</p>}
      <div className="team-table-wrap">
        {tab === "repositories" && <table>
          <thead><tr><th>仓库</th><th>接入状态</th><th>新任务</th><th>目标分支</th><th>审批负责人</th><th>请求上限</th><th>版本</th><th>操作</th></tr></thead>
          <tbody>{repositories.data?.items.map((item) => <tr key={item.id}>
            <td><strong>{item.repository}</strong></td><td>{item.connected_at ? <><WorkspaceBadge tone="success">已接通</WorkspaceBadge><small>上次检查：{formatDate(item.connected_at)}</small><small>{item.policy.enabled ? "新 PR 自动触发审查" : "已暂停自动审查"}</small></> : <><WorkspaceBadge>尚未检查接入</WorkspaceBadge><small>{item.connection_error ?? "已保存策略，请检查授权和访问能力"}</small></>}</td><td><WorkspaceBadge tone={item.policy.enabled ? "success" : "neutral"}>{item.policy.enabled ? "接收" : "暂停"}</WorkspaceBadge></td>
            <td>{item.policy.target_branches?.join("、") || "全部"}</td><td>{item.policy.approver || "按角色审批"}</td>
            <td>{item.policy.max_model_requests == null ? "不限制" : item.policy.max_model_requests + " 次"}</td><td>v{item.revision}</td>
            <td><button type="button" aria-label={"编辑仓库 " + item.repository} disabled={busy} onClick={() => {setRepository(item); setMessage("");}}>编辑</button><button disabled={busy} onClick={() => {
              if (!item.connection_installation_id) {setConnecting(item); setMessage(""); return;}
              setBusy(true); void api.checkRepositoryConnection(item.id).then(next => {setMessage(next.connected_at ? "访问检查通过，新 PR 按项目策略自动审查。" : "访问检查未通过：" + next.connection_error); void repositories.refresh();}).catch(onError).finally(() => setBusy(false));
            }}>检查接入</button>{item.connection_error && <button onClick={() => setConnecting(item)}>补充授权</button>}</td>
          </tr>)}</tbody>
        </table>}
        {tab === "members" && <table>
          <thead><tr><th>用户名</th><th>角色</th><th>可见资源</th><th>状态</th><th>版本</th><th>操作</th></tr></thead>
          <tbody>{members.data?.items.map((item) => <tr key={item.username}>
            <td><div className="team-member-identity"><span className="team-member-avatar">{item.username.slice(0, 1).toUpperCase()}</span><strong>{item.username}</strong>{item.username.toLowerCase() === administrator.toLowerCase() && <small>初始管理员</small>}</div></td><td>{roleLabels[item.role]}</td>
            <td>{item.scope.unrestricted ? "全部仓库" : [...(item.scope.repositories ?? []), ...(item.scope.organizations ?? []).map((name) => name + "/*")].join("、") || (item.scope.installation_ids?.length ? "指定 App 安装范围" : "未分配")}</td>
            <td><WorkspaceBadge tone={item.enabled ? "success" : "neutral"}>{item.enabled ? "启用" : "停用"}</WorkspaceBadge></td><td><span className="ws-chip">v{item.revision}</span></td>
            <td><button type="button" aria-label={"编辑成员 " + item.username} disabled={busy} onClick={() => {setMember(item); setMessage("");}}>编辑</button></td>
          </tr>)}</tbody>
        </table>}
        {tab === "audits" && <table>
          <thead><tr><th>时间</th><th>操作人</th><th>操作</th><th>对象</th><th>变更内容</th></tr></thead>
          <tbody>{audits.data?.items.map((item) => <tr key={item.id}>
            <td>{formatDate(item.occurred_at)}</td><td>{String(item.payload.actor ?? "")}</td>
            <td>{item.event_type === "team.member.updated" ? "更新成员" : item.event_type === "team.repository.connection_checked" ? "检查仓库接入" : "更新仓库策略"}</td>
            <td>{String(item.payload.target ?? "")}</td>
            <td><DetailDialog><summary>查看详情</summary><pre>{JSON.stringify(item.payload, null, 2)}</pre></DetailDialog></td>
          </tr>)}</tbody>
        </table>}
      </div>
      {!page.loading && page.data?.items.length === 0 && <WorkspaceEmpty title={tab === "repositories" ? "尚未配置仓库" : tab === "members" ? "尚未添加成员" : "暂无变更记录"} description={tab === "repositories" ? "为已授权的仓库添加策略，统一管理审查范围与审批。" : tab === "members" ? "新增成员后，为其分配角色与可见仓库。" : "成员与仓库策略的变更会显示在这里。"} />}
      <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={busy || page.loading} onPrevious={page.previous} onNext={page.next} />
    </section>}
    {member && <MemberEditor key={member === "new" ? "new-member" : member.username + ":" + member.revision} member={member} initialAdmin={member !== "new" && member.username.toLowerCase() === administrator.toLowerCase()} busy={busy} onCancel={() => setMember(null)} onSave={(username, body) => void save(() => api.saveTeamMember(username, body))} />}
    {repository && <RepositoryEditor key={repository === "new" ? "new-repository" : repository.id + ":" + repository.revision} repository={repository} initialBudget={initialSection === "budget"} busy={busy} onCancel={() => setRepository(null)} onSave={(body, id) => void save(() => api.saveTeamRepository(body, id))} />}
  </main>;
}

function MemberEditor({ member, initialAdmin, busy, onCancel, onSave }: {
  member: TeamMember | "new"; initialAdmin: boolean; busy: boolean; onCancel: () => void;
  onSave: (username: string, body: TeamMemberWrite) => void;
}) {
  const current = member === "new" ? null : member;
  const [username, setUsername] = useState(current?.username ?? "");
  const [role, setRole] = useState<AccessRole>(current?.role ?? "viewer");
  const [unrestricted, setUnrestricted] = useState(current?.scope.unrestricted ?? false);
  const [enabled, setEnabled] = useState(current?.enabled ?? true);
  const [password, setPassword] = useState("");
  const [repositories, setRepositories] = useState(current?.scope.repositories?.join("\n") ?? "");
  const [organizations, setOrganizations] = useState(current?.scope.organizations?.join("\n") ?? "");
  const [installations, setInstallations] = useState(current?.scope.installation_ids?.join(",") ?? "");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSave(username.trim(), {
      expected_revision: current?.revision ?? 0, role, enabled,
      scope: unrestricted
        ? {unrestricted: true, repositories: [], organizations: [], installation_ids: []}
        : {unrestricted: false, repositories: lines(repositories), organizations: lines(organizations), installation_ids: lines(installations).map(Number)},
      ...(password ? {password} : {}),
    });
  };
  return <section className="team-card team-editor ws-editor" aria-label="成员编辑">
    <div className="ws-editor-heading"><div><h2>{current ? "编辑成员 · " + current.username : "新增成员"}</h2><p>设置账号身份与可见资源，修改后成员需要重新登录。</p></div>{current && <WorkspaceBadge>版本 {current.revision}</WorkspaceBadge>}</div>
    {initialAdmin && <p>这是初始管理员。部署配置只用于首次导入，之后在这里修改密码；角色、启用状态与全部仓库权限固定保留。</p>}
    <form onSubmit={submit}>
      <fieldset disabled={busy}>
        <WorkspaceSection title="账号信息" description="分配角色，维护账号状态与登录密码。"><div className="team-form-grid">
          <label>用户名<input value={username} disabled={Boolean(current)} required maxLength={100} pattern={"[A-Za-z0-9_.@\\-]+"} autoComplete="off" onChange={(event) => setUsername(event.target.value)} /></label>
          <label>角色<select disabled={initialAdmin} value={role} onChange={(event) => {setRole(event.target.value as AccessRole); setUnrestricted(event.target.value === "administrator");}}>{Object.entries(roleLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label>{current ? "新密码（留空保留）" : "初始密码"}<input type="password" value={password} required={!current} minLength={12} maxLength={128} autoComplete="new-password" onChange={(event) => setPassword(event.target.value)} /></label>
          <label className="team-check"><input type="checkbox" disabled={initialAdmin} checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />启用账号</label>
        </div></WorkspaceSection>
        <WorkspaceSection title="资源范围" description="成员只能查看已分配范围内的仓库与任务。">
        {role === "administrator" && <label className="team-check"><input type="checkbox" disabled={initialAdmin} checked={unrestricted} onChange={(event) => setUnrestricted(event.target.checked)} />可查看全部仓库（管理员具有成员和配置管理权限）</label>}
        {!unrestricted && <>
          <label>可见仓库（每行一个）<textarea value={repositories} rows={3} placeholder="owner/repository" onChange={(event) => setRepositories(event.target.value)} /></label>
          <DetailDialog className="team-scope-options"><summary>组织与 App 安装范围（可选）</summary>
            <label>组织名称（该组织的全部仓库可见）<textarea value={organizations} rows={2} onChange={(event) => setOrganizations(event.target.value)} /></label>
            <label>限制 App 安装编号（逗号分隔）<input value={installations} pattern="[0-9, ]*" onChange={(event) => setInstallations(event.target.value)} /></label>
            <p className="team-hint">安装范围与仓库范围同时生效；全部留空时无法查看任何任务。</p>
          </DetailDialog>
        </>}
        </WorkspaceSection>
        <div className="ws-form-actions is-sticky"><button type="submit" className="team-primary">{busy ? "正在保存…" : "保存成员"}</button><button type="button" onClick={onCancel}>取消编辑</button></div>
      </fieldset>
    </form>
  </section>;
}

function RepositoryEditor({ repository, initialBudget = false, busy, onCancel, onSave }: {
  repository: TeamRepository | "new"; initialBudget?: boolean; busy: boolean; onCancel: () => void;
  onSave: (body: TeamRepositoryWrite, id?: string) => void;
}) {
  const current = repository === "new" ? null : repository;
  const [budgetOpen, setBudgetOpen] = useState(initialBudget);
  const [name, setName] = useState(current?.repository ?? "");
  const [enabled, setEnabled] = useState(current?.policy.enabled ?? true);
  const [branches, setBranches] = useState(current?.policy.target_branches?.join("\n") ?? "");
  const [approver, setApprover] = useState(current?.policy.approver ?? "");
  const [limit, setLimit] = useState(current?.policy.max_model_requests?.toString() ?? "");
  const [monthlyBudget, setMonthlyBudget] = useState(current?.policy.monthly_budget_microusd == null ? "" : String(current.policy.monthly_budget_microusd / 1_000_000));
  const [warningPercent, setWarningPercent] = useState(String(current?.policy.budget_warning_percent ?? 80));
  const [concurrency, setConcurrency] = useState(current?.policy.max_concurrent_reviews?.toString() ?? "");
  const [approvalHours, setApprovalHours] = useState(String(current?.policy.approval_timeout_hours ?? 24));
  const [useProfile, setUseProfile] = useState(Boolean(current?.policy.review_profile_id));
  const [blockedPaths, setBlockedPaths] = useState(current?.policy.egress?.blocked_paths?.join("\n") ?? ".env\n.env.*\n**/.env\n**/.env.*\n*.pem\n**/*.pem\n*.key\n**/*.key");
  const [allowedHosts, setAllowedHosts] = useState(current?.policy.egress?.allowed_hosts?.join("\n") ?? "");
  const [blockSecrets, setBlockSecrets] = useState(current?.policy.egress?.block_secrets ?? true);
  const [incremental, setIncremental] = useState(current?.policy.incremental_review ?? false);
  const initialSources = current?.policy.knowledge_sources;
  const [knowledgeMode, setKnowledgeMode] = useState(initialSources == null ? "inherit" : initialSources.length ? "selected" : "none");
  const [sources, setSources] = useState(initialSources?.join("\n") ?? "");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSave({
      repository: name.trim(), expected_revision: current?.revision ?? 0,
      policy: {
        enabled, target_branches: lines(branches), approver: approver.trim() || null,
        max_model_requests: limit ? Number(limit) : null,
        monthly_budget_microusd: monthlyBudget ? Math.round(Number(monthlyBudget) * 1_000_000) : null,
        budget_warning_percent: Number(warningPercent), max_concurrent_reviews: concurrency ? Number(concurrency) : null,
        approval_timeout_hours: Number(approvalHours), review_profile_id: useProfile ? current?.policy.review_profile_id ?? null : null,
        knowledge_sources: knowledgeMode === "inherit" ? null : knowledgeMode === "none" ? [] : lines(sources),
        egress: { blocked_paths: lines(blockedPaths), allowed_hosts: lines(allowedHosts), block_secrets: blockSecrets },
        incremental_review: incremental,
      },
    }, current?.id);
  };
  return <section className="team-card team-editor ws-editor" aria-label="仓库策略编辑">
    <div className="ws-editor-heading"><div><h2>{current ? "项目设置 · " + current.repository : "接入仓库"}</h2><p>先确认仓库和目标分支，其他选项可沿用默认值。</p></div>{current && <WorkspaceBadge>第 {current.revision} 版设置</WorkspaceBadge>}</div>
    <form onSubmit={submit}><fieldset disabled={busy}>
      <WorkspaceSection title="审查范围" description="选择接入仓库、目标分支和团队知识。">
        <div className="team-form-grid"><label>仓库名称<input value={name} required disabled={Boolean(current)} maxLength={255} pattern={"[A-Za-z0-9_.\\-]+/[A-Za-z0-9_.\\-]+"} placeholder="owner/repository" onChange={event => setName(event.target.value)} /></label>
          <label className="team-check"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} />接收新审查任务</label></div>
        <label>目标分支（每行一个，留空审查全部分支）<textarea value={branches} rows={2} placeholder={"main\nrelease/*"} onChange={event => setBranches(event.target.value)} /></label>
        <label>知识规则<select value={knowledgeMode} onChange={event => setKnowledgeMode(event.target.value)}><option value="inherit">继承已启用知识库</option><option value="selected">仅使用指定文档</option><option value="none">不使用知识库</option></select></label>
        {knowledgeMode === "selected" && <label>知识文档来源（从知识库复制，每行一个）<textarea value={sources} rows={3} required placeholder="security.md" onChange={event => setSources(event.target.value)} /></label>}
        <p className="team-hint">仓库内的 AGENTS.md 始终生效。<a href="#knowledge">管理知识库</a></p>
      </WorkspaceSection>
      <WorkspaceSection title="结果由谁批准" description="指定审核负责人，或按成员角色分配审批权限。">
        <div className="team-form-grid"><label>审批负责人（用户名，可留空）<input value={approver} maxLength={100} onChange={event => setApprover(event.target.value)} /><small>留空时按角色审批。</small></label>
          <label>审批时限（小时）<input required type="number" min={1} max={720} value={approvalHours} onChange={event => setApprovalHours(event.target.value)} /></label></div>
        {current?.policy.review_profile_id && <label className="team-check"><input type="checkbox" checked={useProfile} onChange={event => setUseProfile(event.target.checked)} />继续固定已启用的审查方案</label>}
        <p className="team-hint">设置应用于新任务。需要固定模型和规则时，可使用<a href="#platform?tab=profiles">配置版本</a>。</p>
      </WorkspaceSection>
      <DetailDialog className="ws-disclosure" open={budgetOpen} onToggle={event => setBudgetOpen(event.currentTarget.open)}><summary>预算与执行上限<small>{name}</small></summary><div className="ws-disclosure-body">
      <WorkspaceSection title="费用与执行" description="留空表示沿用平台限制；请求数包含实际审查和重试。">
        <div className="team-form-grid">
          <label>月度预算（美元，可留空）<input type="number" min={0.000001} max={1000000} step={0.000001} value={monthlyBudget} placeholder="不限制" onChange={event => setMonthlyBudget(event.target.value)} /><small>按 UTC 月份估算，启用前需配置模型价格。</small></label>
          <label>预算提醒比例（%）<input required type="number" min={1} max={100} value={warningPercent} onChange={event => setWarningPercent(event.target.value)} /></label>
          <label>单次审查最多模型请求数<input type="number" min={1} max={10000} step={1} value={limit} placeholder="不限制" onChange={event => setLimit(event.target.value)} /><small>包含各 Agent 与重试，不含向量、精排。</small></label>
          <label>仓库同时执行的任务上限<input type="number" min={1} max={100} value={concurrency} placeholder="不限制" onChange={event => setConcurrency(event.target.value)} /></label>
        </div>
      </WorkspaceSection>
      <button className="ws-primary" type="submit" disabled={busy}>保存预算与项目设置</button>
      </div></DetailDialog>
      <DetailDialog className="ws-disclosure"><summary>数据外发与增量复用<small>高级设置</small></summary><div className="ws-disclosure-body">
        <p className="team-hint">外发限制覆盖审查、向量与精排；命中限制时拒绝请求并保留原代码。</p>
        <div className="team-form-grid"><label>禁止外发的文件（每行一项，支持 * 通配符）<textarea rows={5} value={blockedPaths} onChange={event => setBlockedPaths(event.target.value)} /></label>
          <label>允许的模型供应商域名（每行一个，留空使用已配置连接）<textarea rows={5} value={allowedHosts} onChange={event => setAllowedHosts(event.target.value)} placeholder="api.openai.com" /></label></div>
        <label className="team-check"><input type="checkbox" checked={blockSecrets} onChange={event => setBlockSecrets(event.target.checked)} />检测到凭据形状时阻止整次外发</label>
        <label className="team-check"><input type="checkbox" checked={incremental} onChange={event => setIncremental(event.target.checked)} />启用跨提交的精确输入复用（需绑定审查方案）</label>
        <p className="team-hint">仅复用相同方案和输入，关联代码变化后重新审查。</p>
      </div></DetailDialog>
      <div className="ws-form-actions is-sticky"><button type="submit" className="team-primary">{busy ? "正在保存…" : "保存项目设置"}</button><button type="button" onClick={onCancel}>取消编辑</button><span className="ws-hint">保存前会检查是否有其他人的修改</span></div>
    </fieldset></form>
  </section>;
}
