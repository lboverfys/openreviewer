import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { api, ApiError } from "./api";
import AgentSettingsPanel from "./AgentSettingsPanel";
import {
  batchInputOptions,
  bytesToInput,
  contextWindowOptions,
  formatTokens,
  inputToBytes,
  KIB,
  MIB,
  normalizeAiSettings,
  optionalDecimal,
  optionalUsdToMicrousd,
  outputTokenOptions,
  providerDraft,
  providerHasChanges,
  requiredInteger,
  requiredNumber,
  reviewPolicyDraft,
  reviewPolicyHasChanges,
} from "./settings-drafts";
import type { ProviderDraft, ReviewPolicyDraft } from "./settings-drafts";
import { testStatusLabels } from "./settings-labels";
import type {
  AiApiProtocol,
  AiProvider,
  AiProviderSettings,
  AiProviderUpdate,
  AiReasoningEffort,
  AiSettings,
  AuthUser,
  ConfigurationAudit,
  ReviewPolicyUpdate,
} from "./types";
import { errorMessage, formatDate } from "./utils";

interface SettingsPageProps {
  user: AuthUser;
  onBack: () => void;
  onSignedOut: (message?: string) => void;
}

const providerLabels: Record<AiProvider, string> = {
  openai: "OpenAI 兼容",
  anthropic: "Anthropic 兼容",
};

const providerShortLabels: Record<AiProvider, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
};

const officialEndpoints: Record<AiProvider, string> = {
  openai: "https://api.openai.com",
  anthropic: "https://api.anthropic.com",
};

const modelPlaceholders: Record<AiProvider, string> = {
  openai: "例如 gpt-4.1-mini 或中转站提供的模型 ID",
  anthropic: "例如 claude-sonnet-4-5 或中转站提供的模型 ID",
};

const openAiProtocolLabels: Record<
  Extract<AiApiProtocol, "responses" | "chat_completions">,
  { title: string; note: string }
> = {
  chat_completions: { title: "通用兼容", note: "多数中转站支持" },
  responses: { title: "Responses", note: "官方与新式中转站" },
};

const reasoningEffortLabels: Record<
  AiReasoningEffort,
  { title: string; note: string }
> = {
  none: { title: "自动", note: "推荐，不发送额外参数" },
  low: { title: "轻量", note: "更快、更省 Token" },
  medium: { title: "标准", note: "质量与速度平衡" },
  high: { title: "深入", note: "复杂代码审查" },
  max: { title: "极致", note: "仅模型明确支持时" },
};

const auditActionLabels: Record<string, string> = {
  "provider.openai.updated": "保存 OpenAI 配置",
  "provider.anthropic.updated": "保存 Anthropic 配置",
  "provider.openai.test_succeeded": "OpenAI 连接测试通过",
  "provider.anthropic.test_succeeded": "Anthropic 连接测试通过",
  "provider.openai.test_failed": "OpenAI 连接测试失败",
  "provider.anthropic.test_failed": "Anthropic 连接测试失败",
  "provider.openai.activated": "激活 OpenAI",
  "provider.anthropic.activated": "激活 Anthropic",
  "review_policy.updated": "更新审查范围",
};

const fieldLabels: Record<string, string> = {
  active_provider: "当前使用的服务",
  api_protocol: "接口格式",
  api_base_url: "API 地址",
  api_key: "API Key",
  model: "模型 ID",
  context_window_tokens: "上下文窗口",
  max_output_tokens: "每批回答上限",
  reasoning_effort: "推理档位",
  max_batch_input_tokens: "每批代码上限",
  connect_timeout_seconds: "连接等待时间",
  read_timeout_seconds: "回答等待时间",
  write_timeout_seconds: "发送等待时间",
  pool_timeout_seconds: "连接排队时间",
  max_request_bytes: "请求保护上限",
  max_response_bytes: "响应保护上限",
  input_usd_per_million: "输入统计单价",
  output_usd_per_million: "输出统计单价",
  cache_read_usd_per_million: "缓存读取统计单价",
  cache_write_usd_per_million: "缓存写入统计单价",
  test_status: "测试状态",
};

function ProviderStatus({ settings }: { settings: AiProviderSettings }) {
  return (
    <div className="settings-status-line">
      {settings.active && <span className="settings-active-badge">正在使用</span>}
      <span className={`settings-test-badge is-${settings.test_status}`}>
        <span className="settings-status-dot" />
        {testStatusLabels[settings.test_status]}
      </span>
      <span className="settings-key-state">
        {settings.api_key_configured
          ? `密钥 ${settings.api_key_mask}`
          : "未保存密钥"}
      </span>
    </div>
  );
}

