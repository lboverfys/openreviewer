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
  ReviewPolicyUpdate,
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
  apiKey: string;
  clearApiKey: boolean;
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

interface PolicyDraft {
  maxUnits: string;
  maxScopeDepth: string;
  maxUnitInputBytes: string;
  maxTotalInputBytes: string;
}

const providerLabels: Record<AiProvider, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
};

const openAiProtocolLabels: Record<
  Extract<AiApiProtocol, "responses" | "chat_completions">,
  string
> = {
  responses: "Responses",
  chat_completions: "Chat Completions",
};

const testStatusLabels = {
  untested: "待测试",
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
  "review_policy.updated": "更新审查预算",
};

const fieldLabels: Record<string, string> = {
  active_provider: "激活供应商",
  api_protocol: "接口协议",
  api_key: "API Key",
  model: "模型",
  max_output_tokens: "输出 Token 上限",
  connect_timeout_seconds: "连接超时",
  read_timeout_seconds: "读取超时",
  write_timeout_seconds: "写入超时",
  pool_timeout_seconds: "连接池超时",
  max_request_bytes: "请求大小",
  max_response_bytes: "响应大小",
  input_usd_per_million: "输入单价",
  output_usd_per_million: "输出单价",
  cache_read_usd_per_million: "缓存读取单价",
  cache_write_usd_per_million: "缓存写入单价",
  test_status: "测试状态",
  max_units: "Review Unit 数量",
  max_scope_depth: "规则目录深度",
  max_unit_input_bytes: "单 Unit 输入",
  max_total_input_bytes: "总输入",
};

