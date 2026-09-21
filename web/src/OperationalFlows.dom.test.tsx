// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { clearReadCache } from "./api";
import DiagnosticsPanel from "./DiagnosticsPanel";
import EvaluationOverviewPanel from "./EvaluationOverviewPanel";
import EvaluationOutputPanel from "./EvaluationOutputPanel";
import PlatformPage from "./PlatformPage";
import type { AuthUser } from "./types";

const now = "2026-09-21T00:00:00Z";
const admin = {username:"reviewer",role:"administrator",permissions:["reviews:view","settings:manage"],expires_at:now} as AuthUser;
const repository = {id:"repo-1",repository:"lboverfys/NiuMa",revision:3,policy:{review_profile_id:"base"}};
const base = {id:"base",name:"基础方案",repository:repository.repository,note:"冻结配置",models:{logic:"fixture-model"},knowledge_versions:{},fingerprint:"a".repeat(64),ai_revision:1,prompt_version:"v1",prompt_content_sha256:"b".repeat(64),role_instructions:{logic:"核对变更逻辑"},supplementary_instructions:"",created_by:"reviewer",created_at:now};
afterEach(() => {cleanup();clearReadCache();vi.unstubAllGlobals();});

it("候选保存冲突保留输入，重试后回到历史且不启用仓库方案", async () => {
  const writes: Array<{path:string;body:Record<string,unknown>}> = [];
  let fail = true, created = false;
  vi.stubGlobal("fetch", vi.fn(async (input:RequestInfo|URL, init?:RequestInit) => {
    const path = String(input);
    if (init?.method === "POST") {
      writes.push({path,body:JSON.parse(String(init.body))});
      if (fail) return Response.json({detail:"配置已变化",},{status:409});
      created = true; return Response.json({...base,id:"candidate",name:"对照候选"});
    }
    if (path.includes("/team/repositories")) return Response.json({items:[repository]});
    if (path.includes("/profiles")) return Response.json({items:[base,...(created?[{...base,id:"candidate",name:"对照候选"}]:[])]});
    throw new Error("意外请求 " + path);
  }));
  render(<PlatformPage user={admin} initialTab="profiles" onSignedOut={vi.fn()}/>);
  fireEvent.click(await screen.findByRole("button",{name:"基于此方案创建候选"}));
  fireEvent.change(screen.getByLabelText("候选方案名称"),{target:{value:"对照候选"}});
  fireEvent.change(screen.getByLabelText("逻辑审查指令"),{target:{value:"只报告本次新增的可证明缺陷"}});
  fireEvent.click(screen.getByRole("button",{name:"保存候选方案"}));
  await screen.findByText("配置已变化");
  expect(screen.getByLabelText("逻辑审查指令")).toHaveValue("只报告本次新增的可证明缺陷");
  fail = false;
  fireEvent.click(screen.getByRole("button",{name:"保存候选方案"}));
  await screen.findByRole("heading",{name:"对照候选"});
  expect(writes).toHaveLength(2);
  expect(writes[1].body).toMatchObject({base_profile_id:"base",repository:repository.repository,role_instructions:{logic:"只报告本次新增的可证明缺陷"}});
  expect(writes.every(item => !item.path.includes("activate"))).toBe(true);
  expect(repository.policy.review_profile_id).toBe("base");
});

it("方案新建等待配置版本，刷新版本后保存当前快照", async () => {
  const writes: unknown[] = [];
  let version = 7;
  vi.stubGlobal("fetch", vi.fn(async (input:RequestInfo|URL, init?:RequestInit) => {
    const path = String(input);
    if (init?.method === "POST") {writes.push(JSON.parse(String(init.body)));return Response.json(base);}
    if (path.includes("/team/repositories")) return Response.json({items:[repository]});
    if (path.includes("/profiles")) return Response.json({items:[base]});
    if (path.includes("/settings/ai")) return Response.json({revision:version});
    throw new Error("意外请求 " + path);
  }));
  render(<PlatformPage user={admin} initialTab="profiles" onSignedOut={vi.fn()}/>);
  await screen.findByRole("heading",{name:"基础方案"});
  fireEvent.click(await screen.findByRole("button",{name:"新建审查方案"}));
  await screen.findByText("当前 AI 配置版本：7");
  version = 8;
  fireEvent.click(screen.getByRole("button",{name:"刷新配置版本"}));
  await screen.findByText("当前 AI 配置版本：8");
  fireEvent.change(screen.getByLabelText("方案名称"),{target:{value:"新快照"}});
  fireEvent.click(screen.getByRole("button",{name:"保存当前配置为方案"}));
  await waitFor(()=>expect(writes).toEqual([{name:"新快照",note:"",repository:repository.repository,expected_ai_revision:8}]));
});

