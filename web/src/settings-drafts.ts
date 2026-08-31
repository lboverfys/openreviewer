import type {
  AiApiProtocol,
  AiProvider,
  AiProviderSettings,
  AiReasoningEffort,
  AiSettings,
} from "./types";

export interface ProviderDraft {
  model: string;
  apiProtocol: AiApiProtocol;
  useCustomEndpoint: boolean;
  apiBaseUrl: string;
  apiKey: string;
  clearApiKey: boolean;
  reasoningEffort: AiReasoningEffort;
  contextWindowTokens: string;
  maxOutputTokens: string;
  maxBatchInputTokens: string;
  connectTimeoutSeconds: string;
  readTimeoutSeconds: string;
  writeTimeoutSeconds: string;
  poolTimeoutSeconds: string;
  maxRequestBytes: string;
  maxResponseBytes: string;
  inputPrice: string;
  outputPrice: string;
  cacheReadPrice: string;
  cacheWritePrice: string;
}

export interface ReviewPolicyDraft {
  maxUnits: string;
  maxScopeDepth: string;
  maxUnitInputKib: string;
  maxTotalInputMib: string;
  maxModelHttpCalls: string;
  maxModelInputTokens: string;
  maxModelOutputTokens: string;
  maxModelCostUsd: string;
  maxModelDurationSeconds: string;
}

export const KIB = 1024;
export const MIB = 1024 * KIB;

const reasoningEfforts: AiReasoningEffort[] = [
  "none",
  "low",
  "medium",
  "high",
  "max",
];

export function providerDraft(settings: AiProviderSettings): ProviderDraft {
  return {
    model: settings.model,
    apiProtocol: settings.api_protocol,
    useCustomEndpoint: Boolean(settings.api_base_url),
    apiBaseUrl: settings.api_base_url ?? "",
    apiKey: "",
    clearApiKey: false,
    reasoningEffort: settings.reasoning_effort,
    contextWindowTokens: String(settings.context_window_tokens),
    maxOutputTokens: String(settings.max_output_tokens),
    maxBatchInputTokens: String(settings.max_batch_input_tokens),
    connectTimeoutSeconds: String(settings.connect_timeout_seconds),
    readTimeoutSeconds: String(settings.read_timeout_seconds),
    writeTimeoutSeconds: String(settings.write_timeout_seconds),
    poolTimeoutSeconds: String(settings.pool_timeout_seconds),
    maxRequestBytes: String(settings.max_request_bytes),
    maxResponseBytes: String(settings.max_response_bytes),
    inputPrice: settings.input_usd_per_million ?? "",
    outputPrice: settings.output_usd_per_million ?? "",
    cacheReadPrice: settings.cache_read_usd_per_million ?? "",
    cacheWritePrice: settings.cache_write_usd_per_million ?? "",
  };
}

export function reviewPolicyDraft(settings: AiSettings): ReviewPolicyDraft {
  return {
    maxUnits: String(settings.max_units),
    maxScopeDepth: String(settings.max_scope_depth),
    maxUnitInputKib: bytesToInput(String(settings.max_unit_input_bytes), KIB),
    maxTotalInputMib: bytesToInput(String(settings.max_total_input_bytes), MIB),
    maxModelHttpCalls: String(settings.max_model_http_calls),
    maxModelInputTokens: String(settings.max_model_input_tokens),
    maxModelOutputTokens: String(settings.max_model_output_tokens),
    maxModelCostUsd: settings.max_model_cost_microusd === null
      ? ""
      : String(settings.max_model_cost_microusd / 1_000_000),
    maxModelDurationSeconds: String(settings.max_model_duration_seconds),
  };
}

export function requiredNumber(value: string, label: string): number {
  if (!value.trim()) throw new Error(`请填写${label}`);
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`${label}必须是数字`);
  return parsed;
}

export function requiredInteger(value: string, label: string): number {
  const parsed = requiredNumber(value, label);
  if (!Number.isInteger(parsed)) throw new Error(`${label}必须是整数`);
  return parsed;
}

export function optionalUsdToMicrousd(value: string): number | null {
  if (!value.trim()) return null;
  const usd = requiredNumber(value, "单次审查费用上限");
  const microusd = Math.round(usd * 1_000_000);
  if (microusd < 1 || !Number.isSafeInteger(microusd)) {
    throw new Error("单次审查费用上限必须大于 0，且不能超过安全数值范围");
  }
  return microusd;
}

