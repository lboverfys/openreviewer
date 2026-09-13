import { useCallback, useState, type FormEvent } from "react";

import { api, ApiError } from "./api";
import Pagination from "./Pagination";
import { roleLabels } from "./rbac";
import type { AccessRole, TeamMember, TeamMemberWrite, TeamRepository, TeamRepositoryWrite } from "./types";
import { useCursorPage } from "./useCursorPage";
import { formatDate } from "./utils";
import "./styles/team.css";

type Tab = "repositories" | "members" | "audits";
const lines = (value: string) => [...new Set(value.split(/[\n,，]+/).map((item) => item.trim()).filter(Boolean))];

export default function TeamPage({ onSignedOut }: { onSignedOut: (message?: string) => void }) {
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
    setTab(next); setMember(null); setRepository(null); setError(""); setMessage("");
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

  return <main className="team-page">
    <header className="team-heading">
      <div><p className="team-eyebrow">TEAM WORKSPACE</p><h1>团队管理</h1><p>分配成员权限，为每个仓库选择审查范围和审批负责人。</p></div>
      <button type="button" disabled={busy || page.loading} onClick={() => {setError(""); void page.refresh();}}>刷新列表</button>
    </header>
    <nav className="team-tabs" aria-label="团队管理分类">
      {([["repositories", "仓库策略"], ["members", "团队成员"], ["audits", "变更记录"]] as const).map(([key, label]) =>
        <button key={key} type="button" aria-pressed={tab === key} disabled={busy} onClick={() => changeTab(key)}>{label}</button>,
      )}
    </nav>
    {error && <p className="team-error" role="alert">{error}</p>}
    {message && <p className="team-success" role="status">{message}</p>}
    <section className="team-card">
      <div className="team-toolbar">
        <h2>{tab === "repositories" ? "已配置仓库" : tab === "members" ? "成员权限" : "最近变更"}</h2>
        {tab !== "audits" && <button type="button" className="team-primary" disabled={busy} onClick={() => {
          setError(""); setMessage("");
          if (tab === "members") setMember("new"); else setRepository("new");
        }}>{tab === "members" ? "新增成员" : "添加仓库策略"}</button>}
      </div>
      {tab === "repositories" && <p className="team-hint">仓库需已授权给 GitHub App。尚未配置策略的仓库沿用原有审查流程；保存后对新任务生效。</p>}
      {tab === "members" && <p className="team-hint">配置管理员 {administrator || "正在读取…"} 保留完整权限。修改成员角色、范围或密码后，该成员需要重新登录。</p>}
      <div className="team-table-wrap">
        {tab === "repositories" && <table>
          <thead><tr><th>仓库</th><th>新任务</th><th>目标分支</th><th>审批负责人</th><th>请求上限</th><th>版本</th><th>操作</th></tr></thead>
          <tbody>{repositories.data?.items.map((item) => <tr key={item.id}>
            <td>{item.repository}</td><td>{item.policy.enabled ? "接收" : "暂停"}</td>
            <td>{item.policy.target_branches?.join("、") || "全部"}</td><td>{item.policy.approver || "按角色审批"}</td>
            <td>{item.policy.max_model_requests == null ? "不限制" : item.policy.max_model_requests + " 次"}</td><td>v{item.revision}</td>
            <td><button type="button" aria-label={"编辑仓库 " + item.repository} disabled={busy} onClick={() => {setRepository(item); setMessage("");}}>编辑</button></td>
          </tr>)}</tbody>
        </table>}
        {tab === "members" && <table>
          <thead><tr><th>用户名</th><th>角色</th><th>可见资源</th><th>状态</th><th>版本</th><th>操作</th></tr></thead>
          <tbody>{members.data?.items.map((item) => <tr key={item.username}>
            <td>{item.username}</td><td>{roleLabels[item.role]}</td>
            <td>{item.scope.unrestricted ? "全部仓库" : [...(item.scope.repositories ?? []), ...(item.scope.organizations ?? []).map((name) => name + "/*")].join("、") || (item.scope.installation_ids?.length ? "指定 App 安装范围" : "未分配")}</td>
            <td>{item.enabled ? "启用" : "停用"}</td><td>v{item.revision}</td>
            <td><button type="button" aria-label={"编辑成员 " + item.username} disabled={busy} onClick={() => {setMember(item); setMessage("");}}>编辑</button></td>
          </tr>)}</tbody>
        </table>}
        {tab === "audits" && <table>
          <thead><tr><th>时间</th><th>操作人</th><th>操作</th><th>对象</th><th>变更内容</th></tr></thead>
          <tbody>{audits.data?.items.map((item) => <tr key={item.id}>
            <td>{formatDate(item.occurred_at)}</td><td>{String(item.payload.actor ?? "")}</td>
            <td>{item.event_type === "team.member.updated" ? "更新成员" : "更新仓库策略"}</td>
            <td>{String(item.payload.target ?? "")}</td>
            <td><details><summary>查看详情</summary><pre>{JSON.stringify(item.payload, null, 2)}</pre></details></td>
          </tr>)}</tbody>
        </table>}
      </div>
      {!page.loading && page.data?.items.length === 0 && <p className="team-empty">{tab === "repositories" ? "还没有仓库策略，可先为正在使用的仓库添加一份。" : tab === "members" ? "还没有普通成员，可新增成员并分配仓库权限。" : "还没有团队变更记录。"}</p>}
      <Pagination page={page.page} count={page.data?.items.length ?? 0} hasNext={Boolean(page.data?.next_cursor)} busy={busy || page.loading} onPrevious={page.previous} onNext={page.next} />
    </section>
    {member && <MemberEditor key={member === "new" ? "new-member" : member.username + ":" + member.revision} member={member} busy={busy} onCancel={() => setMember(null)} onSave={(username, body) => void save(() => api.saveTeamMember(username, body))} />}
    {repository && <RepositoryEditor key={repository === "new" ? "new-repository" : repository.id + ":" + repository.revision} repository={repository} busy={busy} onCancel={() => setRepository(null)} onSave={(body, id) => void save(() => api.saveTeamRepository(body, id))} />}
  </main>;
}

