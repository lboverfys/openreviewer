// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import {cleanup,fireEvent,render,screen,waitFor} from "@testing-library/react";
import {afterEach,expect,it,vi} from "vitest";
import {api,clearReadCache} from "./api";
import {platformApi} from "./platform-api";
import ProfileCandidateEditor from "./ProfileCandidateEditor";
import ReviewDetailPage from "./ReviewDetailPage";
import EvaluationImportPanel from "./EvaluationImportPanel";
import EvaluationReportPanel from "./EvaluationReportPanel";
import fixture from "./fixtures/review-details.json";
import type {AuthUser,EvaluationCaseDetail,EvaluationDataset,EvaluationReport,ReviewDetails,ReviewProfile} from "./types";

afterEach(()=>{cleanup();clearReadCache();vi.restoreAllMocks();});

it("候选编辑、固定提交试跑、收录候选和未复核报告使用同一组身份",async()=>{
  const details:ReviewDetails={...(fixture as unknown as ReviewDetails),snapshot_review:false,available_actions:["review_snapshot"]};
  const base={id:"base",name:"基础方案",repository:details.repository,models:{logic:"fixed-model"},role_instructions:{logic:"审查业务逻辑"},supplementary_instructions:"",knowledge_versions:{},retrieval_settings:{},fingerprint:"a".repeat(64),ai_revision:1,note:"",prompt_version:"v1",created_by:"admin",created_at:details.created_at} as ReviewProfile;
  const candidate={...base,id:"candidate",name:"候选方案",base_profile_id:"base"};
  const user={username:"admin",role:"administrator",permissions:["reviews:view","reviews:manage","findings:adjudicate","settings:manage"]} as AuthUser;
  const create=vi.spyOn(platformApi,"createProfile").mockResolvedValue(candidate);
  const activate=vi.spyOn(platformApi,"activateProfile");
  const saved=vi.fn(), onError=vi.fn();
  const editor=render(<ProfileCandidateEditor base={base} onSaved={saved} onCancel={vi.fn()} onError={onError}/>);
  fireEvent.change(screen.getByLabelText("候选方案名称"),{target:{value:"候选方案"}});
  fireEvent.change(screen.getByLabelText("逻辑审查指令"),{target:{value:"结合调用方核对新增缺陷"}});
  fireEvent.click(screen.getByRole("button",{name:"保存候选方案"}));
  await waitFor(()=>expect(saved).toHaveBeenCalledOnce());
  expect(create).toHaveBeenCalledWith(expect.objectContaining({base_profile_id:"base",repository:details.repository,role_instructions:{logic:"结合调用方核对新增缺陷"}}));
  editor.unmount();

  vi.spyOn(api,"reviewDetails").mockResolvedValue(details);
  vi.spyOn(api,"reviewRetrieval").mockResolvedValue([]);
  vi.spyOn(platformApi,"profiles").mockResolvedValue({items:[candidate],next_cursor:null});
  const action=vi.spyOn(api,"reviewAction").mockResolvedValue({action:"review_snapshot",review_run_id:"trial",review_task_id:"trial-task",execution_status:"queued",workflow_status:"agent_batches"});
  vi.spyOn(window,"confirm").mockReturnValue(true);
  const open=vi.fn();
  const page=render(<ReviewDetailPage user={user} reviewRunId={details.review_run_id} onOpenReview={open} onBack={vi.fn()} onSignedOut={vi.fn()}/>);
  await screen.findByRole("option",{name:/候选方案/});
  fireEvent.change(screen.getByLabelText("本次试跑方案"),{target:{value:"candidate"}});
  fireEvent.click(screen.getByRole("checkbox",{name:/留存本次评测输出/}));
  fireEvent.click(screen.getByRole("button",{name:"复查此版本"}));
  await waitFor(()=>expect(open).toHaveBeenCalledWith("trial"));
  expect(action.mock.calls[0][0]).toBe(details.review_run_id);
  expect(action.mock.calls[0][4]).toMatchObject({headSha:details.head_sha,reviewProfileId:"candidate",captureModelOutputs:true});
  page.unmount();

  const dataset={id:"set",name:"配对试验",repository:details.repository,revision:1,review_mode:"dual"} as EvaluationDataset;
  const sample={id:"case",split:"validation",kind:"normal",baseline:{source_run_id:details.review_run_id},candidate:null} as EvaluationCaseDetail;
  vi.spyOn(api,"evaluationSources").mockResolvedValue({items:[],next_cursor:null});
  const collected=vi.spyOn(api,"importEvaluationObservations").mockResolvedValue({dataset_id:"set",case_ids:["case"],imported:1});
  const imported=vi.fn();
  const collect=render(<EvaluationImportPanel dataset={dataset} sample={sample} initialRunId="trial" onSaved={imported} onCancel={vi.fn()} onError={onError}/>);
  fireEvent.click(screen.getByRole("button",{name:"添加这份审查进行比较"}));
  await waitFor(()=>expect(imported).toHaveBeenCalledWith("set"));
  expect(collected).toHaveBeenCalledWith("set",{review_run_ids:["trial"],variant:"candidate",split:"validation",kind:"normal"});
  collect.unmount();

  const score={sample_count:0,finding_count:0,valid_count:0,false_positive_count:0,precision:null,recall:null,precision_ci95:null,recall_ci95:null,location_accuracy:null,duplicate_rate:null,reference_true_positive_count:0,reference_expected_count:0,mean_turnaround_ms:100,mean_model_duration_ms:80,mean_estimated_cost_usd:null,input_tokens:100,output_tokens:20};
  vi.spyOn(api,"evaluationReport").mockResolvedValue({dataset_id:"set",review_mode:"dual",case_count:1,performance_pairs:1,quality_pairs:0,reference_pairs:0,priced_pairs:0,missing_baseline:0,missing_candidate:0,pending_pairs:1,disputed_pairs:0,notices:["待两位成员独立提交"],baseline:score,candidate:score} as EvaluationReport);
  render(<EvaluationReportPanel datasetId="set" onError={onError}/>);
  await screen.findByText("请完成两份审查的核对");
  expect(screen.queryByText("100.0%")).not.toBeInTheDocument();
  expect(screen.getAllByText("未知")).toHaveLength(2);
  expect(activate).not.toHaveBeenCalled();
  expect(action).toHaveBeenCalledOnce();
  expect(onError).not.toHaveBeenCalled();
});
