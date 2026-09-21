import { Input } from "./components/ui/input";
import { Textarea } from "./components/ui/textarea";
import { Button } from "./components/ui/button";
import { useState, type FormEvent } from "react";
import { platformApi } from "./platform-api";
import type { ReviewProfile } from "./types";
import { WorkspaceSection } from "./Workspace";

const labels: Record<string, string> = {security:"安全",convention:"规范",logic:"逻辑",summary:"汇总"};

export default function ProfileCandidateEditor({base, onSaved, onCancel, onError}: {
  base: ReviewProfile; onSaved: () => void; onCancel: () => void; onError: (error: unknown) => void;
}) {
  const [name, setName] = useState(base.name + " · 候选");
  const [note, setNote] = useState("");
  const [roles, setRoles] = useState(base.role_instructions ?? {});
  const [supplementary, setSupplementary] = useState(base.supplementary_instructions ?? "");
  const [busy, setBusy] = useState(false);
  const changed = Object.fromEntries(Object.entries(roles).filter(([key,value]) => value !== base.role_instructions?.[key]));
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true);
    try {
      await platformApi.createProfile({name, note, repository:base.repository, base_profile_id:base.id,
        role_instructions:changed, supplementary_instructions:supplementary});
      onSaved();
    } catch (error) {onError(error);} finally {setBusy(false);}
  }
  return <section className="team-card ws-editor"><h2>基于 {base.name} 创建候选</h2>
    <p>保存为新方案。模型、参数、知识与检索沿用基础方案；输出格式、引用规则和发布权限继续由程序控制。</p>
    <form onSubmit={event => void save(event)}><fieldset disabled={busy}>
      <WorkspaceSection title="实验说明"><label>候选方案名称<Input required maxLength={120} value={name} onChange={event => setName(event.target.value)}/></label>
        <label>对照假设与变更原因<Textarea maxLength={1000} rows={3} value={note} onChange={event => setNote(event.target.value)} placeholder="例如：减少将既有问题判断为本次变更引入的问题；其他条件保持一致。"/></label></WorkspaceSection>
      <WorkspaceSection title="角色审查指令">{Object.entries(roles).map(([key,value]) => <label key={key}>{labels[key] ?? key}审查指令
        <Textarea required rows={4} maxLength={6000} value={value} onChange={event => setRoles(current => ({...current,[key]:event.target.value}))}/></label>)}</WorkspaceSection>
      <label>补充审查要求<Textarea rows={3} maxLength={2000} value={supplementary} onChange={event => setSupplementary(event.target.value)}/></label>
      <WorkspaceSection title="保存前查看差异">
        {Object.entries(changed).map(([key,value]) => <div key={key}><h4>{labels[key] ?? key}</h4><p>原指令</p><pre>{base.role_instructions?.[key]}</pre><p>候选指令</p><pre>{value}</pre></div>)}
        {supplementary !== (base.supplementary_instructions ?? "") && <div><h4>补充要求</h4><p>原内容</p><pre>{base.supplementary_instructions || "无"}</pre><p>候选内容</p><pre>{supplementary || "无"}</pre></div>}
        {!Object.keys(changed).length && supplementary === (base.supplementary_instructions ?? "") && <p>尚未修改 Prompt 内容。</p>}
      </WorkspaceSection>
      <div className="ws-form-actions"><Button variant="default" className="ws-primary" type="submit">{busy ? "正在保存…" : "保存候选方案"}</Button><Button variant="outline" type="button" onClick={onCancel}>取消</Button></div>
    </fieldset></form>
  </section>;
}
