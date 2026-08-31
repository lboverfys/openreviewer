// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import AgentSettingsPanel from "./AgentSettingsPanel";
import type { AiAgentSettingsResponse, ReviewAgent } from "./types";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      agentSettings: vi.fn(),
    },
  };
});

function agent(agentName: ReviewAgent, model = "gpt-test") {
  return {
    agent: agentName,
    configured: true,
    enabled: false,
    provider: "openai" as const,
    model,
    api_protocol: "chat_completions" as const,
    api_base_url: null,
    reasoning_effort: "none" as const,
    api_key_configured: true,
    api_key_mask: "sk-***test",
    context_window_tokens: 128000,
    max_output_tokens: 8192,
    max_batch_input_tokens: 64000,
    connect_timeout_seconds: 5,
    read_timeout_seconds: 180,
    write_timeout_seconds: 30,
    pool_timeout_seconds: 5,
    max_retries: 2,
    test_status: "succeeded" as const,
    tested_at: null,
    updated_at: null,
  };
}

function settings(revision: number): AiAgentSettingsResponse {
  return {
    revision,
    agents: [
      agent("security"),
      agent("convention"),
      agent("logic"),
      agent("summary"),
    ],
  };
}

afterEach(() => {
  vi.mocked(api.agentSettings).mockReset();
});

describe("Agent 配置 revision 同步", () => {
  it("只上报更高版本，并在父版本变化时保留未保存草稿", async () => {
    vi.mocked(api.agentSettings).mockResolvedValue(settings(2));
    const onRevisionChange = vi.fn();
    const onSignedOut = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <AgentSettingsPanel
        refreshRequest={0}
        parentRevision={1}
        onRevisionChange={onRevisionChange}
        onSignedOut={onSignedOut}
      />,
    );

    await waitFor(() => expect(screen.getAllByLabelText("模型 ID")).toHaveLength(4));
    expect(onRevisionChange).toHaveBeenCalledTimes(1);
    expect(onRevisionChange).toHaveBeenCalledWith(2);

    const modelInput = screen.getAllByLabelText("模型 ID")[0];
    await user.clear(modelInput);
    await user.type(modelInput, "local-draft");

    // 父页面尚未把新 revision 作为 prop 回传时，旧快照不能覆盖草稿，
    // 也不能重复上报同一个版本。
    vi.mocked(api.agentSettings).mockResolvedValue(settings(1));
    rerender(
      <AgentSettingsPanel
        refreshRequest={1}
        parentRevision={1}
        onRevisionChange={onRevisionChange}
        onSignedOut={onSignedOut}
      />,
    );
    await waitFor(() => expect(api.agentSettings).toHaveBeenCalledTimes(2));
    expect(onRevisionChange).toHaveBeenCalledTimes(1);
    expect(modelInput).toHaveValue("local-draft");

    // 父页面只同步 revision，不应触发 Agent 草稿重置。
    await act(async () => {
      rerender(
        <AgentSettingsPanel
          refreshRequest={1}
          parentRevision={3}
          onRevisionChange={onRevisionChange}
          onSignedOut={onSignedOut}
        />,
      );
    });
    expect(modelInput).toHaveValue("local-draft");

    // 即使刷新拿到更高版本，也只能更新服务端快照，不能抹掉用户草稿。
    vi.mocked(api.agentSettings).mockResolvedValue(settings(4));
    rerender(
      <AgentSettingsPanel
        refreshRequest={2}
        parentRevision={3}
        onRevisionChange={onRevisionChange}
        onSignedOut={onSignedOut}
      />,
    );
    await waitFor(() => expect(api.agentSettings).toHaveBeenCalledTimes(3));
    expect(modelInput).toHaveValue("local-draft");
  });
});