export default function SettingsPage({
  user,
  onBack,
  onSignedOut,
}: SettingsPageProps) {
  const [settings, setSettings] = useState<AiSettings | null>(null);
  const [policyDraft, setPolicyDraft] = useState<ReviewPolicyDraft | null>(null);
  const [policyMessage, setPolicyMessage] = useState("");
  const [policyMessageKind, setPolicyMessageKind] = useState<"success" | "error">("success");
  const [audits, setAudits] = useState<ConfigurationAudit[]>([]);
  const [selectedProvider, setSelectedProvider] = useState<AiProvider>("openai");
  const [drafts, setDrafts] = useState<Partial<Record<AiProvider, ProviderDraft>>>({});
  const [message, setMessage] = useState("");
  const [messageKind, setMessageKind] = useState<"success" | "error">("success");
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState("");
  const [agentRefreshRequest, setAgentRefreshRequest] = useState(0);
  const [showApiKey, setShowApiKey] = useState<Record<AiProvider, boolean>>({
    openai: false,
    anthropic: false,
  });
  const providerInitialized = useRef(false);

  const applySettings = useCallback((next: AiSettings) => {
    const normalized = normalizeAiSettings(next);
    setSettings(normalized);
    setPolicyDraft(reviewPolicyDraft(normalized));
    setDrafts(
      Object.fromEntries(
        normalized.providers.map((provider) => [provider.provider, providerDraft(provider)]),
      ) as Record<AiProvider, ProviderDraft>,
    );
    if (!providerInitialized.current) {
      setSelectedProvider(normalized.active_provider ?? "openai");
      providerInitialized.current = true;
    }
  }, []);

  const refresh = useCallback(
    async (showLoading = true, signal?: AbortSignal) => {
      if (showLoading) setLoading(true);
      try {
        const [nextSettings, auditResponse] = await Promise.all([
          api.aiSettings(signal),
          api.configurationAudits(signal),
        ]);
        applySettings(nextSettings);
        setAudits(auditResponse.items);
      } catch (error) {
        if (signal?.aborted) return;
        if (error instanceof ApiError && error.status === 401) {
          onSignedOut("登录状态已失效，请重新登录");
          return;
        }
        setMessageKind("error");
        setMessage(errorMessage(error));
      } finally {
        if (showLoading && !signal?.aborted) setLoading(false);
      }
    },
    [applySettings, onSignedOut],
  );

  useEffect(() => {
    const controller = new AbortController();
    void refresh(true, controller.signal);
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    if (!message || messageKind !== "success") return undefined;
    const timer = window.setTimeout(() => setMessage(""), 4000);
    return () => window.clearTimeout(timer);
  }, [message, messageKind]);

  function refreshAllSettings() {
    setAgentRefreshRequest((current) => current + 1);
    return refresh();
  }

  const selectedSettings = useMemo(
    () => settings?.providers.find((item) => item.provider === selectedProvider),
    [selectedProvider, settings],
  );
  const draft = drafts[selectedProvider];

  function updateDraft<K extends keyof ProviderDraft>(field: K, value: ProviderDraft[K]) {
    setDrafts((current) => ({
      ...current,
      [selectedProvider]: { ...current[selectedProvider]!, [field]: value },
    }));
  }

  function updateContextWindow(value: string) {
    setDrafts((current) => {
      const selected = current[selectedProvider]!;
      const outputLimit = Math.max(256, Number(value) - 4_096);
      const currentOutput = Number(selected.maxOutputTokens);
      return {
        ...current,
        [selectedProvider]: {
          ...selected,
          contextWindowTokens: value,
          maxOutputTokens: String(Math.min(currentOutput, outputLimit)),
        },
      };
    });
  }

  function showSuccess(text: string) {
    setMessageKind("success");
    setMessage(text);
  }

  async function handleAction(
    action: string,
    operation: () => Promise<AiSettings>,
    successText: string,
    refreshAfterError = false,
  ) {
    setBusyAction(action);
    setMessage("");
    try {
      const next = await operation();
      applySettings(next);
      const auditResponse = await api.configurationAudits();
      setAudits(auditResponse.items);
      showSuccess(successText);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setMessageKind("error");
      setMessage(errorMessage(error));
      if (refreshAfterError || (error instanceof ApiError && error.status === 409)) {
        await refresh(false);
      }
    } finally {
      setBusyAction("");
    }
  }

  async function saveProvider(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settings || !draft) return;
    try {
      if (draft.useCustomEndpoint && !draft.apiBaseUrl.trim()) {
        throw new Error("选择中转站后，请填写中转站提供的 API 地址");
      }
      const payload: AiProviderUpdate = {
        expected_revision: settings.revision,
        model: draft.model.trim(),
        api_protocol: draft.apiProtocol,
        api_base_url: draft.useCustomEndpoint ? draft.apiBaseUrl.trim() : null,
        api_key: draft.apiKey.trim() || null,
        clear_api_key: draft.clearApiKey,
        reasoning_effort: draft.reasoningEffort,
        context_window_tokens: requiredNumber(draft.contextWindowTokens, "上下文窗口"),
        max_output_tokens: requiredNumber(draft.maxOutputTokens, "每批回答上限"),
        max_batch_input_tokens: requiredNumber(draft.maxBatchInputTokens, "每批代码上限"),
        connect_timeout_seconds: requiredNumber(draft.connectTimeoutSeconds, "连接等待时间"),
        read_timeout_seconds: requiredNumber(draft.readTimeoutSeconds, "回答等待时间"),
        write_timeout_seconds: requiredNumber(draft.writeTimeoutSeconds, "发送等待时间"),
        pool_timeout_seconds: requiredNumber(draft.poolTimeoutSeconds, "连接排队时间"),
        max_request_bytes: requiredNumber(draft.maxRequestBytes, "请求保护上限"),
        max_response_bytes: requiredNumber(draft.maxResponseBytes, "响应保护上限"),
        input_usd_per_million: optionalDecimal(draft.inputPrice),
        output_usd_per_million: optionalDecimal(draft.outputPrice),
        cache_read_usd_per_million: optionalDecimal(draft.cacheReadPrice),
        cache_write_usd_per_million: optionalDecimal(draft.cacheWritePrice),
      };
      await handleAction(
        `save-${selectedProvider}`,
        () => api.updateAiProvider(selectedProvider, payload),
        `${providerShortLabels[selectedProvider]} 配置已保存，下一步请测试连接`,
      );
    } catch (error) {
      setMessageKind("error");
      setMessage(errorMessage(error));
    }
  }

  async function testProvider() {
    if (!settings) return;
    await handleAction(
      `test-${selectedProvider}`,
      () => api.testAiProvider(selectedProvider, settings.revision),
      `${providerShortLabels[selectedProvider]} 连接正常，现在可以启用`,
      true,
    );
  }

  async function activateProvider() {
    if (!settings) return;
    await handleAction(
      `activate-${selectedProvider}`,
      () => api.activateAiProvider(selectedProvider, settings.revision),
      `${providerShortLabels[selectedProvider]} 已启用，后续审查会使用这套配置`,
    );
  }

  function updatePolicyDraft<K extends keyof ReviewPolicyDraft>(
    field: K,
    value: ReviewPolicyDraft[K],
  ) {
    setPolicyDraft((current) => current ? { ...current, [field]: value } : current);
  }

  async function saveReviewPolicy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settings || !policyDraft) return;
    setBusyAction("save-policy");
    setPolicyMessage("");
    try {
      const payload: ReviewPolicyUpdate = {
        expected_revision: settings.revision,
        max_units: requiredInteger(policyDraft.maxUnits, "最多审查单元"),
        max_scope_depth: requiredInteger(policyDraft.maxScopeDepth, "规则目录深度"),
        max_unit_input_bytes: requiredInteger(
          inputToBytes(policyDraft.maxUnitInputKib, KIB),
          "单文件输入上限",
        ),
        max_total_input_bytes: requiredInteger(
          inputToBytes(policyDraft.maxTotalInputMib, MIB),
          "总输入上限",
        ),
        max_model_http_calls: requiredInteger(
          policyDraft.maxModelHttpCalls,
          "模型请求次数",
        ),
        max_model_input_tokens: requiredInteger(
          policyDraft.maxModelInputTokens,
          "模型输入 Token 上限",
        ),
        max_model_output_tokens: requiredInteger(
          policyDraft.maxModelOutputTokens,
          "模型输出 Token 上限",
        ),
        max_model_cost_microusd: optionalUsdToMicrousd(
          policyDraft.maxModelCostUsd,
        ),
        max_model_duration_seconds: requiredInteger(
          policyDraft.maxModelDurationSeconds,
          "模型总耗时上限",
        ),
      };
      applySettings(await api.updateReviewPolicy(payload));
      setAudits((await api.configurationAudits()).items);
      setPolicyMessageKind("success");
      setPolicyMessage("审查范围和单次任务硬预算已保存");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setPolicyMessageKind("error");
      setPolicyMessage(errorMessage(error));
      if (error instanceof ApiError && error.status === 409) await refresh(false);
    } finally {
      setBusyAction("");
    }
  }

  async function logout() {
    try {
      await api.logout();
    } finally {
      onSignedOut();
    }
  }

  const providerDirty = Boolean(
    selectedSettings && draft && providerHasChanges(selectedSettings, draft),
  );
  const policyDirty = Boolean(
    settings && policyDraft && reviewPolicyHasChanges(settings, policyDraft),
  );
  const contextTokens = Number(draft?.contextWindowTokens ?? 0);
  const outputTokens = Number(draft?.maxOutputTokens ?? 0);
  const maxBatchInputTokens = Number(draft?.maxBatchInputTokens ?? 0);
  const maxRequestBytes = Number(draft?.maxRequestBytes ?? 0);
  const contextInputBudgetTokens = contextTokens
    - outputTokens
    - Math.max(4_096, Math.floor(contextTokens / 20));
  const requestInputBudgetTokens = Math.floor(
    Math.max(
      0,
      maxRequestBytes
        - Math.max(16 * 1024, Math.floor(maxRequestBytes / 10))
        - 4 * 1024,
    ) / 2,
  );
  const inputBudgetTokens = Math.max(
    0,
    Math.min(
      contextInputBudgetTokens,
      maxBatchInputTokens,
      requestInputBudgetTokens,
    ),
  );

  return (
    <div className="settings-page-layout">
      <header className="settings-navbar">
        <div className="settings-nav-start">
          <button type="button" className="settings-icon-btn" onClick={onBack} title="返回审查控制台" aria-label="返回审查控制台">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
          </button>
          <div className="settings-heading-lockup">
            <div className="settings-heading-icon">
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56V21h-4v-.08A1.7 1.7 0 0 0 8.96 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.56-1.03H3v-4h.08A1.7 1.7 0 0 0 4.6 8.96a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 8.96 4.6 1.7 1.7 0 0 0 10 3.08V3h4v.08a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9c.14.6.67 1.02 1.29 1.03H21v4h-.31c-.62 0-1.15.42-1.29 1.03Z" /></svg>
            </div>
            <div><strong>AI 设置</strong><span>OpenReviewer</span></div>
          </div>
        </div>
        <div className="settings-nav-end">
          <button type="button" className="settings-icon-btn" onClick={() => void refreshAllSettings()} disabled={loading || Boolean(busyAction)} title="刷新设置" aria-label="刷新设置">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M20 6v6h-6" /><path d="M4 18v-6h6" /><path d="M18.5 9A7 7 0 0 0 6 5.5L4 8" /><path d="M5.5 15A7 7 0 0 0 18 18.5l2-2.5" /></svg>
          </button>
          <div className="settings-user"><span>{user.username.slice(0, 1).toUpperCase()}</span><strong>{user.username}</strong></div>
          <button type="button" className="settings-icon-btn" onClick={logout} title="退出登录" aria-label="退出登录">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="m16 17 5-5-5-5" /><path d="M21 12H9" /></svg>
          </button>
        </div>
      </header>

      <main className="settings-main">
        <div className="settings-title-row">
          <div>
            <h1>模型服务</h1>
            <p className="settings-intro">支持官方接口和兼容中转站，日常只需配置地址、模型和密钥。</p>
            <div className="settings-meta-line">
              <span>配置版本 {settings?.revision ?? "--"}</span>
              <span>更新于 {formatDate(settings?.updated_at ?? null)}</span>
              <span>修改人 {settings?.updated_by ?? "--"}</span>
            </div>
          </div>
          <div className={`settings-runtime-state ${settings?.active_provider ? "is-active" : "is-idle"}`}>
            <span className="settings-status-dot" />
            {settings?.active_provider ? `${providerShortLabels[settings.active_provider]} 运行中` : "还未启用模型"}
          </div>
        </div>

        {message && (loading || !settings || !selectedSettings || !draft) && <div className={`settings-message is-${messageKind}`} role="alert">{message}</div>}

        {loading || !settings || !selectedSettings || !draft ? (
          <div className="settings-loading">正在读取设置...</div>
        ) : (
          <>
            <section className="settings-provider-workspace">
              <aside className="settings-provider-nav" aria-label="接口类型">
                <span className="settings-nav-label">接口类型</span>
                {settings.providers.map((provider) => (
                  <button key={provider.provider} type="button" className={selectedProvider === provider.provider ? "is-selected" : ""} onClick={() => setSelectedProvider(provider.provider)}>
                    <span className={`provider-mark is-${provider.provider}`}>{provider.provider === "openai" ? "O" : "A"}</span>
                    <span><strong>{providerShortLabels[provider.provider]}</strong><small>{provider.active ? "正在使用" : testStatusLabels[provider.test_status]}</small></span>
                    {provider.active && <i className="provider-active-dot" />}
                  </button>
                ))}
                <div className="settings-provider-tip">选择中转站使用的兼容格式，不代表必须向对应官方购买。</div>
              </aside>

              <div className="settings-provider-content">
                <div className="settings-section-heading">
                  <div>
                    <span className="settings-eyebrow">MODEL CONNECTION</span>
                    <h2>{providerLabels[selectedProvider]}</h2>
                    <ProviderStatus settings={selectedSettings} />
                  </div>
                  {providerDirty && <span className="settings-unsaved-badge">有修改待保存</span>}
                </div>

                <form onSubmit={saveProvider}>
                  <fieldset className="settings-fieldset settings-quick-fieldset" disabled={Boolean(busyAction)}>
                    <legend>快速配置</legend>
                    <div className="settings-config-block">
                      <div className="settings-block-heading">
                        <span className="settings-step-number">1</span>
                        <div><strong>选择服务地址</strong><small>中转站会给你一个以 https:// 开头的 API 地址</small></div>
                      </div>
                      <div className="settings-source-control" role="group" aria-label="服务地址类型">
                        <button type="button" className={!draft.useCustomEndpoint ? "is-selected" : ""} aria-pressed={!draft.useCustomEndpoint} onClick={() => updateDraft("useCustomEndpoint", false)}>
                          <span>官方直连</span><small>{providerShortLabels[selectedProvider]} 官方地址</small>
                        </button>
                        <button type="button" className={draft.useCustomEndpoint ? "is-selected" : ""} aria-pressed={draft.useCustomEndpoint} onClick={() => updateDraft("useCustomEndpoint", true)}>
                          <span>中转站 / 自定义</span><small>兼容接口，通常更灵活</small>
                        </button>
                      </div>
                      {draft.useCustomEndpoint ? (
                        <label className="settings-wide-field">
                          <span>API 地址</span>
                          <input id={`settings-${selectedProvider}-api-base-url`} name={`settings-${selectedProvider}-api-base-url`} type="url" inputMode="url" required maxLength={500} value={draft.apiBaseUrl} onChange={(event) => updateDraft("apiBaseUrl", event.target.value)} placeholder="https://你的中转站地址/v1" autoComplete="url" />
                          <small>直接粘贴中转站文档里的 Base URL；结尾带不带 /v1 都可以。</small>
                        </label>
                      ) : (
                        <div className="settings-endpoint-preview">
                          <span className="settings-endpoint-lock"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="11" width="18" height="10" rx="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></svg></span>
                          <div><strong>{officialEndpoints[selectedProvider]}</strong><small>使用官方安全地址</small></div>
                        </div>
                      )}
                    </div>

                    {selectedProvider === "openai" && (
                      <div className="settings-config-block">
                        <div className="settings-block-heading is-compact">
                          <span className="settings-step-number">2</span>
                          <div><strong>选择接口格式</strong><small>不确定时，中转站优先选择“通用兼容”</small></div>
                        </div>
                        <div className="settings-protocol-options" role="group" aria-label="OpenAI 接口格式">
                          {(Object.keys(openAiProtocolLabels) as Array<keyof typeof openAiProtocolLabels>).map((protocol) => (
                            <button key={protocol} type="button" className={draft.apiProtocol === protocol ? "is-selected" : ""} aria-pressed={draft.apiProtocol === protocol} onClick={() => updateDraft("apiProtocol", protocol)}>
                              <span>{openAiProtocolLabels[protocol].title}</span><small>{openAiProtocolLabels[protocol].note}</small>
                            </button>
                          ))}
                        </div>
                      </div>
                    )}

                    <div className="settings-config-block">
                      <div className="settings-block-heading is-compact">
                        <span className="settings-step-number">{selectedProvider === "openai" ? "3" : "2"}</span>
                        <div><strong>填写模型和密钥</strong><small>模型 ID 必须与服务商后台显示的名称完全一致</small></div>
                      </div>
                      <div className="settings-form-grid settings-form-grid-primary">
                        <label>
                          <span>模型 ID</span>
                          <input id={`settings-${selectedProvider}-model`} name={`settings-${selectedProvider}-model`} required maxLength={200} value={draft.model} onChange={(event) => updateDraft("model", event.target.value)} placeholder={modelPlaceholders[selectedProvider]} autoComplete="off" />
                          <small>请从官方或中转站的模型列表复制，不要凭感觉填写。</small>
                        </label>
                        <label>
                          <span>API Key</span>
                          <div className="settings-secret-input">
                            <input id={`settings-${selectedProvider}-api-key`} name={`settings-${selectedProvider}-api-key`} type={showApiKey[selectedProvider] ? "text" : "password"} value={draft.apiKey} onChange={(event) => updateDraft("apiKey", event.target.value)} placeholder={selectedSettings.api_key_configured ? `已安全保存 ${selectedSettings.api_key_mask}` : "粘贴服务商提供的 API Key"} autoComplete="new-password" disabled={draft.clearApiKey} />
                            <button type="button" onClick={() => setShowApiKey((current) => ({ ...current, [selectedProvider]: !current[selectedProvider] }))} title={showApiKey[selectedProvider] ? "隐藏 API Key" : "显示 API Key"} aria-label={showApiKey[selectedProvider] ? "隐藏 API Key" : "显示 API Key"}>
                              {showApiKey[selectedProvider] ? (
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 3l18 18"/><path d="M10.6 10.6a2 2 0 0 0 2.8 2.8"/><path d="M9.9 4.2A10.4 10.4 0 0 1 12 4c5 0 9 5 9 8a9.6 9.6 0 0 1-2 3.5"/><path d="M6.6 6.6C4.4 8 3 10.2 3 12c0 3 4 8 9 8 1.2 0 2.3-.3 3.3-.8"/></svg>
                              ) : (
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>
                              )}
                            </button>
                          </div>
                          <small>{selectedSettings.api_key_configured ? "留空会保留原密钥；填入新值才会替换。" : "密钥只会加密保存，之后不再显示完整内容。"}</small>
                        </label>
                      </div>
                    </div>

                    {selectedSettings.api_key_configured && (
                      <details className="settings-key-management">
                        <summary>密钥管理</summary>
                        <label className="settings-checkbox-row"><input id={`settings-${selectedProvider}-clear-api-key`} name={`settings-${selectedProvider}-clear-api-key`} type="checkbox" checked={draft.clearApiKey} onChange={(event) => updateDraft("clearApiKey", event.target.checked)} /><span>保存时删除现有 API Key</span></label>
                      </details>
                    )}
                  </fieldset>

                  <details className="settings-disclosure">
                    <summary>
                      <span><strong>代码量与推理</strong><small>推荐保持“自动 + 每批 64K”</small></span>
                      <span className="settings-summary-value">总 {formatTokens(draft.contextWindowTokens)} · 每批 {formatTokens(inputBudgetTokens)}</span>
                    </summary>
                    <fieldset disabled={Boolean(busyAction)}>
                      <div className="settings-reasoning-section">
                        <div className="settings-inline-heading">
                          <strong>推理档位</strong>
                          <small>不同模型支持的档位不同；“自动”不会向中转站附加推理参数。</small>
                        </div>
                        <div className="settings-preset-control settings-reasoning-options" role="group" aria-label="模型推理档位">
                          {(Object.keys(reasoningEffortLabels) as AiReasoningEffort[]).map((effort) => (
                            <button key={effort} type="button" className={draft.reasoningEffort === effort ? "is-selected" : ""} aria-pressed={draft.reasoningEffort === effort} onClick={() => updateDraft("reasoningEffort", effort)}>
                              <strong>{reasoningEffortLabels[effort].title}</strong>
                              <small>{reasoningEffortLabels[effort].note}</small>
                            </button>
                          ))}
                        </div>
                      </div>
                      <div className="settings-form-grid settings-form-grid-compact settings-context-grid">
                        <SelectField name="context-window-tokens" label="模型总容量" help="按模型或中转站说明填写；1M 是总容量，不是每次都发送 1M。" value={draft.contextWindowTokens} options={contextWindowOptions(draft.contextWindowTokens)} onChange={updateContextWindow} />
                        <SelectField name="max-batch-input-tokens" label="每批代码量" help="推荐 64K；出现 524 或长时间超时时可降到 32K。" value={draft.maxBatchInputTokens} options={batchInputOptions(draft.contextWindowTokens, draft.maxBatchInputTokens)} onChange={(value) => updateDraft("maxBatchInputTokens", value)} />
                        <SelectField name="max-output-tokens" label="每批回答上限" help="只限制模型每批返回的审查结果长度。" value={draft.maxOutputTokens} options={outputTokenOptions(draft.contextWindowTokens, draft.maxOutputTokens)} onChange={(value) => updateDraft("maxOutputTokens", value)} />
                        <NumberField name="read-timeout-seconds" label="最多等待回答" value={draft.readTimeoutSeconds} min="10" max="3600" step="1" suffix="秒" onChange={(value) => updateDraft("readTimeoutSeconds", value)} />
                      </div>
                      <div className="settings-info-band settings-context-budget"><strong>实际单批输入最多约 {formatTokens(inputBudgetTokens)} Token</strong><span>提交再大也会继续审查，超出的代码自动切到下一批</span></div>
                      <details className="settings-nested-disclosure">
                        <summary>传输与网络高级设置</summary>
                        <div className="settings-form-grid settings-form-grid-compact">
                          <NumberField name="max-request-mib" label="请求保护上限" value={bytesToInput(draft.maxRequestBytes, MIB)} min="0.0625" max="10" step="0.0625" suffix="MiB" onChange={(value) => updateDraft("maxRequestBytes", inputToBytes(value, MIB))} />
                          <NumberField name="max-response-mib" label="响应保护上限" value={bytesToInput(draft.maxResponseBytes, MIB)} min="0.0625" max="10" step="0.0625" suffix="MiB" onChange={(value) => updateDraft("maxResponseBytes", inputToBytes(value, MIB))} />
                          <NumberField name="connect-timeout-seconds" label="建立连接" value={draft.connectTimeoutSeconds} min="0.1" max="3600" step="0.1" suffix="秒" onChange={(value) => updateDraft("connectTimeoutSeconds", value)} />
                          <NumberField name="write-timeout-seconds" label="发送请求" value={draft.writeTimeoutSeconds} min="0.1" max="3600" step="0.1" suffix="秒" onChange={(value) => updateDraft("writeTimeoutSeconds", value)} />
                          <NumberField name="pool-timeout-seconds" label="等待空闲连接" value={draft.poolTimeoutSeconds} min="0.1" max="3600" step="0.1" suffix="秒" onChange={(value) => updateDraft("poolTimeoutSeconds", value)} />
                        </div>
                      </details>
                    </fieldset>
                  </details>

                  <details className="settings-disclosure">
                    <summary>
                      <span><strong>费用统计（可选）</strong><small>按你的中转站账单填写，也可以全部留空</small></span>
                      <span className="settings-summary-value is-neutral">不影响实际扣费</span>
                    </summary>
                    <fieldset disabled={Boolean(busyAction)}>
                      <div className="settings-info-band">这里只估算审查记录的成本，不会替你充值、扣费或改变服务商价格。单位统一为美元 / 100 万 Token。</div>
                      <div className="settings-form-grid settings-form-grid-pricing">
                        <NumberField name="input-price" label="输入单价" value={draft.inputPrice} min="0" max="1000000" step="0.000001" required={false} suffix="$" onChange={(value) => updateDraft("inputPrice", value)} />
                        <NumberField name="output-price" label="输出单价" value={draft.outputPrice} min="0" max="1000000" step="0.000001" required={false} suffix="$" onChange={(value) => updateDraft("outputPrice", value)} />
                        <NumberField name="cache-read-price" label="缓存读取单价" value={draft.cacheReadPrice} min="0" max="1000000" step="0.000001" required={false} suffix="$" onChange={(value) => updateDraft("cacheReadPrice", value)} />
                        <NumberField name="cache-write-price" label="缓存写入单价" value={draft.cacheWritePrice} min="0" max="1000000" step="0.000001" required={false} suffix="$" onChange={(value) => updateDraft("cacheWritePrice", value)} />
                      </div>
                    </fieldset>
                  </details>

                  <div className="settings-activation-flow" aria-label="启用流程">
                    <div className={selectedSettings.configured && !providerDirty ? "is-done" : "is-current"}><span>1</span><strong>保存</strong><small>{providerDirty ? "等待保存" : selectedSettings.configured ? "已保存" : "填写配置"}</small></div>
                    <i />
                    <div className={selectedSettings.test_status === "succeeded" && !providerDirty ? "is-done" : ""}><span>2</span><strong>测试</strong><small>{providerDirty ? "先保存" : testStatusLabels[selectedSettings.test_status]}</small></div>
                    <i />
                    <div className={selectedSettings.active ? "is-done" : ""}><span>3</span><strong>启用</strong><small>{selectedSettings.active ? "运行中" : "测试后启用"}</small></div>
                  </div>

                  <div className="settings-action-bar">
                    <button className="settings-primary-btn" type="submit" disabled={Boolean(busyAction) || (!providerDirty && selectedSettings.configured)}><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/></svg>{busyAction === `save-${selectedProvider}` ? "保存中..." : "保存配置"}</button>
                    <button className="settings-secondary-btn" type="button" onClick={() => void testProvider()} disabled={!selectedSettings.configured || !selectedSettings.api_key_configured || providerDirty || Boolean(busyAction)} title={providerDirty ? "请先保存当前修改" : "测试当前已保存配置"}><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="m13 2-9 12h7l-1 8 9-12h-7l1-8Z"/></svg>{busyAction === `test-${selectedProvider}` ? "测试中..." : "测试连接"}</button>
                    <button className="settings-secondary-btn is-activate" type="button" onClick={() => void activateProvider()} disabled={selectedSettings.test_status !== "succeeded" || selectedSettings.active || providerDirty || Boolean(busyAction)}><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="m5 12 4 4L19 6"/></svg>{selectedSettings.active ? "已启用" : "启用这套配置"}</button>
                    {message && <div className={`settings-inline-feedback is-${messageKind}`} role="status">{message}</div>}
                  </div>
                </form>
              </div>
            </section>

            {policyDraft && (
              <section className="settings-policy-section">
              <form onSubmit={saveReviewPolicy}>
                <div className="settings-section-heading settings-policy-heading">
                  <div>
                    <span className="settings-eyebrow">REVIEW GUARDRAILS</span>
                    <h2>审查范围与硬预算</h2>
                    <p>这些上限会固化到新任务，重试不能绕过；已有任务保持原配置。</p>
                  </div>
                  {policyDirty && <span className="settings-unsaved-badge">有修改待保存</span>}
                </div>
                <fieldset disabled={Boolean(busyAction)}>
                  <div className="settings-policy-group">
                    <div className="settings-inline-heading">
                      <strong>代码范围</strong>
                      <small>控制一次审查最多接收多少文件、规则层级和文本体积。</small>
                    </div>
                    <div className="settings-form-grid settings-policy-grid">
                      <NumberField name="policy-max-units" label="最多审查单元" value={policyDraft.maxUnits} min="1" max="3000" onChange={(value) => updatePolicyDraft("maxUnits", value)} />
                      <NumberField name="policy-scope-depth" label="规则目录深度" value={policyDraft.maxScopeDepth} min="1" max="64" onChange={(value) => updatePolicyDraft("maxScopeDepth", value)} />
                      <NumberField name="policy-unit-kib" label="单文件输入上限" value={policyDraft.maxUnitInputKib} min="4" max="10240" suffix="KiB" onChange={(value) => updatePolicyDraft("maxUnitInputKib", value)} />
                      <NumberField name="policy-total-mib" label="总输入上限" value={policyDraft.maxTotalInputMib} min="0.00390625" max="100" step="0.00390625" suffix="MiB" onChange={(value) => updatePolicyDraft("maxTotalInputMib", value)} />
                    </div>
                  </div>
                  <div className="settings-policy-group">
                    <div className="settings-inline-heading">
                      <strong>每个审查任务的模型预算</strong>
                      <small>模型请求发送前按最坏情况预留，响应后再按可确认的实际用量结算。</small>
                    </div>
                    <div className="settings-form-grid settings-policy-grid">
                      <NumberField name="policy-http-calls" label="HTTP 请求次数" value={policyDraft.maxModelHttpCalls} min="1" max="10000" onChange={(value) => updatePolicyDraft("maxModelHttpCalls", value)} />
                      <NumberField name="policy-input-tokens" label="输入 Token" value={policyDraft.maxModelInputTokens} min="1000" max="1000000000" onChange={(value) => updatePolicyDraft("maxModelInputTokens", value)} />
                      <NumberField name="policy-output-tokens" label="输出 Token" value={policyDraft.maxModelOutputTokens} min="256" max="100000000" onChange={(value) => updatePolicyDraft("maxModelOutputTokens", value)} />
                      <NumberField name="policy-duration" label="总耗时" value={policyDraft.maxModelDurationSeconds} min="30" max="86400" suffix="秒" onChange={(value) => updatePolicyDraft("maxModelDurationSeconds", value)} />
                      <NumberField name="policy-cost-usd" label="预估费用上限" value={policyDraft.maxModelCostUsd} min="0.000001" max="1000000" step="0.000001" required={false} suffix="$" onChange={(value) => updatePolicyDraft("maxModelCostUsd", value)} />
                    </div>
                    <div className="settings-info-band">
                      <strong>{policyDraft.maxModelCostUsd ? "费用硬上限已启用" : "费用硬上限未启用"}</strong>
                      <span>{policyDraft.maxModelCostUsd ? "启用后，当前模型必须填写完整价格，否则任务会在调用前暂停。" : "留空时仍会强制限制请求次数、Token 和总耗时。"}</span>
                    </div>
                  </div>
                </fieldset>
                <div className="settings-action-bar settings-policy-action">
                  <button className="settings-primary-btn" type="submit" disabled={Boolean(busyAction) || !policyDirty}>
                    {busyAction === "save-policy" ? "保存中..." : "保存审查策略"}
                  </button>
                  {policyMessage && <div className={`settings-inline-feedback is-${policyMessageKind}`} role="status">{policyMessage}</div>}
                </div>
              </form>
              </section>
            )}

            <details className="settings-audit-section">
              <summary><span><strong>配置变更记录</strong><small>最近 {audits.length} 条，不包含密钥内容</small></span><span>展开查看</span></summary>
              <div className="settings-audit-table-wrap">
                <table className="settings-audit-table">
                  <thead><tr><th>版本</th><th>操作</th><th>变更内容</th><th>管理员</th><th>时间</th></tr></thead>
                  <tbody>{audits.map((audit) => <tr key={audit.revision}><td className="code-font">r{audit.revision}</td><td>{auditActionLabels[audit.action] ?? audit.action}</td><td>{audit.changed_fields.map((field) => fieldLabels[field] ?? field).join("、")}</td><td>{audit.actor}</td><td>{formatDate(audit.created_at)}</td></tr>)}</tbody>
                </table>
                {audits.length === 0 && <div className="settings-empty-audit">暂无配置变更</div>}
              </div>
            </details>
          </>
        )}
        <AgentSettingsPanel refreshRequest={agentRefreshRequest} onSignedOut={onSignedOut} />
      </main>
    </div>
  );
}

