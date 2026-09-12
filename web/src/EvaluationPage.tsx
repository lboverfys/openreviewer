import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import EvaluationCasePanel, { assessmentLabels } from "./EvaluationCasePanel";
import EvaluationImportPanel from "./EvaluationImportPanel";
import EvaluationReportPanel from "./EvaluationReportPanel";
import Pagination from "./Pagination";
import { hasPermission } from "./rbac";
import type { AuthUser, EvaluationDataset, EvaluationSplit } from "./types";
import { useCursorPage } from "./useCursorPage";
import { formatDate, shortSha } from "./utils";
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
  return <main className="evaluation-page">
    <header className="evaluation-heading"><div><p className="evaluation-eyebrow">REVIEW EVALUATION</p><h1>真实 PR 评测</h1><p>收录审查快照，由两位成员复核，用同一提交的结果比较效果与成本。</p></div>
      {datasetId && <a href="#evaluations" className="evaluation-back">返回评测集</a>}</header>
    {error && <div role="alert" className="evaluation-error">{error}<button type="button" onClick={() => setError("")}>收起</button></div>}
    {datasetId ? <DatasetWorkspace key={datasetId} datasetId={datasetId} caseId={caseId} user={user} canEdit={canEdit} reviewRunId={reviewRunId} onError={onError} />
      : <DatasetList key={reviewRunId ?? "list"} canEdit={canEdit} reviewRunId={reviewRunId} onError={onError} />}
  </main>;
}

