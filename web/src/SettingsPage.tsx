import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  api,
  ApiError,
  clearSettingsCache,
  peekReadCache,
  subscribeReadCache,
} from "./api";
import AgentSettingsPanel from "./AgentSettingsPanel";
import { NumberField } from "./SettingsFields";
import {
  bytesToInput,
  inputToBytes,
  KIB,
  mergeProviderDrafts,
  mergeReviewPolicyDraft,
  MIB,
  normalizeAiSettings,
  optionalDecimal,
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
  // 复用未过期快照，后台 refresh 校验最新版本，避免路由切换时整页 loading。
  const cachedSettings = peekReadCache<AiSettings>("ai-settings");
  const initialSettings = cachedSettings ? normalizeAiSettings(cachedSettings) : null;
  const [settings, setSettings] = useState<AiSettings | null>(initialSettings);
  const [policyDraft, setPolicyDraft] = useState<ReviewPolicyDraft | null>(
    initialSettings ? reviewPolicyDraft(initialSettings) : null,
  );
  const [policyMessage, setPolicyMessage] = useState("");
  const [policyMessageKind, setPolicyMessageKind] = useState<"success" | "error">("success");
  const [audits, setAudits] = useState<ConfigurationAudit[]>([]);
  const [auditsLoaded, setAuditsLoaded] = useState(false);
  const [auditsLoading, setAuditsLoading] = useState(false);
  const [auditError, setAuditError] = useState("");
  const [auditExpanded, setAuditExpanded] = useState(false);
  const [selectedProvider, setSelectedProvider] = useState<AiProvider>(
    initialSettings?.active_provider ?? "openai",
  );
  const [drafts, setDrafts] = useState<Partial<Record<AiProvider, ProviderDraft>>>(
    initialSettings
      ? Object.fromEntries(
        initialSettings.providers.map((provider) => [provider.provider, providerDraft(provider)]),
      ) as Record<AiProvider, ProviderDraft>
      : {},
  );
  const [message, setMessage] = useState("");
  const [messageKind, setMessageKind] = useState<"success" | "error">("success");
  const [loading, setLoading] = useState(initialSettings === null);
  const [busyAction, setBusyAction] = useState("");
  const [agentRefreshRequest, setAgentRefreshRequest] = useState(0);
  const [showApiKey, setShowApiKey] = useState<Record<AiProvider, boolean>>({
    openai: false,
    anthropic: false,
  });
  const providerInitialized = useRef(Boolean(initialSettings));
  const refreshSequence = useRef(0);
  const auditRequestSequence = useRef(0);
  const settingsRevisionRef = useRef<number | null>(initialSettings?.revision ?? null);
  const appliedSettingsRef = useRef<AiSettings | null>(initialSettings);
  // Agent 可能先返回新 revision；主配置落地前暂存它，避免旧快照被永久丢弃。
  const pendingAgentRevisionRef = useRef<number | null>(null);
  // 编辑时锁定服务端版本，让并发更新明确返回 409，避免静默覆盖远端修改。
  const providerDraftRevisionRef = useRef<Partial<Record<AiProvider, number>>>({});
  const policyDraftRevisionRef = useRef<number | null>(null);

  const applySettings = useCallback((
    next: AiSettings,
    options: { resetProvider?: AiProvider; resetPolicy?: boolean } = {},
  ): boolean => {
    const normalized = normalizeAiSettings(next);
    // Agent 写操作也递增全局 revision，旧响应不能回退版本或重置主表单草稿。
    const knownRevision = settingsRevisionRef.current;
    if (knownRevision !== null && normalized.revision < knownRevision) return false;
    settingsRevisionRef.current = Math.max(
      knownRevision ?? normalized.revision,
      normalized.revision,
    );
    const previous = appliedSettingsRef.current;
    appliedSettingsRef.current = normalized;
    setSettings(normalized);
    setDrafts((current) => {
      const merged = mergeProviderDrafts(
        previous,
        current,
        normalized,
        options.resetProvider,
      );
      for (const item of normalized.providers) {
        const localDraft = merged[item.provider];
        const dirty = localDraft !== undefined
          && providerHasChanges(item, localDraft);
        if (item.provider === options.resetProvider || !dirty) {
          delete providerDraftRevisionRef.current[item.provider];
        } else if (providerDraftRevisionRef.current[item.provider] === undefined) {
          providerDraftRevisionRef.current[item.provider] =
            knownRevision ?? normalized.revision;
        }
      }
      return merged;
    });
    setPolicyDraft((current) => {
      const merged = mergeReviewPolicyDraft(
        previous,
        current,
        normalized,
        options.resetPolicy,
      );
      if (options.resetPolicy || !reviewPolicyHasChanges(normalized, merged)) {
        policyDraftRevisionRef.current = null;
      } else if (policyDraftRevisionRef.current === null) {
        policyDraftRevisionRef.current = knownRevision ?? normalized.revision;
      }
      return merged;
    });
    if (!providerInitialized.current) {
      setSelectedProvider(normalized.active_provider ?? "openai");
      providerInitialized.current = true;
    }
    return true;
  }, []);

  // stale-while-revalidate 先渲染旧快照，再订阅 API 层后台写入的新快照。
  useEffect(() => subscribeReadCache<AiSettings>("ai-settings", (next) => {
    applySettings(next);
  }), [applySettings]);

  const handleAgentRevisionChange = useCallback((revision: number) => {
    if (appliedSettingsRef.current === null) {
      pendingAgentRevisionRef.current = Math.max(
        pendingAgentRevisionRef.current ?? 0,
        revision,
      );
      return;
    }
    const knownRevision = settingsRevisionRef.current;
    if (knownRevision !== null && revision <= knownRevision) return;
    settingsRevisionRef.current = revision;
    if (appliedSettingsRef.current) {
      appliedSettingsRef.current = { ...appliedSettingsRef.current, revision };
    }
    setSettings((current) => {
      if (!current || revision <= current.revision) return current;
      return { ...current, revision };
    });
  }, []);

  const loadAudits = useCallback(async (force = false, signal?: AbortSignal) => {
    if (auditsLoaded && !force) return;
    const sequence = ++auditRequestSequence.current;
    setAuditsLoading(true);
    setAuditError("");
    try {
      const response = await api.configurationAudits(signal, force);
      if (sequence !== auditRequestSequence.current || signal?.aborted) return;
      setAudits(response.items);
      setAuditsLoaded(true);
    } catch (error) {
      if (signal?.aborted || sequence !== auditRequestSequence.current) return;
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setAuditError(errorMessage(error));
    } finally {
      if (sequence === auditRequestSequence.current && !signal?.aborted) {
        setAuditsLoading(false);
      }
    }
  }, [auditsLoaded, onSignedOut]);

  const invalidateAudits = useCallback(() => {
    auditRequestSequence.current += 1;
    setAuditsLoaded(false);
    setAudits([]);
    setAuditError("");
    setAuditsLoading(false);
  }, []);

  const refresh = useCallback(
    async (showLoading = true, signal?: AbortSignal, force = false) => {
      const sequence = ++refreshSequence.current;
      if (showLoading) setLoading(true);
      const reportError = (error: unknown) => {
        if (signal?.aborted || sequence !== refreshSequence.current) return;
        if (error instanceof ApiError && error.status === 401) {
          onSignedOut("登录状态已失效，请重新登录");
          return;
        }
        setMessageKind("error");
        setMessage(errorMessage(error));
      };

      // 审计记录默认折叠，首屏只请求主配置。
      const settingsRequest = api.aiSettings(signal, force)
        .then((next) => {
          if (sequence === refreshSequence.current) applySettings(next);
        })
        .catch(reportError)
        .finally(() => {
          if (
            showLoading
            && sequence === refreshSequence.current
            && !signal?.aborted
          ) setLoading(false);
        });
      await settingsRequest;
    },
    [applySettings, onSignedOut],
  );

  useEffect(() => {
    const pendingRevision = pendingAgentRevisionRef.current;
    if (!settings || pendingRevision === null) return;
    // 首个快照显示后，若 Agent 观察到更高版本，补一次有界强制读取并清标记。
    pendingAgentRevisionRef.current = null;
    if (pendingRevision <= settings.revision) return;
    clearSettingsCache();
    void refresh(false, undefined, true);
  }, [refresh, settings]);

  useEffect(() => {
    const controller = new AbortController();
    // 未过期缓存直接复用；手动刷新或写入配置时才清缓存并绕过缓存。
    void refresh(initialSettings === null, controller.signal);
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    if (!message || messageKind !== "success") return undefined;
    const timer = window.setTimeout(() => setMessage(""), 4000);
    return () => window.clearTimeout(timer);
  }, [message, messageKind]);

  async function refreshAllSettings() {
    clearSettingsCache();
    invalidateAudits();
    setAgentRefreshRequest((current) => current + 1);
    await refresh();
    if (auditExpanded) await loadAudits(true);
  }

  const selectedSettings = useMemo(
    () => settings?.providers.find((item) => item.provider === selectedProvider),
    [selectedProvider, settings],
  );
  const draft = drafts[selectedProvider];

  function updateProviderDraft(
    provider: AiProvider,
    updater: (current: ProviderDraft) => ProviderDraft,
  ) {
    setDrafts((current) => {
      const previousDraft = current[provider];
      if (!previousDraft) return current;
      const nextDraft = updater(previousDraft);
      const saved = appliedSettingsRef.current?.providers.find(
        (item) => item.provider === provider,
      );
      if (saved && providerHasChanges(saved, nextDraft)) {
        if (providerDraftRevisionRef.current[provider] === undefined) {
          providerDraftRevisionRef.current[provider] = expectedRevision();
        }
      } else {
        delete providerDraftRevisionRef.current[provider];
      }
      return { ...current, [provider]: nextDraft };
    });
  }

  function updateDraft<K extends keyof ProviderDraft>(field: K, value: ProviderDraft[K]) {
    updateProviderDraft(selectedProvider, (current) => ({
      ...current,
      [field]: value,
    }));
  }

  // 主设置与 Agent 共用 revision；事件处理器优先取 ref 中已观察到的高版本。
  function expectedRevision(): number {
    return Math.max(
      settings?.revision ?? 0,
      settingsRevisionRef.current ?? 0,
    );
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
    resetProvider?: AiProvider,
  ) {
    // 让尚未完成的首屏 GET 失效，避免它在保存/测试返回后回填旧快照。
    const sequence = ++refreshSequence.current;
    setBusyAction(action);
    setMessage("");
    try {
      const next = await operation();
      if (sequence !== refreshSequence.current) return;
      const applied = applySettings(next, { resetProvider });
      if (!applied) {
        // Agent 已推进 revision，当前响应是旧快照；强制读取完整配置。
        clearSettingsCache();
        invalidateAudits();
        const refreshSequenceBefore = refreshSequence.current;
        await refresh(false, undefined, true);
        if (refreshSequence.current === refreshSequenceBefore + 1) {
          setBusyAction("");
        }
        return;
      }
      if (auditExpanded) {
        await loadAudits(true);
        if (sequence !== refreshSequence.current) return;
      } else {
        invalidateAudits();
      }
      showSuccess(successText);
    } catch (error) {
      if (sequence !== refreshSequence.current) return;
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setMessageKind("error");
      setMessage(errorMessage(error));
      if (refreshAfterError || (error instanceof ApiError && error.status === 409)) {
        clearSettingsCache();
        invalidateAudits();
        const refreshSequenceBefore = refreshSequence.current;
        await refresh(false);
        // refresh() 占用新序列号；没有更新操作抢占时，当前 action 才结束 busy 状态。
        if (refreshSequence.current === refreshSequenceBefore + 1) {
          setBusyAction("");
        }
      }
    } finally {
      if (sequence === refreshSequence.current) setBusyAction("");
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
        expected_revision: providerDraftRevisionRef.current[selectedProvider]
          ?? expectedRevision(),
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
        false,
        selectedProvider,
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
      () => api.testAiProvider(selectedProvider, expectedRevision()),
      `${providerShortLabels[selectedProvider]} 连接正常，现在可以启用`,
      true,
    );
  }

  async function activateProvider() {
    if (!settings) return;
    await handleAction(
      `activate-${selectedProvider}`,
      () => api.activateAiProvider(selectedProvider, expectedRevision()),
      `${providerShortLabels[selectedProvider]} 已启用，后续审查会使用这套配置`,
    );
  }

  function updatePolicyDraft<K extends keyof ReviewPolicyDraft>(
    field: K,
    value: ReviewPolicyDraft[K],
  ) {
    setPolicyDraft((current) => {
      if (!current) return current;
      const next = { ...current, [field]: value };
      const saved = appliedSettingsRef.current;
      if (saved && reviewPolicyHasChanges(saved, next)) {
        policyDraftRevisionRef.current ??= expectedRevision();
      } else {
        policyDraftRevisionRef.current = null;
      }
      return next;
    });
  }

  async function saveReviewPolicy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settings || !policyDraft) return;
    const sequence = ++refreshSequence.current;
    setBusyAction("save-policy");
    setPolicyMessage("");
    try {
      const payload: ReviewPolicyUpdate = {
        expected_revision: policyDraftRevisionRef.current ?? expectedRevision(),
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
      };
      const next = await api.updateReviewPolicy(payload);
      if (sequence !== refreshSequence.current) return;
      const applied = applySettings(next, { resetPolicy: true });
      if (!applied) {
        clearSettingsCache();
        invalidateAudits();
        const refreshSequenceBefore = refreshSequence.current;
        await refresh(false, undefined, true);
        if (refreshSequence.current === refreshSequenceBefore + 1) {
          setBusyAction("");
        }
        return;
      }
      if (auditExpanded) {
        await loadAudits(true);
        if (sequence !== refreshSequence.current) return;
      } else {
        invalidateAudits();
      }
      setPolicyMessageKind("success");
      setPolicyMessage("审查范围已保存");
    } catch (error) {
      if (sequence !== refreshSequence.current) return;
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setPolicyMessageKind("error");
      setPolicyMessage(errorMessage(error));
      if (error instanceof ApiError && error.status === 409) {
        clearSettingsCache();
        invalidateAudits();
        const refreshSequenceBefore = refreshSequence.current;
        await refresh(false);
        if (refreshSequence.current === refreshSequenceBefore + 1) {
          setBusyAction("");
        }
      }
    } finally {
      if (sequence === refreshSequence.current) setBusyAction("");
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
                      <span><strong>模型行为</strong><small>代码分批、上下文和输出长度由系统自动管理</small></span>
                      <span className="settings-summary-value">自动</span>
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
                      <div className="settings-form-grid settings-form-grid-compact">
                        <NumberField name="read-timeout-seconds" label="最多等待回答" value={draft.readTimeoutSeconds} min="10" max="3600" step="1" suffix="秒" onChange={(value) => updateDraft("readTimeoutSeconds", value)} />
                      </div>
                      <details className="settings-nested-disclosure">
                        <summary>传输与网络高级设置</summary>
                        <div className="settings-form-grid settings-form-grid-compact">
                          <NumberField name="max-request-mib" label="请求保护上限" value={bytesToInput(draft.maxRequestBytes, MIB)} min="0.0625" max="10" step="0.0625" suffix="MiB" onChange={(value) => updateDraft("maxRequestBytes", inputToBytes(value, MIB))} />
                          <NumberField name="max-response-mib" label="响应保护上限" value={bytesToInput(draft.maxResponseBytes, MIB)} min="0.0625" max="16" step="0.0625" suffix="MiB" onChange={(value) => updateDraft("maxResponseBytes", inputToBytes(value, MIB))} />
                          <NumberField name="connect-timeout-seconds" label="建立连接" value={draft.connectTimeoutSeconds} min="0.1" max="3600" step="0.1" suffix="秒" onChange={(value) => updateDraft("connectTimeoutSeconds", value)} />
                          <NumberField name="write-timeout-seconds" label="发送请求" value={draft.writeTimeoutSeconds} min="0.1" max="3600" step="0.1" suffix="秒" onChange={(value) => updateDraft("writeTimeoutSeconds", value)} />
                          <NumberField name="pool-timeout-seconds" label="等待空闲连接" value={draft.poolTimeoutSeconds} min="0.1" max="3600" step="0.1" suffix="秒" onChange={(value) => updateDraft("poolTimeoutSeconds", value)} />
                        </div>
                      </details>
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
                    <h2>审查范围</h2>
                    <p>系统会自动管理模型上下文、分批和输出长度，避免遗漏可审查内容。</p>
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

            <details
              className="settings-audit-section"
              onToggle={(event) => {
                const open = event.currentTarget.open;
                setAuditExpanded(open);
                if (open) void loadAudits();
              }}
            >
              <summary>
                <span>
                  <strong>配置变更记录</strong>
                  <small>{auditsLoaded ? `最近 ${audits.length} 条，不包含密钥内容` : "展开后加载记录"}</small>
                </span>
                <span>{auditsLoading ? "正在加载" : "展开查看"}</span>
              </summary>
              <div className="settings-audit-table-wrap">
                {auditError && <div className="settings-message is-error" role="alert">{auditError}</div>}
                <table className="settings-audit-table">
                  <thead><tr><th>版本</th><th>操作</th><th>变更内容</th><th>管理员</th><th>时间</th></tr></thead>
                  <tbody>{audits.map((audit) => <tr key={audit.revision}><td className="code-font">r{audit.revision}</td><td>{auditActionLabels[audit.action] ?? audit.action}</td><td>{audit.changed_fields.map((field) => fieldLabels[field] ?? field).join("、")}</td><td>{audit.actor}</td><td>{formatDate(audit.created_at)}</td></tr>)}</tbody>
                </table>
                {auditsLoading && <div className="settings-loading">正在读取配置变更...</div>}
                {!auditsLoading && auditsLoaded && audits.length === 0 && <div className="settings-empty-audit">暂无配置变更</div>}
              </div>
            </details>
          </>
        )}
        <AgentSettingsPanel parentRevision={settings?.revision ?? null} refreshRequest={agentRefreshRequest} onRevisionChange={handleAgentRevisionChange} onSignedOut={onSignedOut} />
      </main>
    </div>
  );
}
