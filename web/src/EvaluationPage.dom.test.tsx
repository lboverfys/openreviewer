// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { clearReadCache } from "./api";
import EvaluationPage from "./EvaluationPage";
import EvaluationCasePanel from "./EvaluationCasePanel";
import EvaluationReportPanel from "./EvaluationReportPanel";
import type { AuthUser, EvaluationDataset } from "./types";

const now="2026-09-12T10:00:00Z";
const user={username:"alice",role:"adjudicator",permissions:["reviews:view","findings:adjudicate"],expires_at:now} as AuthUser;
const dataset={id:"set-1",name:"权限评测",repository:"example/repo",review_mode:"single",case_count:1,revision:2,created_by:"alice",created_at:now,updated_at:now,archived_at:null} as EvaluationDataset;
const source={review_run_id:"run-1",repository:"example/repo",pull_request_number:1,head_sha:"a".repeat(40),title:"权限修复",finding_count:1,model:"test-model",completed_at:now,created_at:now};
let requests: Array<{path:string;method:string;body:unknown;headers:Headers}>;
beforeEach(()=>{
  requests=[];clearReadCache();window.location.hash="";
  vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
    const path=String(input);requests.push({path,method:init?.method??"GET",body:init?.body?JSON.parse(String(init.body)):null,headers:new Headers(init?.headers)});
    if(init?.method==="POST")return new Response(JSON.stringify(dataset));
    if(path.includes("/sources"))return new Response(JSON.stringify({items:[source]}));
    return new Response(JSON.stringify({items:[]}));
  }));
});
afterEach(()=>{cleanup();clearReadCache();vi.unstubAllGlobals();window.location.hash="";});

it("从任务详情预选运行并以稳定请求标识创建评测集",async()=>{
  render(<EvaluationPage user={user} reviewRunId="run-1" onSignedOut={vi.fn()}/>);
  fireEvent.change(screen.getByLabelText("评测名称（可不填）"),{target:{value:"权限评测"}});
  fireEvent.click(screen.getByRole("button",{name:"开始核对问题"}));
  await waitFor(()=>expect(window.location.hash).toBe("#evaluations/set-1"));
  const saved=requests.find(item=>item.method==="POST")!;
  expect(saved.body).toMatchObject({name:"权限评测",review_run_ids:["run-1"],variant:"baseline",split:"validation"});
  expect(saved.headers.get("Idempotency-Key")).toBeTruthy();
});

it("只读成员可以查看评测集但没有收录入口",async()=>{
  render(<EvaluationPage user={{...user,role:"viewer",permissions:["reviews:view"]}} onSignedOut={vi.fn()}/>);
  await screen.findByText("尚无评测记录");
  expect(screen.queryByRole("button",{name:"开始新评测"})).not.toBeInTheDocument();
});

it("创建双人验收集时明确选择不可变的核对方式",async()=>{
  render(<EvaluationPage user={user} reviewRunId="run-1" onSignedOut={vi.fn()}/>);
  fireEvent.change(screen.getByLabelText("核对方式"),{target:{value:"dual"}});
  expect(screen.getByText(/首次提交前隐藏对方判断/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"开始核对问题"}));
  await waitFor(()=>expect(requests.some(item=>item.method==="POST")).toBe(true));
  expect(requests.find(item=>item.method==="POST")?.body).toMatchObject({review_mode:"dual"});
});

it("已收起记录单独查询，恢复后打开原评测且不重新发起模型",async()=>{
  vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
    const path=String(input); requests.push({path,method:init?.method??"GET",body:init?.body?JSON.parse(String(init.body)):null,headers:new Headers(init?.headers)});
    if(init?.method==="POST") return Response.json(dataset);
    return Response.json({items:path.includes("archived_only=true")?[{...dataset,archived_at:now}]:[]});
  }));
  render(<EvaluationPage user={user} onSignedOut={vi.fn()}/>);
  await screen.findByText("尚无评测记录");
  fireEvent.click(screen.getByRole("button",{name:"已收起记录"}));
  fireEvent.click(await screen.findByRole("button",{name:"恢复评测"}));
  await waitFor(()=>expect(window.location.hash).toBe("#evaluations/set-1"));
  expect(requests.filter(item=>item.method==="POST")).toHaveLength(1);
  expect(requests.find(item=>item.method==="POST")?.body).toEqual({expected_revision:2,archived:false});
});

it("未复核指标和未知费用不显示为零分或满分",async()=>{
  const score={sample_count:0,finding_count:0,valid_count:0,false_positive_count:0,duplicate_count:0,
    out_of_scope_count:0,known_issue_count:0,precision:null,precision_ci95:null,recall:null,recall_ci95:null,
    location_accuracy:null,duplicate_rate:null,reference_expected_count:0,reference_true_positive_count:0,
    reference_false_negative_count:0,reference_unexpected_valid_count:0,mean_turnaround_ms:1000,
    mean_model_duration_ms:600,mean_estimated_cost_usd:null,input_tokens:100,output_tokens:20,configuration_count:1};
  vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL)=>{
    requests.push({path:String(input),method:"GET",body:null,headers:new Headers()});
    return new Response(JSON.stringify({dataset_id:"set-1",dataset_name:"权限评测",repository:"example/repo",
      split:"validation",case_count:1,performance_pairs:1,quality_pairs:0,reference_pairs:0,priced_pairs:0,
      missing_baseline:0,missing_candidate:0,pending_pairs:1,disputed_pairs:0,normal_count:1,
      known_defect_count:0,cross_file_count:0,notices:["两份审查尚未完成核对"],baseline:score,candidate:score}));
  }));
  render(<EvaluationReportPanel datasetId="set-1" onError={vi.fn()}/>);
  fireEvent.click(await screen.findByRole("button", {name:/数据完整性与统计口径/}));
  await screen.findByText("两份审查尚未完成核对");
  expect(screen.getAllByText("未知")).toHaveLength(4);
  expect(screen.queryByText("100.0%")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("报告样本集"),{target:{value:"tuning"}});
  await waitFor(()=>expect(requests.some(item=>item.path.endsWith("split=tuning"))).toBe(true));
});