export function optionalDecimal(value: string): string | null {
  const normalized = value.trim();
  return normalized ? normalized : null;
}

export function bytesToInput(value: string, unit: number): string {
  if (!value) return "";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? String(parsed / unit) : "";
}

export function inputToBytes(value: string, unit: number): string {
  if (!value) return "";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? String(Math.round(parsed * unit)) : "";
}

export function formatTokens(value: string | number): string {
  const tokens = Number(value);
  if (!Number.isFinite(tokens)) return "--";
  if (tokens >= 1_000_000) return `${Number((tokens / 1_000_000).toFixed(2))}M`;
  if (tokens >= 1_000) return `${Number((tokens / 1_000).toFixed(0))}K`;
  return String(tokens);
}

function finiteIntegerOr(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && Number.isInteger(value)
    ? value
    : fallback;
}

export function normalizeProviderSettings(
  settings: AiProviderSettings,
): AiProviderSettings {
  const raw = settings as AiProviderSettings & {
    reasoning_effort?: unknown;
    context_window_tokens?: unknown;
    max_output_tokens?: unknown;
    max_batch_input_tokens?: unknown;
  };
  const defaultContext = settings.provider === "anthropic" ? 200_000 : 128_000;
  const contextWindowTokens = Math.max(
    8_192,
    Math.min(4_000_000, finiteIntegerOr(raw.context_window_tokens, defaultContext)),
  );
  const requestedOutput = finiteIntegerOr(raw.max_output_tokens, 8_192);
  const maxOutputTokens = Math.max(
    256,
    Math.min(131_072, contextWindowTokens - 4_096, requestedOutput),
  );
  const requestedBatch = finiteIntegerOr(raw.max_batch_input_tokens, 64_000);
  const maxBatchInputTokens = Math.max(
    4_096,
    Math.min(4_000_000, contextWindowTokens - 4_096, requestedBatch),
  );
  return {
    ...settings,
    reasoning_effort: reasoningEfforts.includes(raw.reasoning_effort as AiReasoningEffort)
      ? raw.reasoning_effort as AiReasoningEffort
      : "none",
    context_window_tokens: contextWindowTokens,
    max_output_tokens: maxOutputTokens,
    max_batch_input_tokens: maxBatchInputTokens,
  };
}

export function normalizeAiSettings(next: AiSettings): AiSettings {
  return {
    ...next,
    providers: next.providers.map(normalizeProviderSettings),
  };
}

export function contextWindowOptions(current: string): Array<[string, string]> {
  const presets: Array<[string, string]> = [
    ["8192", "8K Token"],
    ["16384", "16K Token"],
    ["32768", "32K Token"],
    ["65536", "64K Token"],
    ["128000", "128K Token"],
    ["200000", "200K Token"],
    ["256000", "256K Token"],
    ["384000", "384K Token"],
    ["1000000", "1M Token"],
    ["2000000", "2M Token"],
    ["4000000", "4M Token"],
  ];
  if (presets.some(([value]) => value === current)) return presets;
  return [[current, `${formatTokens(current)} Token（当前值）`], ...presets];
}

export function outputTokenOptions(
  context: string,
  current: string,
): Array<[string, string]> {
  const contextTokens = Number(context);
  const presets: Array<[string, string]> = [
    ["2048", "简短（2K Token）"],
    ["4096", "日常（4K Token）"],
    ["8192", "标准（8K Token）"],
    ["16384", "详细（16K Token）"],
    ["32768", "超长（32K Token）"],
    ["65536", "极长（64K Token）"],
    ["131072", "最大（128K Token）"],
  ].filter(([value]) => Number(value) <= contextTokens - 4_096) as Array<[
    string,
    string,
  ]>;
  if (presets.some(([value]) => value === current)) return presets;
  return [[current, `${formatTokens(current)} Token（当前值）`], ...presets];
}

export function batchInputOptions(
  context: string,
  current: string,
): Array<[string, string]> {
  const contextTokens = Number(context);
  const presets: Array<[string, string]> = [
    ["16384", "小批（16K）"],
    ["32768", "稳妥（32K）"],
    ["64000", "推荐（64K）"],
    ["96000", "大批（96K）"],
    ["128000", "超大（128K）"],
    ["256000", "极大（256K）"],
  ].filter(
    ([value]) => Number(value) <= Math.max(4_096, contextTokens - 4_096),
  ) as Array<[string, string]>;
  if (presets.some(([value]) => value === current)) return presets;
  return [[current, `${formatTokens(current)}（当前值）`], ...presets];
}