it.each(["profiles","diagnostics"] as const)("只读身份直接进入 %s 也不请求运营数据", async initialTab => {
  const paths:string[]=[];
  vi.stubGlobal("fetch",vi.fn(async (input:RequestInfo|URL)=>{paths.push(String(input));return Response.json({items:[]});}));
  render(<PlatformPage user={{...admin,role:"viewer",permissions:["reviews:view"]}} initialTab={initialTab} onSignedOut={vi.fn()}/>);
  await screen.findByRole("heading",{name:"问题处理"});
  await waitFor(()=>expect(paths.length).toBeGreaterThan(0));
  expect(paths.some(path=>path.includes("profiles")||path.includes("diagnostics"))).toBe(false);
});

it("诊断失败可以刷新，切换窗口后迟到结果不能覆盖当前统计", async () => {
  const onError=vi.fn(), paths:string[]=[];
  let resolveOld!: (response:Response)=>void;
  let count=0;
  const report=(repository:string)=>({since:now,until:now,repositories:[{repository,queued:2,running:1,paused:0,failed:1,completed:3,oldest_queued_at:null,mean_queue_ms:10,p95_queue_ms:20,mean_model_ms:30,max_concurrent_reviews:1}],failures:[{code:"model_timeout",count:1}],provider_channels:[]});
  vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL)=>{
    const path=String(input); paths.push(path);
    if(path.includes("/diagnostics")) {
      count++;
      if(count===1)return Response.json({detail:"统计暂不可用"},{status:503});
      if(count===2)return new Promise<Response>(resolve=>{resolveOld=resolve;});
      return Response.json(report("current/repo"));
    }
    return Response.json({items:[]});
  }));
  render(<DiagnosticsPanel onError={onError}/>);
  await screen.findByText("运行统计读取失败，请点击刷新重试");
  expect(onError).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button",{name:"刷新"}));
  await waitFor(()=>expect(count).toBe(2));
  fireEvent.change(screen.getByLabelText("统计范围"),{target:{value:"30"}});
  await screen.findByText("current/repo");
  await act(async()=>resolveOld(Response.json(report("stale/repo"))));
  expect(screen.queryByText("stale/repo")).not.toBeInTheDocument();
  expect(paths.some(path=>path.includes("/audits"))).toBe(false);
  fireEvent.click(screen.getByRole("button",{name:/操作记录/}));
  await waitFor(()=>expect(paths.some(path=>path.includes("/audits"))).toBe(true));
});

it("评测概况不将加载或失败显示成单人结果，切换样本清除旧统计", async()=>{
  const onError=vi.fn(); let failed=false;
  vi.stubGlobal("fetch",vi.fn(async()=>failed?Response.json({detail:"不可用"},{status:503}):Response.json({review_mode:"dual",review_source:"双人独立提交",case_count:1,observation_count:2,reviewed_observations:0,model_duration_ms:1200})));
  const view=render(<EvaluationOverviewPanel datasetId="first" version={1} onError={onError}/>);
  expect(screen.queryByText(/评测结果 · 单人核对/)).not.toBeInTheDocument();
  await screen.findByText(/评测结果 · 双人验收/);
  failed=true;
  view.rerender(<EvaluationOverviewPanel datasetId="second" version={1} onError={onError}/>);
  await screen.findByText("评测概况读取失败");
  expect(screen.queryByText(/双人独立提交/)).not.toBeInTheDocument();
  failed=false;
  fireEvent.click(screen.getByRole("button",{name:"重试读取概况"}));
  await screen.findByText(/来源：双人独立提交/);
});

it("调用证据分页保留过期与缺失状态，不把元数据当作正文",async()=>{
  const paths:string[]=[];
  vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL)=>{
    const path=String(input);paths.push(path);
    return Response.json({items:[{id:path.includes("cursor")?"request-2":"request-1",agent:"logic",status:path.includes("cursor")?"missing":"expired",created_at:now,expires_at:now,prompt_content_sha256:"a".repeat(64),request_sha256:"b".repeat(64),application_revision:"c".repeat(40),byte_size:0,model:"fixture",api_protocol:"responses",attempt_kind:"initial",split_depth:0}],next_cursor:path.includes("cursor")?null:"page2"});
  }));
  render(<EvaluationOutputPanel runId="run-1" onError={vi.fn()}/>);
  await screen.findByText(/在线正文已到期/);
  fireEvent.click(within(screen.getByRole("navigation")).getByRole("button",{name:"下一页"}));
  await screen.findByText(/缺少可保存输出/);
  expect(screen.queryByText("request-1")).not.toBeInTheDocument();
  expect(paths).toHaveLength(2);
});
