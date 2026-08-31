import { describe, expect, it } from "vitest";

import {
  batchInputOptions,
  mergeProviderDrafts,
  mergeReviewPolicyDraft,
  normalizeProviderSettings,
  optionalUsdToMicrousd,
  outputTokenOptions,
  providerDraft,
  providerHasChanges,
  requiredInteger,
  reviewPolicyHasChanges,
  reviewPolicyDraft,
} from "./settings-drafts";
import type { AiProviderSettings, AiSettings } from "./types";

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

describe("设置快照与本地草稿合并", () => {
  it("保存一个 Provider 时保留另一个 Provider 的未保存输入", () => {
    const openai = provider({ provider: "openai", model: "server-openai" });
    const anthropic = provider({
      provider: "anthropic",
      model: "server-anthropic",
      api_protocol: "messages",
    });
    const previous = { providers: [openai, anthropic] } as AiSettings;
    const next = { providers: [
      { ...openai, model: "saved-openai" },
      anthropic,
    ] } as AiSettings;
    const local = providerDraft(anthropic);
    local.model = "local-anthropic-draft";

    const merged = mergeProviderDrafts(
      previous,
      { openai: providerDraft(openai), anthropic: local },
      next,
      "openai",
    );

    expect(merged.openai.model).toBe("saved-openai");
    expect(merged.anthropic.model).toBe("local-anthropic-draft");
  });

  it("刷新时保留未保存的审查策略，成功保存后才重置", () => {
    const previous = {
      max_units: 10,
      max_scope_depth: 2,
      max_unit_input_bytes: 1024,
      max_total_input_bytes: 2048,
      max_model_http_calls: 4,
      max_model_input_tokens: 1000,
      max_model_output_tokens: 500,
      max_model_cost_microusd: null,
      max_model_duration_seconds: 60,
    } as AiSettings;
    const next = { ...previous, max_units: 20 } as AiSettings;
    const local = reviewPolicyDraft(previous);
    local.maxUnits = "15";

    expect(mergeReviewPolicyDraft(previous, local, next).maxUnits).toBe("15");
    expect(mergeReviewPolicyDraft(previous, local, next, true).maxUnits).toBe("20");
  });

  it("忽略仅为兼容保留的隐藏预算字段", () => {
    const settings = {
      max_units: 10,
      max_scope_depth: 2,
      max_unit_input_bytes: 1024,
      max_total_input_bytes: 2048,
      max_model_http_calls: 4,
      max_model_input_tokens: 1000,
      max_model_output_tokens: 500,
      max_model_cost_microusd: null,
      max_model_duration_seconds: 60,
    } as AiSettings;
    const draft = reviewPolicyDraft(settings);
    draft.maxModelHttpCalls = "999";
    draft.maxModelInputTokens = "999999";
    draft.maxModelOutputTokens = "999999";
    draft.maxModelCostUsd = "42";

    // 这些字段不再出现在表单中，服务端变化不应制造假脏状态。
    expect(reviewPolicyHasChanges(settings, draft)).toBe(false);
  });
});
