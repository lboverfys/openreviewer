import { describe, expect, it } from "vitest";

import {
  batchInputOptions,
  normalizeProviderSettings,
  optionalUsdToMicrousd,
  outputTokenOptions,
  providerDraft,
  providerHasChanges,
  requiredInteger,
} from "./settings-drafts";
import type { AiProviderSettings } from "./types";

function provider(
  overrides: Partial<AiProviderSettings> = {},
): AiProviderSettings {
  return {
    provider: "openai",
    configured: true,
    active: true,
    model: "gpt-4.1-mini",
    api_protocol: "responses",
    api_base_url: null,
    reasoning_effort: "none",
    api_key_configured: true,
    api_key_mask: "sk-...abcd",
    context_window_tokens: 128_000,
    max_output_tokens: 8_192,
    max_batch_input_tokens: 64_000,
    connect_timeout_seconds: 5,
    read_timeout_seconds: 120,
    write_timeout_seconds: 30,
    pool_timeout_seconds: 5,
    max_request_bytes: 4_194_304,
    max_response_bytes: 4_194_304,
    input_usd_per_million: null,
    output_usd_per_million: null,
    cache_read_usd_per_million: null,
    cache_write_usd_per_million: null,
    test_status: "succeeded",
    tested_at: null,
    updated_at: null,
    ...overrides,
  };
}

describe("设置草稿数值转换", () => {
  it("将美元精确换算为微美元，并拒绝空泛或不安全的值", () => {
    expect(optionalUsdToMicrousd("1.25")).toBe(1_250_000);
    expect(optionalUsdToMicrousd(" ")).toBeNull();
    expect(() => optionalUsdToMicrousd("0")).toThrow("必须大于 0");
    expect(() => requiredInteger("1.5", "批次数")).toThrow("必须是整数");
  });

  it("为旧版缺失字段补默认值，并按上下文窗口夹紧上限", () => {
    const legacy = provider({
      context_window_tokens: undefined as unknown as number,
      max_output_tokens: 999_999,
      max_batch_input_tokens: 999_999,
      reasoning_effort: "legacy" as AiProviderSettings["reasoning_effort"],
    });

    const normalized = normalizeProviderSettings(legacy);

    expect(normalized.reasoning_effort).toBe("none");
    expect(normalized.context_window_tokens).toBe(128_000);
    expect(normalized.max_output_tokens).toBe(123_904);
    expect(normalized.max_batch_input_tokens).toBe(123_904);
  });

  it("只提供不超过上下文余量的输出和批次预设", () => {
    expect(outputTokenOptions("16384", "8192").map(([value]) => Number(value)))
      .toEqual([2_048, 4_096, 8_192]);
    expect(batchInputOptions("32768", "16384").map(([value]) => Number(value)))
      .toEqual([16_384]);
  });

  it("能区分原样草稿与真正需要保存的修改", () => {
    const settings = provider();
    const draft = providerDraft(settings);

    expect(providerHasChanges(settings, draft)).toBe(false);
    expect(providerHasChanges(settings, { ...draft, apiKey: "new-secret" })).toBe(true);
    expect(providerHasChanges(settings, {
      ...draft,
      useCustomEndpoint: true,
      apiBaseUrl: " https://gateway.example.com ",
    })).toBe(true);
  });
});
