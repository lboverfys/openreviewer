import { DetailDialog, Notice } from "./Feedback";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import EvaluationCasePanel, { assessmentLabels } from "./EvaluationCasePanel";
import EvaluationImportPanel from "./EvaluationImportPanel";
import EvaluationReportPanel from "./EvaluationReportPanel";
import EvaluationOverviewPanel from "./EvaluationOverviewPanel";
import Pagination from "./Pagination";
import { hasPermission } from "./rbac";
import type { AuthUser, EvaluationDataset, EvaluationSplit } from "./types";
import { useCursorPage } from "./useCursorPage";
import { formatDate, shortSha } from "./utils";
import { WorkspaceBack, WorkspaceBadge, WorkspaceEmpty, WorkspaceHeader } from "./Workspace";
import "./styles/evaluation.css";

export default function EvaluationPage({ user, datasetId, caseId, reviewRunId, onSignedOut }: {
  user: AuthUser; datasetId?: string; caseId?: string; reviewRunId?: string;
  onSignedOut: (message?: string) => void;
}) {
  const [error,setError]=useState("");
  const canEdit=hasPermission(user,"findings:adjudicate");
  const onError=useCallback((failure: unknown) => {
    if (failure instanceof ApiError && failure.status === 401) {onSignedOut("登录已失效，请重新登录"); return;}
    setError(failure instanceof Error ? failure.message : "评测数据暂时不可用");
  },[onSignedOut]);
  useEffect(() => setError(""),[datasetId,caseId]);
  return <main className="workspace-page evaluation-page">
    <WorkspaceHeader title="效果评测" icon="chart" description="审查负责找问题；评测负责确认找得对不对、有没有漏掉。这里不会自动重新调用模型。" actions={datasetId && !caseId && <a href="#evaluations" className="ws-button-link">返回评测记录</a>} />

    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    {datasetId ? <DatasetWorkspace key={datasetId} datasetId={datasetId} caseId={caseId} user={user} canEdit={canEdit} reviewRunId={reviewRunId} onError={onError} />
      : <DatasetList key={reviewRunId ?? "list"} canEdit={canEdit} reviewRunId={reviewRunId} onError={onError} />}
  </main>;
}

function DatasetList({ canEdit,reviewRunId,onError }: {canEdit:boolean;reviewRunId?:string;onError:(error:unknown)=>void}) {
  const [archived,setArchived]=useState(false);
  const [creating,setCreating]=useState(Boolean(reviewRunId));
  const [restoring,setRestoring]=useState<string|null>(null);
  const load=useCallback((cursor?:string,signal?:AbortSignal,force?:boolean)=>api.evaluationDatasets(archived,cursor,signal,force,archived),[archived]);
  const page=useCursorPage({cacheKey:"evaluation-datasets:"+(archived?"removed":false),load,onError});
  const open=async(id:string)=>{
    try {
      const cases=await api.evaluationCases(id);
      const only=cases.items.length===1 && !cases.next_cursor ? cases.items[0] : undefined;
      window.location.hash="evaluations/"+encodeURIComponent(id)+(only ? "?case="+encodeURIComponent(only.id) : "");
    } catch(error) {onError(error);}
  };
  async function restore(item: EvaluationDataset) {
    if (restoring) return; setRestoring(item.id);
    try { await api.archiveEvaluationDataset(item.id,item.revision,false); open(item.id); }
    catch(error) { onError(error); } finally { setRestoring(null); }
  }
  if (creating && canEdit) return <EvaluationImportPanel initialRunId={reviewRunId} onSaved={open} onCancel={()=>setCreating(false)} onError={onError}/>;
  return <>
    <section className="evaluation-card">
      <div className="evaluation-toolbar"><div><h2>评测记录</h2><p>从一份审查结果开始，按需要再添加结果进行比较。</p></div><div className="ws-actions">
        <button type="button" disabled={page.loading} onClick={()=>void page.refresh()}>刷新列表</button>
        {canEdit && <button type="button" className="evaluation-primary" onClick={()=>setCreating(true)}>开始新评测</button>}</div></div>
      <nav className="evaluation-tabs" aria-label="评测记录范围"><button type="button" aria-pressed={!archived} onClick={()=>setArchived(false)}>评测列表</button><button type="button" aria-pressed={archived} onClick={()=>setArchived(true)}>已收起记录</button></nav>
      <div className="evaluation-table-wrap"><table><thead><tr><th>名称 / 仓库</th><th>审查数量</th><th>创建人</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>{page.data?.items.map(item=><tr key={item.id}><td><strong>{item.name}</strong><small>{item.repository}</small></td><td className="ws-numeric">{item.case_count}</td><td>{item.created_by}</td><td><WorkspaceBadge tone={item.archived_at ? "neutral" : "accent"}>{item.archived_at?"已收起":"可继续核对"}</WorkspaceBadge></td>
          <td><button type="button" onClick={()=>open(item.id)}>查看评测</button>{item.archived_at && canEdit && <button type="button" disabled={restoring!==null} onClick={()=>void restore(item)}>{restoring===item.id?"恢复中…":"恢复评测"}</button>}</td></tr>)}</tbody></table></div>
      {!page.loading && page.data?.items.length===0 && <WorkspaceEmpty title={archived?"没有已收起的评测":"尚无评测记录"} description={archived?"收起的评测会保留在这里，可随时恢复。":"从已完成的审查中选择样本，先核对一组结果就能开始。"} />}
      <Pagination page={page.page} count={page.data?.items.length??0} hasNext={Boolean(page.data?.next_cursor)} busy={page.loading} onPrevious={page.previous} onNext={page.next} label="评测集分页"/>
    </section>
  </>;
}