interface NumberFieldProps {
  name: string;
  label: string;
  value: string;
  min: string;
  max: string;
  step?: string;
  suffix?: string;
  required?: boolean;
  onChange: (value: string) => void;
}

function NumberField({ name, label, value, min, max, step = "1", suffix, required = true, onChange }: NumberFieldProps) {
  return (
    <label>
      <span>{label}</span>
      <span className="settings-input-with-suffix">
        <input id={`settings-${name}`} name={`settings-${name}`} type="number" value={value} min={min} max={max} step={step} required={required} onChange={(event) => onChange(event.target.value)} />
        {suffix && <i>{suffix}</i>}
      </span>
    </label>
  );
}

interface SelectFieldProps {
  name: string;
  label: string;
  help?: string;
  value: string;
  options: Array<[string, string]>;
  onChange: (value: string) => void;
}

function SelectField({ name, label, help, value, options, onChange }: SelectFieldProps) {
  const knownValue = options.some(([option]) => option === value);
  return (
    <label>
      <span>{label}</span>
      <select id={`settings-${name}`} name={`settings-${name}`} value={value} onChange={(event) => onChange(event.target.value)}>
        {!knownValue && <option value={value}>自定义（{value} Token）</option>}
        {options.map(([option, text]) => <option key={option} value={option}>{text}</option>)}
      </select>
      {help && <small>{help}</small>}
    </label>
  );
}
