import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { api, ApiError } from "./api";
import type {
  AiApiProtocol,
  AiProvider,
  AiProviderSettings,
  AiProviderUpdate,
  AiSettings,
  AuthUser,
  ConfigurationAudit,
} from "./types";
import { errorMessage, formatDate } from "./utils";

interface SettingsPageProps {
  user: AuthUser;
  onBack: () => void;
  onSignedOut: (message?: string) => void;
}

interface ProviderDraft {
  model: string;
  apiProtocol: AiApiProtocol;
  useCustomEndpoint: boolean;
  apiBaseUrl: string;
  apiKey: string;
  clearApiKey: boolean;
  contextWindowTokens: string;
  maxOutputTokens: string;
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

const KIB = 1024;
const MIB = 1024 * KIB;

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

const testStatusLabels = {
  untested: "等待测试",
  succeeded: "连接正常",
  failed: "测试失败",
} as const;

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

function providerDraft(settings: AiProviderSettings): ProviderDraft {
  return {
    model: settings.model,
    apiProtocol: settings.api_protocol,
    useCustomEndpoint: Boolean(settings.api_base_url),
    apiBaseUrl: settings.api_base_url ?? "",
    apiKey: "",
    clearApiKey: false,
    contextWindowTokens: String(settings.context_window_tokens),
    maxOutputTokens: String(settings.max_output_tokens),
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

function requiredNumber(value: string, label: string): number {
  if (!value.trim()) throw new Error(`请填写${label}`);
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`${label}必须是数字`);
  return parsed;
}

function optionalDecimal(value: string): string | null {
  const normalized = value.trim();
  return normalized ? normalized : null;
}

function bytesToInput(value: string, unit: number): string {
  if (!value) return "";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? String(parsed / unit) : "";
}

function inputToBytes(value: string, unit: number): string {
  if (!value) return "";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? String(Math.round(parsed * unit)) : "";
}

function formatTokens(value: string | number): string {
  const tokens = Number(value);
  if (!Number.isFinite(tokens)) return "--";
  if (tokens >= 1_000_000) return `${Number((tokens / 1_000_000).toFixed(2))}M`;
  if (tokens >= 1_000) return `${Number((tokens / 1_000).toFixed(0))}K`;
  return String(tokens);
}

function contextWindowOptions(current: string): Array<[string, string]> {
  const presets: Array<[string, string]> = [
    ["8192", "8K Token"],
    ["16384", "16K Token"],
    ["32768", "32K Token"],
    ["65536", "64K Token"],
    ["128000", "128K Token"],
    ["200000", "200K Token"],
    ["256000", "256K Token"],
    ["384000", "384K Token"],
    ["1000000", "1M Token（DeepSeek V4）"],
    ["2000000", "2M Token"],
    ["4000000", "4M Token"],
  ];
  if (presets.some(([value]) => value === current)) return presets;
  return [[current, `${formatTokens(current)} Token（当前值）`], ...presets];
}

function outputTokenOptions(context: string, current: string): Array<[string, string]> {
  const contextTokens = Number(context);
  const presets: Array<[string, string]> = [
    ["2048", "简短（2K Token）"],
    ["4096", "日常（4K Token）"],
    ["8192", "标准（8K Token）"],
    ["16384", "详细（16K Token）"],
    ["32768", "超长（32K Token）"],
    ["65536", "极长（64K Token）"],
    ["131072", "最大（128K Token）"],
  ].filter(([value]) => Number(value) <= contextTokens - 4_096) as Array<[string, string]>;
  if (presets.some(([value]) => value === current)) return presets;
  return [[current, `${formatTokens(current)} Token（当前值）`], ...presets];
}

function providerHasChanges(
  settings: AiProviderSettings,
  draft: ProviderDraft,
): boolean {
  const effectiveBaseUrl = draft.useCustomEndpoint
    ? draft.apiBaseUrl.trim() || null
    : null;
  return (
    draft.model.trim() !== settings.model ||
    draft.apiProtocol !== settings.api_protocol ||
    effectiveBaseUrl !== settings.api_base_url ||
    Boolean(draft.apiKey.trim()) ||
    draft.clearApiKey ||
    Number(draft.contextWindowTokens) !== settings.context_window_tokens ||
    Number(draft.maxOutputTokens) !== settings.max_output_tokens ||
    Number(draft.connectTimeoutSeconds) !== settings.connect_timeout_seconds ||
    Number(draft.readTimeoutSeconds) !== settings.read_timeout_seconds ||
    Number(draft.writeTimeoutSeconds) !== settings.write_timeout_seconds ||
    Number(draft.poolTimeoutSeconds) !== settings.pool_timeout_seconds ||
    Number(draft.maxRequestBytes) !== settings.max_request_bytes ||
    Number(draft.maxResponseBytes) !== settings.max_response_bytes ||
    draft.inputPrice !== (settings.input_usd_per_million ?? "") ||
    draft.outputPrice !== (settings.output_usd_per_million ?? "") ||
    draft.cacheReadPrice !== (settings.cache_read_usd_per_million ?? "") ||
    draft.cacheWritePrice !== (settings.cache_write_usd_per_million ?? "")
  );
}

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
  const [audits, setAudits] = useState<ConfigurationAudit[]>([]);
  const [selectedProvider, setSelectedProvider] = useState<AiProvider>("openai");
  const [drafts, setDrafts] = useState<Partial<Record<AiProvider, ProviderDraft>>>({});
  const [message, setMessage] = useState("");
  const [messageKind, setMessageKind] = useState<"success" | "error">("success");
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState("");
  const [showApiKey, setShowApiKey] = useState<Record<AiProvider, boolean>>({
    openai: false,
    anthropic: false,
  });
  const providerInitialized = useRef(false);

  const applySettings = useCallback((next: AiSettings) => {
    setSettings(next);
    setDrafts(
      Object.fromEntries(
        next.providers.map((provider) => [provider.provider, providerDraft(provider)]),
      ) as Record<AiProvider, ProviderDraft>,
    );
    if (!providerInitialized.current) {
      setSelectedProvider(next.active_provider ?? "openai");
      providerInitialized.current = true;
    }
  }, []);

  const refresh = useCallback(
    async (showLoading = true) => {
      if (showLoading) setLoading(true);
      try {
        const [nextSettings, auditResponse] = await Promise.all([
          api.aiSettings(),
          api.configurationAudits(),
        ]);
        applySettings(nextSettings);
        setAudits(auditResponse.items);
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          onSignedOut("登录状态已失效，请重新登录");
          return;
        }
        setMessageKind("error");
        setMessage(errorMessage(error));
      } finally {
        if (showLoading) setLoading(false);
      }
    },
    [applySettings, onSignedOut],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

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
        context_window_tokens: requiredNumber(draft.contextWindowTokens, "上下文窗口"),
        max_output_tokens: requiredNumber(draft.maxOutputTokens, "每批回答上限"),
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
  const contextTokens = Number(draft?.contextWindowTokens ?? 0);
  const outputTokens = Number(draft?.maxOutputTokens ?? 0);
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
    Math.min(contextInputBudgetTokens, requestInputBudgetTokens),
  );