function DatasetWorkspace({ datasetId,caseId,user,canEdit,reviewRunId,onError }: {
  datasetId:string;caseId?:string;user:AuthUser;canEdit:boolean;reviewRunId?:string;onError:(error:unknown)=>void;
}) {
  const [dataset,setDataset]=useState<EvaluationDataset|null>(null);
  const [tab,setTab]=useState<"samples"|"report"|"audits">("samples");
  const [split,setSplit]=useState<EvaluationSplit|undefined>();
  const [importing,setImporting]=useState(Boolean(reviewRunId));
  const [busy,setBusy]=useState(false);
  const [refreshVersion,setRefreshVersion]=useState(0);
  useEffect(() => { if (caseId) setTab("samples"); }, [caseId]);
  const loadDataset=useCallback(async(signal?:AbortSignal)=>{
    try{const data=await api.evaluationDataset(datasetId,signal);if(!signal?.aborted)setDataset(data);}
    catch(error){if(!signal?.aborted)onError(error);}
  },[datasetId,onError]);
  useEffect(()=>{const controller=new AbortController();void loadDataset(controller.signal);return()=>controller.abort();},[loadDataset]);
  const loadCases=useCallback((cursor?:string,signal?:AbortSignal,force?:boolean)=>api.evaluationCases(datasetId,split,cursor,signal,force),[datasetId,split]);
  const loadAudits=useCallback((cursor?:string,signal?:AbortSignal,force?:boolean)=>api.evaluationAudits(datasetId,cursor,signal,force),[datasetId]);
  const cases=useCursorPage({cacheKey:"evaluation-cases:"+datasetId+":"+(split??"all"),load:loadCases,onError,enabled:tab==="samples" && !caseId && !importing});
  const audits=useCursorPage({cacheKey:"evaluation-audits:"+datasetId,load:loadAudits,onError,enabled:tab==="audits"});
  const changed=useCallback(()=>{setRefreshVersion(value=>value+1);void loadDataset();if (!caseId && tab === "samples") void cases.refresh();},[loadDataset,cases.refresh,caseId,tab]);
  async function archive(){
    if(!dataset)return;setBusy(true);
    try{setDataset(await api.archiveEvaluationDataset(datasetId,dataset.revision,!dataset.archived_at));}
    catch(error){onError(error);}finally{setBusy(false);}
  }
  if(!dataset)return <section className="evaluation-card"><WorkspaceEmpty loading title="正在加载评测集…" /></section>;
  if (importing && canEdit && !dataset.archived_at) return <EvaluationImportPanel dataset={dataset} initialRunId={reviewRunId} onSaved={()=>{setImporting(false);changed();}} onCancel={()=>setImporting(false)} onError={onError}/>;
  if (caseId && tab === "samples") return <><WorkspaceBack onClick={() => { window.location.hash = "evaluations"; }}>返回评测记录</WorkspaceBack><EvaluationCasePanel key={caseId} dataset={dataset} caseId={caseId} user={user} canEdit={canEdit} onError={onError} onChanged={changed}/></>;
  return <>
    <section className="evaluation-card"><div className="evaluation-toolbar"><div><h2>{dataset.name}</h2><p>{dataset.repository} · {dataset.case_count} 个 PR · {dataset.archived_at?"已收起，可恢复":"可继续核对"}</p></div>
      {canEdit && <button type="button" disabled={busy} onClick={()=>void archive()}>{dataset.archived_at?"恢复评测":"收起这条评测"}</button>}</div>
      <p className="evaluation-hint">收起只会移出常用列表，复核结果仍保留。返回评测列表可从“已收起记录”找回。</p>
      <nav className="evaluation-tabs" aria-label="评测集内容">{([["samples","核对问题"],["report","评测结果"],["audits","变更记录"]] as const).map(([key,label])=><button type="button" key={key} aria-pressed={tab===key} onClick={()=>setTab(key)}>{label}</button>)}</nav>
    </section>
    {tab==="samples" && <EvaluationOverviewPanel datasetId={datasetId} version={refreshVersion} onError={onError}/>}
    {tab==="report" && <><EvaluationOverviewPanel datasetId={datasetId} version={refreshVersion} onError={onError}/><DetailDialog className="evaluation-card"><summary>比较两份结果（可选，仅同一提交）</summary><EvaluationReportPanel datasetId={datasetId} onError={onError}/></DetailDialog></>}
    {tab==="samples" && <>
      <section className="evaluation-card"><div className="evaluation-toolbar"><h3>待核对的审查</h3>
        <label>筛选样本集<select value={split??"all"} onChange={event=>setSplit(event.target.value==="all"?undefined:event.target.value as EvaluationSplit)}><option value="all">全部</option><option value="tuning">调参集</option><option value="validation">验收集</option></select></label>
        <button type="button" disabled={cases.loading} onClick={()=>void cases.refresh()}>刷新列表</button>
        {canEdit && !dataset.archived_at && <button type="button" className="evaluation-primary" onClick={()=>setImporting(value=>!value)}>添加审查结果</button>}</div>
        <div className="evaluation-table-wrap"><table><thead><tr><th>PR / 提交</th><th>样本用途</th><th>参考问题</th><th>第一份审查</th><th>第二份审查（可选）</th><th>操作</th></tr></thead>
          <tbody>{cases.data?.items.map(item=><tr key={item.id}><td><strong>PR #{item.pull_request_number} · {item.title}</strong><small>{shortSha(item.head_sha)}</small></td><td>{item.split==="validation"?"验收":"调参"}</td>
            <td>{item.reference_count==null?"未标注":item.reference_count+" 条"}<small>{item.reference_status==="confirmed"?"已确认":item.reference_status==="disputed"?"存在分歧":"待确认"}</small></td>
            <td>{item.baseline?assessmentLabels[item.baseline.assessment_status]:"未添加"}</td><td>{item.candidate?assessmentLabels[item.candidate.assessment_status]:"未添加"}</td>
            <td><button type="button" onClick={()=>{window.location.hash="evaluations/"+datasetId+"?case="+item.id;}}>核对问题</button></td></tr>)}</tbody></table></div>
        {!cases.loading && cases.data?.items.length===0 && <WorkspaceEmpty title="当前筛选下没有样本" description="可调整样本划分，或收录已完成的审查运行。" />}
        <Pagination page={cases.page} count={cases.data?.items.length??0} hasNext={Boolean(cases.data?.next_cursor)} busy={cases.loading} onPrevious={cases.previous} onNext={cases.next} label="评测样本分页"/>
      </section>
    </>}
    {tab==="audits" && <section className="evaluation-card"><h3>变更记录</h3><div className="evaluation-table-wrap"><table>
      <thead><tr><th>时间</th><th>操作人</th><th>操作</th><th>详情</th></tr></thead><tbody>{audits.data?.items.map(item=><tr key={item.id}>
        <td>{formatDate(item.occurred_at)}</td><td>{String(item.payload.actor??"")}</td><td>{auditLabel(item.event_type)}</td><td><DetailDialog><summary>查看详情</summary><pre>{JSON.stringify(item.payload,null,2)}</pre></DetailDialog></td>
      </tr>)}</tbody></table></div><Pagination page={audits.page} count={audits.data?.items.length??0} hasNext={Boolean(audits.data?.next_cursor)} busy={audits.loading} onPrevious={audits.previous} onNext={audits.next} label="评测审计分页"/></section>}
  </>;
}

function auditLabel(value:string){
  return ({"evaluation.dataset_created":"创建评测集","evaluation.observations_imported":"收录观察结果","evaluation.reference_updated":"更新参考标签","evaluation.reference_reviewed":"复核参考标签","evaluation.finding_reviewed":"保存问题结论","evaluation.review_submitted":"提交复核","evaluation.observation_replaced":"更换观察结果","evaluation.dataset_archived":"归档评测集","evaluation.dataset_restored":"恢复评测集"} as Record<string,string>)[value]??value;
}