function providerDraft(settings: AiProviderSettings): ProviderDraft {
  return {
    model: settings.model,
    apiProtocol: settings.api_protocol,
    apiKey: "",
    clearApiKey: false,
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

function policyDraft(settings: AiSettings): PolicyDraft {
  return {
    maxUnits: String(settings.max_units),
    maxScopeDepth: String(settings.max_scope_depth),
    maxUnitInputBytes: String(settings.max_unit_input_bytes),
    maxTotalInputBytes: String(settings.max_total_input_bytes),
  };
}

function requiredNumber(value: string, label: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`${label}必须是数字`);
  return parsed;
}

function optionalDecimal(value: string): string | null {
  const normalized = value.trim();
  return normalized ? normalized : null;
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
          : "未配置密钥"}
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
  const [selectedProvider, setSelectedProvider] =
    useState<AiProvider>("openai");
  const [drafts, setDrafts] = useState<
    Partial<Record<AiProvider, ProviderDraft>>
  >({});
  const [policy, setPolicy] = useState<PolicyDraft | null>(null);
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
        next.providers.map((provider) => [
          provider.provider,
          providerDraft(provider),
        ]),
      ) as Record<AiProvider, ProviderDraft>,
    );
    setPolicy(policyDraft(next));
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

  function updateDraft<K extends keyof ProviderDraft>(
    field: K,
    value: ProviderDraft[K],
  ) {
    setDrafts((current) => ({
      ...current,
      [selectedProvider]: {
        ...current[selectedProvider]!,
        [field]: value,
      },
    }));
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
      const payload: AiProviderUpdate = {
        expected_revision: settings.revision,
        model: draft.model.trim(),
        api_protocol: draft.apiProtocol,
        api_key: draft.apiKey.trim() || null,
        clear_api_key: draft.clearApiKey,
        max_output_tokens: requiredNumber(
          draft.maxOutputTokens,
          "输出 Token 上限",
        ),
        connect_timeout_seconds: requiredNumber(
          draft.connectTimeoutSeconds,
          "连接超时",
        ),
        read_timeout_seconds: requiredNumber(draft.readTimeoutSeconds, "读取超时"),
        write_timeout_seconds: requiredNumber(
          draft.writeTimeoutSeconds,
          "写入超时",
        ),
        pool_timeout_seconds: requiredNumber(
          draft.poolTimeoutSeconds,
          "连接池超时",
        ),
        max_request_bytes: requiredNumber(draft.maxRequestBytes, "请求大小"),
        max_response_bytes: requiredNumber(draft.maxResponseBytes, "响应大小"),
        input_usd_per_million: optionalDecimal(draft.inputPrice),
        output_usd_per_million: optionalDecimal(draft.outputPrice),
        cache_read_usd_per_million: optionalDecimal(draft.cacheReadPrice),
        cache_write_usd_per_million: optionalDecimal(draft.cacheWritePrice),
      };
      await handleAction(
        `save-${selectedProvider}`,
        () => api.updateAiProvider(selectedProvider, payload),
        `${providerLabels[selectedProvider]} 配置已保存`,
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
      `${providerLabels[selectedProvider]} 连接测试通过`,
      true,
    );
  }

  async function activateProvider() {
    if (!settings) return;
    await handleAction(
      `activate-${selectedProvider}`,
      () => api.activateAiProvider(selectedProvider, settings.revision),
      `${providerLabels[selectedProvider]} 已激活`,
    );
  }

  async function savePolicy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settings || !policy) return;
    try {
      const payload: ReviewPolicyUpdate = {
        expected_revision: settings.revision,
        max_units: requiredNumber(policy.maxUnits, "Review Unit 数量"),
        max_scope_depth: requiredNumber(policy.maxScopeDepth, "规则目录深度"),
        max_unit_input_bytes: requiredNumber(
          policy.maxUnitInputBytes,
          "单 Unit 输入",
        ),
        max_total_input_bytes: requiredNumber(policy.maxTotalInputBytes, "总输入"),
      };
      await handleAction(
        "save-policy",
        () => api.updateReviewPolicy(payload),
        "审查预算已保存",
      );
    } catch (error) {
      setMessageKind("error");
      setMessage(errorMessage(error));
    }
  }

  async function logout() {
    try {
      await api.logout();
    } finally {
      onSignedOut();
    }
  }

  return (
    <div className="settings-page-layout">
      <header className="settings-navbar">
        <div className="settings-nav-start">
          <button className="settings-icon-btn" onClick={onBack} title="返回审查控制台">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M19 12H5" />
              <path d="m12 19-7-7 7-7" />
            </svg>
          </button>
          <div className="settings-heading-lockup">
            <div className="settings-heading-icon">
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="3" />
                <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56V21h-4v-.08A1.7 1.7 0 0 0 8.96 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.56-1.03H3v-4h.08A1.7 1.7 0 0 0 4.6 8.96a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 8.96 4.6 1.7 1.7 0 0 0 10 3.08V3h4v.08a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9c.14.6.67 1.02 1.29 1.03H21v4h-.31c-.62 0-1.15.42-1.29 1.03Z" />
              </svg>
            </div>
            <div>
              <strong>AI 运行设置</strong>
              <span>OpenReviewer</span>
            </div>
          </div>
        </div>
        <div className="settings-nav-end">
          <button
            className="settings-icon-btn"
            onClick={() => void refresh()}
            disabled={loading || Boolean(busyAction)}
            title="刷新设置"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M20 6v6h-6" />
              <path d="M4 18v-6h6" />
              <path d="M18.5 9A7 7 0 0 0 6 5.5L4 8" />
              <path d="M5.5 15A7 7 0 0 0 18 18.5l2-2.5" />
            </svg>
          </button>
          <div className="settings-user">
            <span>{user.username.slice(0, 1).toUpperCase()}</span>
            <strong>{user.username}</strong>
          </div>
          <button className="settings-icon-btn" onClick={logout} title="退出登录">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
              <path d="m16 17 5-5-5-5" />
              <path d="M21 12H9" />
            </svg>
          </button>
        </div>
      </header>

      <main className="settings-main">
        <div className="settings-title-row">
          <div>
            <h1>模型与审查策略</h1>
            <div className="settings-meta-line">
              <span>配置版本 {settings?.revision ?? "—"}</span>
              <span>最近更新 {formatDate(settings?.updated_at ?? null)}</span>
              <span>修改人 {settings?.updated_by ?? "—"}</span>
            </div>
          </div>
          <div className={`settings-runtime-state ${settings?.active_provider ? "is-active" : "is-idle"}`}>
            <span className="settings-status-dot" />
            {settings?.active_provider
              ? `${providerLabels[settings.active_provider]} 已激活`
              : "未激活供应商"}
          </div>
        </div>

        {message && (
          <div className={`settings-message is-${messageKind}`} role="alert">
            {message}
          </div>
        )}

        {loading || !settings || !selectedSettings || !draft || !policy ? (
          <div className="settings-loading">正在读取设置…</div>
        ) : (
          <>
            <section className="settings-provider-workspace">
              <aside className="settings-provider-nav" aria-label="AI 供应商">
                <span className="settings-nav-label">供应商</span>
                {settings.providers.map((provider) => (
                  <button
                    key={provider.provider}
                    className={selectedProvider === provider.provider ? "is-selected" : ""}
                    onClick={() => setSelectedProvider(provider.provider)}
                  >
                    <span className={`provider-mark is-${provider.provider}`}>
                      {provider.provider === "openai" ? "O" : "A"}
                    </span>
                    <span>
                      <strong>{providerLabels[provider.provider]}</strong>
                      <small>
                        {provider.active
                          ? "正在使用"
                          : testStatusLabels[provider.test_status]}
                      </small>
                    </span>
                    {provider.active && <i className="provider-active-dot" />}
                  </button>
                ))}
              </aside>

              <div className="settings-provider-content">
                <div className="settings-section-heading">
                  <div>
                    <h2>{providerLabels[selectedProvider]}</h2>
                    <ProviderStatus settings={selectedSettings} />
                  </div>
                </div>

                <form onSubmit={saveProvider}>
                  <fieldset className="settings-fieldset" disabled={Boolean(busyAction)}>
                    <legend>基础连接</legend>
                    {selectedProvider === "openai" && (
                      <div className="settings-protocol-field">
                        <span>接口协议</span>
                        <div
                          className="settings-segmented-control"
                          role="group"
                          aria-label="OpenAI 接口协议"
                        >
                          {(
                            Object.keys(openAiProtocolLabels) as Array<
                              keyof typeof openAiProtocolLabels
                            >
                          ).map((protocol) => (
                            <button
                              key={protocol}
                              type="button"
                              className={
                                draft.apiProtocol === protocol ? "is-selected" : ""
                              }
                              aria-pressed={draft.apiProtocol === protocol}
                              onClick={() => updateDraft("apiProtocol", protocol)}
                            >
                              {openAiProtocolLabels[protocol]}
                            </button>
                          ))}
                        </div>
                      </div>
                    )}
                    <div className="settings-form-grid settings-form-grid-primary">
                      <label>
                        <span>模型 ID</span>
                        <input
                          id={`settings-${selectedProvider}-model`}
                          name={`settings-${selectedProvider}-model`}
                          required
                          maxLength={200}
                          value={draft.model}
                          onChange={(event) => updateDraft("model", event.target.value)}
                          placeholder={selectedProvider === "openai" ? "gpt-5" : "claude-sonnet-4-5"}
                          autoComplete="off"
                        />
                      </label>
                      <label>
                        <span>API Key</span>
                        <div className="settings-secret-input">
                          <input
                            id={`settings-${selectedProvider}-api-key`}
                            name={`settings-${selectedProvider}-api-key`}
                            type={showApiKey[selectedProvider] ? "text" : "password"}
                            value={draft.apiKey}
                            onChange={(event) => updateDraft("apiKey", event.target.value)}
                            placeholder={selectedSettings.api_key_configured ? `已保存 ${selectedSettings.api_key_mask}` : "输入 API Key"}
                            autoComplete="new-password"
                            disabled={draft.clearApiKey}
                          />
                          <button
                            type="button"
                            onClick={() =>
                              setShowApiKey((current) => ({
                                ...current,
                                [selectedProvider]: !current[selectedProvider],
                              }))
                            }
                            title={showApiKey[selectedProvider] ? "隐藏 API Key" : "显示 API Key"}
                          >
                            {showApiKey[selectedProvider] ? (
                              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 3l18 18"/><path d="M10.6 10.6a2 2 0 0 0 2.8 2.8"/><path d="M9.9 4.2A10.4 10.4 0 0 1 12 4c5 0 9 5 9 8a9.6 9.6 0 0 1-2 3.5"/><path d="M6.6 6.6C4.4 8 3 10.2 3 12c0 3 4 8 9 8 1.2 0 2.3-.3 3.3-.8"/></svg>
                            ) : (
                              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>
                            )}
                          </button>
                        </div>
                      </label>
                    </div>
                    {selectedSettings.api_key_configured && (
                      <label className="settings-checkbox-row">
                        <input
                          id={`settings-${selectedProvider}-clear-api-key`}
                          name={`settings-${selectedProvider}-clear-api-key`}
                          type="checkbox"
                          checked={draft.clearApiKey}
                          onChange={(event) => updateDraft("clearApiKey", event.target.checked)}
                        />
                        <span>清除已保存的 API Key</span>
                      </label>
                    )}
                  </fieldset>

                  <fieldset className="settings-fieldset" disabled={Boolean(busyAction)}>
                    <legend>调用边界</legend>
                    <div className="settings-form-grid settings-form-grid-compact">
                      <NumberField name="max-output-tokens" label="输出 Token 上限" value={draft.maxOutputTokens} min="256" max="131072" onChange={(value) => updateDraft("maxOutputTokens", value)} />
                      <NumberField name="connect-timeout-seconds" label="连接超时（秒）" value={draft.connectTimeoutSeconds} min="0.1" max="3600" step="0.1" onChange={(value) => updateDraft("connectTimeoutSeconds", value)} />
                      <NumberField name="read-timeout-seconds" label="读取超时（秒）" value={draft.readTimeoutSeconds} min="0.1" max="3600" step="0.1" onChange={(value) => updateDraft("readTimeoutSeconds", value)} />
                      <NumberField name="write-timeout-seconds" label="写入超时（秒）" value={draft.writeTimeoutSeconds} min="0.1" max="3600" step="0.1" onChange={(value) => updateDraft("writeTimeoutSeconds", value)} />
                      <NumberField name="pool-timeout-seconds" label="连接池超时（秒）" value={draft.poolTimeoutSeconds} min="0.1" max="3600" step="0.1" onChange={(value) => updateDraft("poolTimeoutSeconds", value)} />
                      <NumberField name="max-request-bytes" label="请求上限（字节）" value={draft.maxRequestBytes} min="65536" max="10485760" onChange={(value) => updateDraft("maxRequestBytes", value)} />
                      <NumberField name="max-response-bytes" label="响应上限（字节）" value={draft.maxResponseBytes} min="65536" max="10485760" onChange={(value) => updateDraft("maxResponseBytes", value)} />
                    </div>
                  </fieldset>

                  <fieldset className="settings-fieldset" disabled={Boolean(busyAction)}>
                    <legend>成本估算（美元 / 百万 Token）</legend>
                    <div className="settings-form-grid settings-form-grid-pricing">
                      <NumberField name="input-price" label="输入" value={draft.inputPrice} min="0" max="1000000" step="0.000001" required={false} onChange={(value) => updateDraft("inputPrice", value)} />
                      <NumberField name="output-price" label="输出" value={draft.outputPrice} min="0" max="1000000" step="0.000001" required={false} onChange={(value) => updateDraft("outputPrice", value)} />
                      <NumberField name="cache-read-price" label="缓存读取" value={draft.cacheReadPrice} min="0" max="1000000" step="0.000001" required={false} onChange={(value) => updateDraft("cacheReadPrice", value)} />
                      <NumberField name="cache-write-price" label="缓存写入" value={draft.cacheWritePrice} min="0" max="1000000" step="0.000001" required={false} onChange={(value) => updateDraft("cacheWritePrice", value)} />
                    </div>
                  </fieldset>

                  <div className="settings-action-bar">
                    <button className="settings-secondary-btn" type="button" onClick={() => void testProvider()} disabled={!selectedSettings.configured || !selectedSettings.api_key_configured || Boolean(busyAction)}>
                      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="m13 2-9 12h7l-1 8 9-12h-7l1-8Z"/></svg>
                      {busyAction === `test-${selectedProvider}` ? "测试中…" : "测试连接"}
                    </button>
                    <button className="settings-secondary-btn is-activate" type="button" onClick={() => void activateProvider()} disabled={selectedSettings.test_status !== "succeeded" || selectedSettings.active || Boolean(busyAction)}>
                      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="m5 12 4 4L19 6"/></svg>
                      {selectedSettings.active ? "已激活" : "激活供应商"}
                    </button>
                    <button className="settings-primary-btn" type="submit" disabled={Boolean(busyAction)}>
                      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/></svg>
                      {busyAction === `save-${selectedProvider}` ? "保存中…" : "保存配置"}
                    </button>
                  </div>
                </form>
              </div>
            </section>

            <section className="settings-policy-section">
              <div className="settings-section-heading">
                <div>
                  <span className="settings-eyebrow">REVIEW POLICY</span>
                  <h2>审查输入预算</h2>
                </div>
              </div>
              <form onSubmit={savePolicy}>
                <fieldset disabled={Boolean(busyAction)}>
                  <div className="settings-form-grid settings-policy-grid">
                    <NumberField name="max-review-units" label="Review Unit 上限" value={policy.maxUnits} min="1" max="3000" onChange={(value) => setPolicy((current) => ({ ...current!, maxUnits: value }))} />
                    <NumberField name="max-scope-depth" label="规则目录深度" value={policy.maxScopeDepth} min="1" max="64" onChange={(value) => setPolicy((current) => ({ ...current!, maxScopeDepth: value }))} />
                    <NumberField name="max-unit-input-bytes" label="单 Unit 输入上限（字节）" value={policy.maxUnitInputBytes} min="4096" max="10485760" onChange={(value) => setPolicy((current) => ({ ...current!, maxUnitInputBytes: value }))} />
                    <NumberField name="max-total-input-bytes" label="总输入上限（字节）" value={policy.maxTotalInputBytes} min="4096" max="104857600" onChange={(value) => setPolicy((current) => ({ ...current!, maxTotalInputBytes: value }))} />
                  </div>
                  <div className="settings-policy-action">
                    <button className="settings-primary-btn" type="submit" disabled={Boolean(busyAction)}>
                      保存审查预算
                    </button>
                  </div>
                </fieldset>
              </form>
            </section>

            <section className="settings-audit-section">
              <div className="settings-section-heading">
                <div>
                  <span className="settings-eyebrow">AUDIT LOG</span>
                  <h2>配置变更记录</h2>
                </div>
              </div>
              <div className="settings-audit-table-wrap">
                <table className="settings-audit-table">
                  <thead><tr><th>版本</th><th>操作</th><th>变更字段</th><th>管理员</th><th>时间</th></tr></thead>
                  <tbody>
                    {audits.map((audit) => (
                      <tr key={audit.revision}>
                        <td className="code-font">r{audit.revision}</td>
                        <td>{auditActionLabels[audit.action] ?? audit.action}</td>
                        <td>{audit.changed_fields.map((field) => fieldLabels[field] ?? field).join("、")}</td>
                        <td>{audit.actor}</td>
                        <td>{formatDate(audit.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {audits.length === 0 && <div className="settings-empty-audit">暂无配置变更</div>}
              </div>
            </section>
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
  required?: boolean;
  onChange: (value: string) => void;
}

function NumberField({
  name,
  label,
  value,
  min,
  max,
  step = "1",
  required = true,
  onChange,
}: NumberFieldProps) {
  return (
    <label>
      <span>{label}</span>
      <input
        id={`settings-${name}`}
        name={`settings-${name}`}
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        required={required}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}
