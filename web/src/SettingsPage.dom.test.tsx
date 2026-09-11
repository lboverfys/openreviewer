// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";

import { render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import SettingsPage from "./SettingsPage";
import type { AiSettings } from "./types";

vi.mock("./AgentSettingsPanel", () => ({
  default: ({ onRevisionChange }: { onRevisionChange: (revision: number) => void }) => {
    useEffect(() => {
      // 模拟 Agent 端点先于主配置端点返回了更新后的全局 revision。
      onRevisionChange(2);
    }, [onRevisionChange]);
    return <div data-testid="agent-settings-mock" />;
  },
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      aiSettings: vi.fn(),
    },
  };
});

function settings(revision: number): AiSettings {
  return {
    revision,
    active_provider: "openai",
    max_units: 100,
    max_scope_depth: 8,
    max_unit_input_bytes: 1024 * 1024,
    max_total_input_bytes: 10 * 1024 * 1024,
    updated_at: null,
    updated_by: "admin",
    providers: [
      {
        provider: "openai",
        active: true,
        configured: true,
        model: "gpt-test",
        api_protocol: "chat_completions",
        api_base_url: null,
        api_key_configured: true,
        api_key_mask: "sk-***test",
        reasoning_effort: "none",
        context_window_tokens: 128000,
        max_output_tokens: 8192,
        max_batch_input_tokens: 64000,
        connect_timeout_seconds: 5,
        read_timeout_seconds: 180,
        write_timeout_seconds: 30,
        pool_timeout_seconds: 5,
        max_request_bytes: 4 * 1024 * 1024,
        max_response_bytes: 2 * 1024 * 1024,
        input_usd_per_million: null,
        output_usd_per_million: null,
        cache_read_usd_per_million: null,
        cache_write_usd_per_million: null,
        test_status: "succeeded",
        tested_at: null,
        updated_at: null,
      },
      {
        provider: "anthropic",
        active: false,
        configured: false,
        model: "claude-test",
        api_protocol: "messages",
        api_base_url: null,
        api_key_configured: false,
        api_key_mask: null,
        reasoning_effort: "none",
        context_window_tokens: 200000,
        max_output_tokens: 8192,
        max_batch_input_tokens: 64000,
        connect_timeout_seconds: 5,
        read_timeout_seconds: 180,
        write_timeout_seconds: 30,
        pool_timeout_seconds: 5,
        max_request_bytes: 4 * 1024 * 1024,
        max_response_bytes: 2 * 1024 * 1024,
        input_usd_per_million: null,
        output_usd_per_million: null,
        cache_read_usd_per_million: null,
        cache_write_usd_per_million: null,
        test_status: "untested",
        tested_at: null,
        updated_at: null,
      },
    ],
  };
}

afterEach(() => {
  vi.mocked(api.aiSettings).mockReset();
});

describe("设置页初始 revision 同步", () => {
  it("Agent 先返回新 revision 时仍会显示主配置并补取一次快照", async () => {
    vi.mocked(api.aiSettings)
      .mockResolvedValueOnce(settings(1))
      .mockResolvedValueOnce(settings(2));

    render(
      <SettingsPage
        onSignedOut={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByDisplayValue("gpt-test")).toBeInTheDocument();
    });
    await waitFor(() => expect(api.aiSettings).toHaveBeenCalledTimes(2));
    expect(screen.getByText("配置版本 2")).toBeInTheDocument();
    expect(
      screen.queryByText(/资源预算|资源统计|费用统计|模型用量|单批预算/),
    ).not.toBeInTheDocument();
  });
});