it("问题复核发生版本冲突时保留输入",async()=>{
  const observation={id:"obs-1",variant:"baseline",source_run_id:"run-1",snapshot_sha256:"c".repeat(64),
    model_label:"test-model",provenance_complete:false,finding_count:1,assessment_status:"pending",
    revision:1,captured_by:"alice",created_at:now,updated_at:now};
  const sample={id:"case-1",dataset_id:"set-1",repository:"example/repo",pull_request_number:1,head_sha:"a".repeat(40),
    title:"权限修复",split:"validation",kind:"known_defect",reference_status:"pending",reference_count:null,
    revision:1,created_at:now,updated_at:now,baseline:observation,candidate:null,reference_defects:null,reference_reviews:[]};
  const finding={id:"finding-1",fingerprint:"f".repeat(64),title:"缺少归属检查",severity:"high",category:"authorization",
    file:"service.py",start_line:1,end_line:1,evidence:"缺少条件",impact:"越权",suggestion:"添加条件",confidence:0.9,
    location_status:"verified",evidence_status:"unverified",context_references:[]};
  vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
    if(init?.method==="PUT")return new Response(JSON.stringify({detail:"记录已更新，请刷新后重试"}),{status:409});
    const path=String(input);
    if(path.includes("/findings"))return new Response(JSON.stringify({items:[{finding,reviews:[]}]}));
    if(path.includes("/observations/"))return new Response(JSON.stringify({observation,ballots:[],source:{limitations:[],models:[],rule_versions:[],retrieval:[],change_count:1}}));
    return new Response(JSON.stringify(sample));
  }));
  const error=vi.fn();
  render(<EvaluationCasePanel dataset={dataset} caseId="case-1" user={user} canEdit onError={error} onChanged={vi.fn()}/>);
  fireEvent.click(await screen.findByRole("button", {name:/补充说明、位置与已知缺陷/}));
  fireEvent.change(await screen.findByLabelText("我的结论"),{target:{value:"valid"}});
  fireEvent.change(screen.getByLabelText("核对说明"),{target:{value:"输入需要保留"}});
  fireEvent.click(screen.getByRole("button",{name:"保存补充判断"}));
  await waitFor(()=>expect(error).toHaveBeenCalled());
  expect(screen.getByLabelText("核对说明")).toHaveValue("输入需要保留");
});

it.each([0,1])("双人验收在 %i 条问题且尚无复核时仍要求两位成员独立提交",async(findingCount)=>{
  const observation={id:"obs-dual",variant:"baseline",source_run_id:"run-1",snapshot_sha256:"c".repeat(64),
    model_label:"test-model",finding_count:findingCount,assessment_status:"pending",revision:1};
  const sample={id:"case-dual",dataset_id:"set-1",repository:"example/repo",pull_request_number:1,head_sha:"a".repeat(40),
    title:"权限修复",split:"validation",kind:"normal",reference_status:"pending",revision:1,
    baseline:observation,candidate:null,reference_defects:null,reference_reviews:[]};
  vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
    const path=String(input);requests.push({path,method:init?.method??"GET",body:null,headers:new Headers(init?.headers)});
    if(path.includes("/findings"))return Response.json({items:findingCount?[{finding:{id:"f1",title:"缺少归属检查",severity:"high",category:"authorization"},reviews:[]}]:[]});
    if(path.includes("/observations/"))return Response.json({observation,ballots:[],source:{limitations:[],models:[],rule_versions:[],retrieval:[]}});
    return Response.json(sample);
  }));
  render(<EvaluationCasePanel dataset={{...dataset,review_mode:"dual"}} caseId="case-dual" user={user} canEdit onError={vi.fn()} onChanged={vi.fn()}/>);
  await screen.findByRole("button",{name:"提交我的独立复核并查看统计"});
  expect(screen.getByText("请独立核对全部问题后提交；双人验收需要两位不同成员的提交。")).toBeInTheDocument();
  expect(screen.getByText("提交前必须核对全部问题；无法确认时可选择暂不确定，它们不会算作有效问题。")).toBeInTheDocument();
  expect(screen.queryByText("由当前账号核对即可完成，无需第二个账号。")).not.toBeInTheDocument();
  expect(screen.queryByRole("button",{name:"完成核对并查看统计"})).not.toBeInTheDocument();
  if(!findingCount) expect(await screen.findByText("两位成员仍需分别提交独立复核；未提供可靠的已知缺陷清单时，不计算找回率。")).toBeInTheDocument();
  expect(requests.every(item=>item.method==="GET")).toBe(true);
});