export function providerHasChanges(
  settings: AiProviderSettings,
  draft: ProviderDraft,
): boolean {
  const effectiveBaseUrl = draft.useCustomEndpoint
    ? draft.apiBaseUrl.trim() || null
    : null;
  return (
    draft.model.trim() !== settings.model
    || draft.apiProtocol !== settings.api_protocol
    || effectiveBaseUrl !== settings.api_base_url
    || Boolean(draft.apiKey.trim())
    || draft.clearApiKey
    || draft.reasoningEffort !== settings.reasoning_effort
    || Number(draft.contextWindowTokens) !== settings.context_window_tokens
    || Number(draft.maxOutputTokens) !== settings.max_output_tokens
    || Number(draft.maxBatchInputTokens) !== settings.max_batch_input_tokens
    || Number(draft.connectTimeoutSeconds) !== settings.connect_timeout_seconds
    || Number(draft.readTimeoutSeconds) !== settings.read_timeout_seconds
    || Number(draft.writeTimeoutSeconds) !== settings.write_timeout_seconds
    || Number(draft.poolTimeoutSeconds) !== settings.pool_timeout_seconds
    || Number(draft.maxRequestBytes) !== settings.max_request_bytes
    || Number(draft.maxResponseBytes) !== settings.max_response_bytes
    || draft.inputPrice !== (settings.input_usd_per_million ?? "")
    || draft.outputPrice !== (settings.output_usd_per_million ?? "")
    || draft.cacheReadPrice !== (settings.cache_read_usd_per_million ?? "")
    || draft.cacheWritePrice !== (settings.cache_write_usd_per_million ?? "")
  );
}

export function reviewPolicyHasChanges(
  settings: AiSettings,
  draft: ReviewPolicyDraft,
): boolean {
  const savedCostUsd = settings.max_model_cost_microusd === null
    ? ""
    : String(settings.max_model_cost_microusd / 1_000_000);
  return (
    Number(draft.maxUnits) !== settings.max_units
    || Number(draft.maxScopeDepth) !== settings.max_scope_depth
    || Number(inputToBytes(draft.maxUnitInputKib, KIB)) !== settings.max_unit_input_bytes
    || Number(inputToBytes(draft.maxTotalInputMib, MIB)) !== settings.max_total_input_bytes
    || Number(draft.maxModelHttpCalls) !== settings.max_model_http_calls
    || Number(draft.maxModelInputTokens) !== settings.max_model_input_tokens
    || Number(draft.maxModelOutputTokens) !== settings.max_model_output_tokens
    || draft.maxModelCostUsd.trim() !== savedCostUsd
    || Number(draft.maxModelDurationSeconds) !== settings.max_model_duration_seconds
  );
}

/**
 * 合并服务端新快照和本地 Provider 草稿。
 *
 * 完整配置响应会包含所有 Provider；保存其中一个时，其他 Provider 仍可能有
 * 未保存输入。只重置明确保存的 Provider，其余脏草稿继续保留。
 */
export function mergeProviderDrafts(
  previous: AiSettings | null,
  current: Partial<Record<AiProvider, ProviderDraft>>,
  next: AiSettings,
  resetProvider?: AiProvider,
): Record<AiProvider, ProviderDraft> {
  const previousProviders = new Map(
    previous?.providers.map((item) => [item.provider, item]) ?? [],
  );
  return Object.fromEntries(
    next.providers.map((item) => {
      const savedBefore = previousProviders.get(item.provider);
      const localDraft = current[item.provider];
      const keepLocal = (
        item.provider !== resetProvider
        && savedBefore !== undefined
        && localDraft !== undefined
        && providerHasChanges(savedBefore, localDraft)
      );
      return [item.provider, keepLocal ? localDraft : providerDraft(item)];
    }),
  ) as Record<AiProvider, ProviderDraft>;
}

/** 保留未保存的审查策略草稿；策略保存成功时由调用方显式重置。 */
export function mergeReviewPolicyDraft(
  previous: AiSettings | null,
  current: ReviewPolicyDraft | null,
  next: AiSettings,
  reset = false,
): ReviewPolicyDraft {
  if (
    !reset
    && previous !== null
    && current !== null
    && reviewPolicyHasChanges(previous, current)
  ) {
    return current;
  }
  return reviewPolicyDraft(next);
}
