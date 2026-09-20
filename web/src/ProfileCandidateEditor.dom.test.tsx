// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import ProfileCandidateEditor from "./ProfileCandidateEditor";
import SnapshotProfilePicker from "./SnapshotProfilePicker";
import { platformApi } from "./platform-api";
import type { ReviewProfile } from "./types";

const base: ReviewProfile = {id:"base", name:"基础方案", repository:"example/repo", note:"",fingerprint:"a".repeat(64),ai_revision:1,
  prompt_version:"structured-review-v5",prompt_content_sha256:"b".repeat(64),models:{logic:"model"},knowledge_versions:{},retrieval_settings:{},
  role_instructions:{security:"安全原指令",convention:"规范原指令",logic:"逻辑原指令",summary:"汇总原指令"},supplementary_instructions:"",
  created_by:"admin",created_at:"2026-09-21T00:00:00Z"};

afterEach(()=>{cleanup();vi.restoreAllMocks();});

it("编辑候选只提交变化的角色，展示前后内容且不启用方案",async()=>{
  const create = vi.spyOn(platformApi,"createProfile").mockResolvedValue({...base,id:"candidate"});
  const activate = vi.spyOn(platformApi,"activateProfile");
  const saved = vi.fn();
  render(<ProfileCandidateEditor base={base} onSaved={saved} onCancel={vi.fn()} onError={vi.fn()}/>);
  fireEvent.change(screen.getByLabelText("逻辑审查指令"),{target:{value:"仅核查本次新增的事务问题"}});
  fireEvent.change(screen.getByLabelText("补充审查要求"),{target:{value:"记录缺少上下文的判断"}});
  expect(screen.getByText("原指令")).toBeInTheDocument();
  expect(screen.getByText("逻辑原指令")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"保存候选方案"}));
  await waitFor(()=>expect(saved).toHaveBeenCalledOnce());
  expect(create.mock.calls[0][0]).toMatchObject({base_profile_id:"base",repository:"example/repo",role_instructions:{logic:"仅核查本次新增的事务问题"},supplementary_instructions:"记录缺少上下文的判断"});
  expect(create.mock.calls[0][0]).not.toHaveProperty("system");
  expect(activate).not.toHaveBeenCalled();
});

it("试跑选择仅加载同仓库的分页方案并返回所选身份",async()=>{
  const load = vi.spyOn(platformApi,"profiles").mockResolvedValue({items:[base,{...base,id:"candidate",name:"候选方案"}],next_cursor:null});
  const selected = vi.fn();
  render(<SnapshotProfilePicker repository="example/repo" selected="" onSelected={selected} onError={vi.fn()} disabled={false}/>);
  await screen.findByRole("option",{name:/候选方案/});
  fireEvent.change(screen.getByLabelText("本次试跑方案"),{target:{value:"candidate"}});
  expect(selected).toHaveBeenCalledWith("candidate");
  expect(load.mock.calls[0][0]).toBe("example/repo");
});