function DatasetList({ canEdit,reviewRunId,onError }: {canEdit:boolean;reviewRunId?:string;onError:(error:unknown)=>void}) {
  const [archived,setArchived]=useState(false);
  const [creating,setCreating]=useState(Boolean(reviewRunId));
  const load=useCallback((cursor?:string,signal?:AbortSignal,force?:boolean)=>api.evaluationDatasets(archived,cursor,signal,force),[archived]);
  const page=useCursorPage({cacheKey:"evaluation-datasets:"+archived,load,onError});
  const open=(id:string)=>{window.location.hash="evaluations/"+encodeURIComponent(id);};
  return <>
    <section className="evaluation-card">
      <div className="evaluation-toolbar"><h2>评测集</h2><label className="evaluation-check"><input type="checkbox" checked={archived} onChange={event=>setArchived(event.target.checked)}/>包含已归档</label>
        <button type="button" disabled={page.loading} onClick={()=>void page.refresh()}>刷新列表</button>
        {canEdit && <button type="button" className="evaluation-primary" onClick={()=>setCreating(value=>!value)}>创建评测集</button>}</div>
      <div className="evaluation-table-wrap"><table><thead><tr><th>名称 / 仓库</th><th>PR 样本数</th><th>创建人</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>{page.data?.items.map(item=><tr key={item.id}><td><strong>{item.name}</strong><small>{item.repository}</small></td><td>{item.case_count}</td><td>{item.created_by}</td><td>{item.archived_at?"已归档":"进行中"}</td>
          <td><button type="button" onClick={()=>open(item.id)}>打开评测集</button></td></tr>)}</tbody></table></div>
      {!page.loading && page.data?.items.length===0 && <div className="evaluation-empty"><strong>尚无评测集</strong><p>先选择已完成的审查运行建立基线，再收录同一提交的候选结果。</p></div>}
      <Pagination page={page.page} count={page.data?.items.length??0} hasNext={Boolean(page.data?.next_cursor)} busy={page.loading} onPrevious={page.previous} onNext={page.next} label="评测集分页"/>
    </section>
    {creating && canEdit && <EvaluationImportPanel initialRunId={reviewRunId} onSaved={open} onCancel={()=>setCreating(false)} onError={onError}/>}
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
  const loadDataset=useCallback(async(signal?:AbortSignal)=>{
    try{const data=await api.evaluationDataset(datasetId,signal);if(!signal?.aborted)setDataset(data);}
    catch(error){if(!signal?.aborted)onError(error);}
  },[datasetId,onError]);
  useEffect(()=>{const controller=new AbortController();void loadDataset(controller.signal);return()=>controller.abort();},[loadDataset]);
  const loadCases=useCallback((cursor?:string,signal?:AbortSignal,force?:boolean)=>api.evaluationCases(datasetId,split,cursor,signal,force),[datasetId,split]);
  const loadAudits=useCallback((cursor?:string,signal?:AbortSignal,force?:boolean)=>api.evaluationAudits(datasetId,cursor,signal,force),[datasetId]);
  const cases=useCursorPage({cacheKey:"evaluation-cases:"+datasetId+":"+(split??"all"),load:loadCases,onError,enabled:tab==="samples"});
  const audits=useCursorPage({cacheKey:"evaluation-audits:"+datasetId,load:loadAudits,onError,enabled:tab==="audits"});
  const changed=useCallback(()=>{void loadDataset();void cases.refresh();},[loadDataset,cases.refresh]);
  async function archive(){
    if(!dataset)return;setBusy(true);
    try{setDataset(await api.archiveEvaluationDataset(datasetId,dataset.revision,!dataset.archived_at));}
    catch(error){onError(error);}finally{setBusy(false);}
  }
  if(!dataset)return <section className="evaluation-card" role="status">正在加载评测集…</section>;
  return <>
    <section className="evaluation-card"><div className="evaluation-toolbar"><div><h2>{dataset.name}</h2><p>{dataset.repository} · {dataset.case_count} 个 PR · {dataset.archived_at?"已归档":"进行中"}</p></div>
      {canEdit && <button type="button" disabled={busy} onClick={()=>void archive()}>{dataset.archived_at?"恢复评测集":"归档评测集"}</button>}</div>
      <p className="evaluation-hint">每个 PR 固定提交与样本划分。归档保留快照和报告，恢复后可继续复核。</p>
      <nav className="evaluation-tabs" aria-label="评测集内容">{([["samples","样本管理"],["report","对比报告"],["audits","变更记录"]] as const).map(([key,label])=><button type="button" key={key} aria-pressed={tab===key} onClick={()=>setTab(key)}>{label}</button>)}</nav>
    </section>
    {tab==="report" && <EvaluationReportPanel datasetId={datasetId} onError={onError}/>}
    {tab==="samples" && <>
      <section className="evaluation-card"><div className="evaluation-toolbar"><h3>PR 样本</h3>
        <label>筛选样本集<select value={split??"all"} onChange={event=>setSplit(event.target.value==="all"?undefined:event.target.value as EvaluationSplit)}><option value="all">全部</option><option value="tuning">调参集</option><option value="validation">验收集</option></select></label>
        <button type="button" disabled={cases.loading} onClick={()=>void cases.refresh()}>刷新样本列表</button>
        {canEdit && !dataset.archived_at && <button type="button" className="evaluation-primary" onClick={()=>setImporting(value=>!value)}>收录审查运行</button>}</div>
        <div className="evaluation-table-wrap"><table><thead><tr><th>PR / 提交</th><th>样本集</th><th>参考标签</th><th>基线</th><th>候选</th><th>操作</th></tr></thead>
          <tbody>{cases.data?.items.map(item=><tr key={item.id}><td><strong>PR #{item.pull_request_number} · {item.title}</strong><small>{shortSha(item.head_sha)}</small></td><td>{item.split==="validation"?"验收":"调参"}</td>
            <td>{item.reference_count==null?"未标注":item.reference_count+" 条"}<small>{item.reference_status==="confirmed"?"两人已确认":item.reference_status==="disputed"?"存在分歧":"待确认"}</small></td>
            <td>{item.baseline?assessmentLabels[item.baseline.assessment_status]:"未收录"}</td><td>{item.candidate?assessmentLabels[item.candidate.assessment_status]:"未收录"}</td>
            <td><button type="button" onClick={()=>{window.location.hash="evaluations/"+datasetId+"?case="+item.id;}}>查看与复核</button></td></tr>)}</tbody></table></div>
        {!cases.loading && cases.data?.items.length===0 && <p className="evaluation-empty">当前筛选下没有样本。</p>}
        <Pagination page={cases.page} count={cases.data?.items.length??0} hasNext={Boolean(cases.data?.next_cursor)} busy={cases.loading} onPrevious={cases.previous} onNext={cases.next} label="评测样本分页"/>
      </section>
      {importing && canEdit && !dataset.archived_at && <EvaluationImportPanel dataset={dataset} initialRunId={reviewRunId} onSaved={()=>{setImporting(false);changed();}} onCancel={()=>setImporting(false)} onError={onError}/>}
      {caseId && <EvaluationCasePanel key={caseId} dataset={dataset} caseId={caseId} user={user} canEdit={canEdit} onError={onError} onChanged={changed}/>}
    </>}
    {tab==="audits" && <section className="evaluation-card"><h3>变更记录</h3><div className="evaluation-table-wrap"><table>
      <thead><tr><th>时间</th><th>操作人</th><th>操作</th><th>详情</th></tr></thead><tbody>{audits.data?.items.map(item=><tr key={item.id}>
        <td>{formatDate(item.occurred_at)}</td><td>{String(item.payload.actor??"")}</td><td>{auditLabel(item.event_type)}</td><td><details><summary>查看详情</summary><pre>{JSON.stringify(item.payload,null,2)}</pre></details></td>
      </tr>)}</tbody></table></div><Pagination page={audits.page} count={audits.data?.items.length??0} hasNext={Boolean(audits.data?.next_cursor)} busy={audits.loading} onPrevious={audits.previous} onNext={audits.next} label="评测审计分页"/></section>}
  </>;
}

function auditLabel(value:string){
  return ({"evaluation.dataset_created":"创建评测集","evaluation.observations_imported":"收录观察结果","evaluation.reference_updated":"更新参考标签","evaluation.reference_reviewed":"复核参考标签","evaluation.finding_reviewed":"保存问题结论","evaluation.review_submitted":"提交复核","evaluation.observation_replaced":"更换观察结果","evaluation.dataset_archived":"归档评测集","evaluation.dataset_restored":"恢复评测集"} as Record<string,string>)[value]??value;
}