  return (
    <div className="settings-page-layout">
      <header className="settings-navbar">
        <div className="settings-nav-start">
          <button className="settings-icon-btn" onClick={onBack} title="返回审查控制台" aria-label="返回审查控制台">
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
          <button className="settings-icon-btn" onClick={() => void refresh()} disabled={loading || Boolean(busyAction)} title="刷新设置" aria-label="刷新设置">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M20 6v6h-6" /><path d="M4 18v-6h6" /><path d="M18.5 9A7 7 0 0 0 6 5.5L4 8" /><path d="M5.5 15A7 7 0 0 0 18 18.5l2-2.5" /></svg>
          </button>
          <div className="settings-user"><span>{user.username.slice(0, 1).toUpperCase()}</span><strong>{user.username}</strong></div>
          <button className="settings-icon-btn" onClick={logout} title="退出登录" aria-label="退出登录">
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

        {message && <div className={`settings-message is-${messageKind}`} role="alert">{message}</div>}

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
                      <span><strong>上下文与回答</strong><small>按模型官网或中转站说明填写</small></span>
                      <span className="settings-summary-value">{formatTokens(draft.contextWindowTokens)} Token</span>
                    </summary>
                    <fieldset disabled={Boolean(busyAction)}>
                      <div className="settings-form-grid settings-form-grid-compact settings-context-grid">
                        <SelectField name="context-window-tokens" label="模型上下文窗口" value={draft.contextWindowTokens} options={contextWindowOptions(draft.contextWindowTokens)} onChange={updateContextWindow} />
                        <SelectField name="max-output-tokens" label="每批回答上限" value={draft.maxOutputTokens} options={outputTokenOptions(draft.contextWindowTokens, draft.maxOutputTokens)} onChange={(value) => updateDraft("maxOutputTokens", value)} />
                        <NumberField name="read-timeout-seconds" label="最多等待回答" value={draft.readTimeoutSeconds} min="10" max="3600" step="1" suffix="秒" onChange={(value) => updateDraft("readTimeoutSeconds", value)} />
                      </div>
                      <div className="settings-info-band settings-context-budget"><strong>单批可用输入约 {formatTokens(inputBudgetTokens)} Token</strong><span>超出的文件会自动进入下一批</span></div>
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
                  </div>
                </form>
              </div>
            </section>

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
  value: string;
  options: Array<[string, string]>;
  onChange: (value: string) => void;
}

function SelectField({ name, label, value, options, onChange }: SelectFieldProps) {
  const knownValue = options.some(([option]) => option === value);
  return (
    <label>
      <span>{label}</span>
      <select id={`settings-${name}`} name={`settings-${name}`} value={value} onChange={(event) => onChange(event.target.value)}>
        {!knownValue && <option value={value}>自定义（{value} Token）</option>}
        {options.map(([option, text]) => <option key={option} value={option}>{text}</option>)}
      </select>
    </label>
  );
}