function MemberEditor({ member, busy, onCancel, onSave }: {
  member: TeamMember | "new"; busy: boolean; onCancel: () => void;
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
  return <section className="team-card team-editor" aria-label="成员编辑">
    <h2>{current ? "编辑成员 · " + current.username : "新增成员"}</h2>
    <form onSubmit={submit}>
      <fieldset disabled={busy}>
        <div className="team-form-grid">
          <label>用户名<input value={username} disabled={Boolean(current)} required maxLength={100} pattern={"[A-Za-z0-9_.@\\-]+"} autoComplete="off" onChange={(event) => setUsername(event.target.value)} /></label>
          <label>角色<select value={role} onChange={(event) => {setRole(event.target.value as AccessRole); setUnrestricted(event.target.value === "administrator");}}>{Object.entries(roleLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label>{current ? "新密码（留空保留）" : "初始密码"}<input type="password" value={password} required={!current} minLength={12} maxLength={128} autoComplete="new-password" onChange={(event) => setPassword(event.target.value)} /></label>
          <label className="team-check"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />启用账号</label>
        </div>
        {role === "administrator" && <label className="team-check"><input type="checkbox" checked={unrestricted} onChange={(event) => setUnrestricted(event.target.checked)} />可查看全部仓库（管理员具有成员和配置管理权限）</label>}
        {!unrestricted && <>
          <label>可见仓库（每行一个）<textarea value={repositories} rows={3} placeholder="owner/repository" onChange={(event) => setRepositories(event.target.value)} /></label>
          <details className="team-scope-options"><summary>组织与 App 安装范围（可选）</summary>
            <label>组织名称（该组织的全部仓库可见）<textarea value={organizations} rows={2} onChange={(event) => setOrganizations(event.target.value)} /></label>
            <label>限制 App 安装编号（逗号分隔）<input value={installations} pattern="[0-9, ]*" onChange={(event) => setInstallations(event.target.value)} /></label>
            <p className="team-hint">安装范围与仓库范围同时生效；全部留空时无法查看任何任务。</p>
          </details>
        </>}
        <div className="team-form-actions"><button type="submit" className="team-primary">{busy ? "正在保存…" : "保存成员"}</button><button type="button" onClick={onCancel}>取消编辑</button></div>
      </fieldset>
    </form>
  </section>;
}

function RepositoryEditor({ repository, busy, onCancel, onSave }: {
  repository: TeamRepository | "new"; busy: boolean; onCancel: () => void;
  onSave: (body: TeamRepositoryWrite, id?: string) => void;
}) {
  const current = repository === "new" ? null : repository;
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
  return <section className="team-card team-editor" aria-label="仓库策略编辑">
    <h2>{current ? "编辑策略 · " + current.repository : "添加仓库策略"}</h2>
    <form onSubmit={submit}>
      <fieldset disabled={busy}>
        <div className="team-form-grid">
          <label>仓库名称<input value={name} required disabled={Boolean(current)} maxLength={255} pattern={"[A-Za-z0-9_.\\-]+/[A-Za-z0-9_.\\-]+"} placeholder="owner/repository" onChange={(event) => setName(event.target.value)} /></label>
          <label className="team-check"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />接收新审查任务</label>
          <label>审批负责人（用户名，可留空）<input value={approver} maxLength={100} onChange={(event) => setApprover(event.target.value)} /><small>留空时按角色审批；管理员可以代为处理。</small></label>
          <label>单次审查最多模型请求数<input type="number" min={1} max={10000} step={1} value={limit} placeholder="不限制" onChange={(event) => setLimit(event.target.value)} /><small>含各 Agent 和重试；不包含向量、精排请求。</small></label>
          <label>月度预算（美元，可留空）<input type="number" min={0.000001} max={1000000} step={0.000001} value={monthlyBudget} onChange={event => setMonthlyBudget(event.target.value)} /><small>按 UTC 自然月和配置价格估算。预算不足时暂停新模型请求；启用前需配置模型价格。</small></label>
          <label>预算提醒比例（%）<input required type="number" min={1} max={100} value={warningPercent} onChange={event => setWarningPercent(event.target.value)} /></label>
          <label>仓库同时执行的任务上限<input type="number" min={1} max={100} value={concurrency} placeholder="不限制" onChange={event => setConcurrency(event.target.value)} /></label>
          <label>审批时限（小时）<input required type="number" min={1} max={720} value={approvalHours} onChange={event => setApprovalHours(event.target.value)} /></label>
          {current?.policy.review_profile_id && <label className="team-check"><input type="checkbox" checked={useProfile} onChange={event => setUseProfile(event.target.checked)} />继续固定已启用的审查方案</label>}
        </div>
        <label>目标分支（每行一个，留空审查全部分支）<textarea value={branches} rows={3} placeholder={"main\nrelease/*"} onChange={(event) => setBranches(event.target.value)} /></label>
        <label>禁止外发的文件（每行一项，支持 * 通配符）<textarea rows={4} value={blockedPaths} onChange={event => setBlockedPaths(event.target.value)} /></label>
        <label>允许的模型供应商域名（每行一个，留空使用已配置连接）<textarea rows={2} value={allowedHosts} onChange={event => setAllowedHosts(event.target.value)} placeholder="api.openai.com" /></label>
        <label className="team-check"><input type="checkbox" checked={blockSecrets} onChange={event => setBlockSecrets(event.target.checked)} />检测到凭据形状时阻止整次外发</label>
        <label className="team-check"><input type="checkbox" checked={incremental} onChange={event => setIncremental(event.target.checked)} />启用跨提交的精确输入复用（需绑定审查方案）</label>
        <p className="team-hint">外发限制覆盖审查、向量和精排；命中限制时保留原代码并拒绝请求。增量复用只适用于相同方案和输入，关联代码变化会失效，汇总仍重新执行。</p>
        <label>知识规则<select value={knowledgeMode} onChange={(event) => setKnowledgeMode(event.target.value)}><option value="inherit">继承已启用知识库</option><option value="selected">仅使用指定文档</option><option value="none">不使用知识库</option></select></label>
        {knowledgeMode === "selected" && <label>知识文档来源（从知识库复制，每行一个）<textarea value={sources} rows={3} required placeholder="security.md" onChange={(event) => setSources(event.target.value)} /></label>}
        <p className="team-hint">仓库内的 AGENTS.md 始终生效。审查范围和方案用于新任务；预算与仓库并发限制在后续请求、任务领取时生效。<a href="#platform?tab=profiles">管理审查方案</a></p>
        <div className="team-form-actions"><button type="submit" className="team-primary">{busy ? "正在保存…" : "保存仓库策略"}</button><button type="button" onClick={onCancel}>取消编辑</button></div>
      </fieldset>
    </form>
  </section>;
}
