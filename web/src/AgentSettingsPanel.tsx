import { useCallback, useEffect, useRef, useState } from "react";

import {
  api,
  ApiError,
  clearSettingsCache,
  peekReadCache,
  subscribeReadCache,
} from "./api";
import { testStatusLabels } from "./settings-labels";
import type {
  AiAgentSettings,
  AiAgentSettingsResponse,
  ReviewAgent,
} from "./types";
import { errorMessage } from "./utils";

interface AgentDraft {
  provider: AiAgentSettings["provider"];
  model: string;
  useSharedConnection: boolean;
  modelOverride: string;
  apiProtocol: AiAgentSettings["api_protocol"];
  apiBaseUrl: string;
  apiKey: string;
  clearApiKey: boolean;
  reasoningEffort: AiAgentSettings["reasoning_effort"];
  contextWindowTokens: string;
  maxOutputTokens: string;
  maxBatchInputTokens: string;
  connectTimeoutSeconds: string;
  readTimeoutSeconds: string;
  writeTimeoutSeconds: string;
  poolTimeoutSeconds: string;
  maxRetries: string;
  inputPrice: string;
  outputPrice: string;
  cacheReadPrice: string;
  cacheWritePrice: string;
}
const agentLabels: Record<ReviewAgent, { title: string; description: string }> = {
  security: { title: "安全审查", description: "关注权限、注入、敏感数据和可靠性风险。" },
  convention: { title: "规范审查", description: "检查仓库约定、编码风格和接口一致性。" },
  logic: { title: "逻辑审查", description: "检查业务逻辑、边界条件和回归风险。" },
  summary: { title: "汇总审查", description: "合并前三路结果并生成最终审查报告。" },
};

const agentOrder: ReviewAgent[] = ["security", "convention", "logic", "summary"];

const agentIcons: Record<ReviewAgent, string> = {
  security: "🛡",
  convention: "📐",
  logic: "🧠",
  summary: "📋",
};

function agentDraft(settings: AiAgentSettings): AgentDraft {
  return {
    provider: settings.provider,
    model: settings.model,
    useSharedConnection: Boolean(settings.use_shared_connection),
    modelOverride: settings.model_override ?? "",
    apiProtocol: settings.api_protocol,
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
    maxRetries: String(settings.max_retries),
    inputPrice: settings.input_usd_per_million ?? "",
    outputPrice: settings.output_usd_per_million ?? "",
    cacheReadPrice: settings.cache_read_usd_per_million ?? "",
    cacheWritePrice: settings.cache_write_usd_per_million ?? "",
  };
}

function agentDraftDirty(settings: AiAgentSettings, draft: AgentDraft): boolean {
  const saved = agentDraft(settings);
  return (
    draft.provider !== saved.provider
    || draft.model !== saved.model
    || draft.useSharedConnection !== saved.useSharedConnection
    || draft.modelOverride !== saved.modelOverride
    || draft.apiProtocol !== saved.apiProtocol
    || draft.apiBaseUrl !== saved.apiBaseUrl
    || draft.apiKey.trim() !== ""
    || draft.clearApiKey
    || draft.reasoningEffort !== saved.reasoningEffort
    || draft.contextWindowTokens !== saved.contextWindowTokens
    || draft.maxOutputTokens !== saved.maxOutputTokens
    || draft.maxBatchInputTokens !== saved.maxBatchInputTokens
    || draft.connectTimeoutSeconds !== saved.connectTimeoutSeconds
    || draft.readTimeoutSeconds !== saved.readTimeoutSeconds
    || draft.writeTimeoutSeconds !== saved.writeTimeoutSeconds
    || draft.poolTimeoutSeconds !== saved.poolTimeoutSeconds
    || draft.maxRetries !== saved.maxRetries
    || draft.inputPrice !== saved.inputPrice
    || draft.outputPrice !== saved.outputPrice
    || draft.cacheReadPrice !== saved.cacheReadPrice
    || draft.cacheWritePrice !== saved.cacheWritePrice
  );
}

function agentNumber(value: string, label: string, integer = false): number {
  if (!value.trim()) throw new Error("请填写" + label);
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || (integer && !Number.isInteger(parsed))) {
    throw new Error(label + "必须是有效数字");
  }
  return parsed;
}

export default function AgentSettingsPanel({
  refreshRequest,
  parentRevision,
  onRevisionChange,
  onSignedOut,
}: {
  refreshRequest: number;
  /** 主设置页与 Agent 配置共用 AiSettingsRecord.revision。 */
  parentRevision: number | null;
  /** 将 Agent 写操作产生的全局 revision 反馈给父页面。 */
  onRevisionChange: (revision: number) => void;
  onSignedOut: (message?: string) => void;
}) {
  // 与主设置页一样，优先使用 API 层的短时快照。这样返回设置页时 Agent
  // 表格会立即可见；若快照已过期，后台 refresh 仍会取最新数据。
  const cachedSettings = peekReadCache<AiAgentSettingsResponse>("agent-settings");
  const initialSettings = cachedSettings ?? null;
  const [settings, setSettings] = useState<AiAgentSettingsResponse | null>(initialSettings);
  const [drafts, setDrafts] = useState<Partial<Record<ReviewAgent, AgentDraft>>>(
    initialSettings
      ? Object.fromEntries(
        initialSettings.agents.map((item) => [item.agent, agentDraft(item)]),
      ) as Record<ReviewAgent, AgentDraft>
      : {},
  );
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [messageKind, setMessageKind] = useState<"success" | "error">("success");
  const [messageAgent, setMessageAgent] = useState<ReviewAgent | null>(null);
  const [shouldLoad, setShouldLoad] = useState(initialSettings !== null);
  const refreshSequence = useRef(0);
  const parentRevisionRef = useRef<number | null>(parentRevision);
  const sectionRef = useRef<HTMLElement | null>(null);
  const appliedSettingsRef = useRef<AiAgentSettingsResponse | null>(initialSettings);
  const initialCacheValidationPendingRef = useRef(initialSettings !== null);
  // 每个 Agent 草稿独立记录开始编辑时的全局 revision，防止主设置或
  // 其他 Agent 在编辑期间变更后被本地旧草稿覆盖。
  const draftRevisionRef = useRef<Partial<Record<ReviewAgent, number>>>({});

  const apply = useCallback((
    next: AiAgentSettingsResponse,
    resetDraftAgent?: ReviewAgent,
  ): boolean => {
    // 主设置与 Agent 设置共用全局 revision。若 Agent GET 恰好返回旧快照，
    // 只提升 revision，不覆盖用户正在编辑的草稿。
    const knownRevision = parentRevisionRef.current;
    const current = appliedSettingsRef.current;
    if (knownRevision !== null && next.revision < knownRevision) {
      return false;
    }
    const effectiveRevision = Math.max(
      next.revision,
      knownRevision ?? next.revision,
    );
    // 父页面只需要接收单调递增的 revision；避免一次普通刷新重复触发
    // 父页面更新，也避免旧快照把已知版本再次传播出去。
    if (knownRevision === null || effectiveRevision > knownRevision) {
      parentRevisionRef.current = effectiveRevision;
      onRevisionChange(effectiveRevision);
    }
    const applied =
      effectiveRevision === next.revision
        ? next
        : { ...next, revision: effectiveRevision };
    // 响应可能是 mutation 之前发出的旧快照。已有较新本地快照时，只
    // 提升显示版本，不替换 Agent 行和用户正在编辑的草稿。
    const staleAgainstLocal = current && next.revision < current.revision;
    const staleAgainstParent =
      current && knownRevision !== null && next.revision < knownRevision;
    if (current && (staleAgainstLocal || staleAgainstParent)) {
      if (effectiveRevision > current.revision) {
        const advanced = { ...current, revision: effectiveRevision };
        appliedSettingsRef.current = advanced;
        setSettings(advanced);
      }
      return false;
    }
    appliedSettingsRef.current = applied;
    setSettings(applied);
    setDrafts((currentDrafts) => {
      const merged = Object.fromEntries(
        next.agents.map((item) => {
        const previousItem = current?.agents.find(
          (candidate) => candidate.agent === item.agent,
        );
        const currentDraft = currentDrafts[item.agent];
        const preserveDirtyDraft =
          item.agent !== resetDraftAgent
          && previousItem
          && currentDraft
          && agentDraftDirty(previousItem, currentDraft);
        return [
          item.agent,
          preserveDirtyDraft ? currentDraft : agentDraft(item),
        ];
        }),
      ) as Record<ReviewAgent, AgentDraft>;
      for (const item of next.agents) {
        const localDraft = merged[item.agent];
        if (
          item.agent === resetDraftAgent
          || !localDraft
          || !agentDraftDirty(item, localDraft)
        ) {
          delete draftRevisionRef.current[item.agent];
        } else if (draftRevisionRef.current[item.agent] === undefined) {
          draftRevisionRef.current[item.agent] = knownRevision ?? next.revision;
        }
      }
      return merged;
    });
    return true;
  }, [onRevisionChange]);

  // 后台 stale-while-revalidate 完成后，API 层会写入新快照；把它推入
  // 当前面板，避免只有重新进入设置页才能看到 Agent 的最新状态。
  useEffect(() => subscribeReadCache<AiAgentSettingsResponse>("agent-settings", (next) => {
    apply(next);
  }), [apply]);

  useEffect(() => {
    if (parentRevision === null) {
      parentRevisionRef.current = null;
      return;
    }
    // 回调更新父状态是异步的；保留本地已经观察到的更高版本，防止旧
    // prop 在中间一次渲染中把它降回去。
    parentRevisionRef.current = Math.max(
      parentRevisionRef.current ?? parentRevision,
      parentRevision,
    );
    setSettings((current) => {
      if (!current || current.revision >= parentRevision) return current;
      const advanced = { ...current, revision: parentRevision };
      appliedSettingsRef.current = advanced;
      return advanced;
    });
  }, [parentRevision]);

  const refresh = useCallback(async (signal?: AbortSignal, force = false) => {
    const sequence = ++refreshSequence.current;
    let retriedStaleSnapshot = false;
    let requestForce = force;
    try {
      while (true) {
        const next = await api.agentSettings(signal, requestForce);
        if (sequence !== refreshSequence.current || signal?.aborted) return;
        const knownRevision = parentRevisionRef.current;
        // 若服务端返回了比父页面更旧的快照（包括路由返回时的初始缓存），
        // 不能只把 revision 改大后继续显示旧 Agent 行；清缓存并有界重试一次。
        if (
          knownRevision !== null
          && next.revision < knownRevision
          && (
            appliedSettingsRef.current === null
            || initialCacheValidationPendingRef.current
          )
        ) {
          if (retriedStaleSnapshot) {
            setMessageKind("error");
            setMessageAgent(null);
            setMessage("Agent 配置正在同步，请稍后刷新");
            return;
          }
          retriedStaleSnapshot = true;
          clearSettingsCache();
          requestForce = true;
          continue;
        }
        initialCacheValidationPendingRef.current = false;
        // 普通刷新期间若拿到旧快照，保留当前本地快照即可；只有初始缓存
        // 校验阶段才重试一次，避免每次父 revision 变化都制造额外 GET。
        apply(next);
        return;
      }
    } catch (error) {
      if (signal?.aborted || sequence !== refreshSequence.current) return;
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setMessageKind("error");
      setMessageAgent(null);
      setMessage(errorMessage(error));
    }
  }, [apply, onSignedOut]);

  useEffect(() => {
    if (shouldLoad) return undefined;
    const node = sectionRef.current;
    if (!node || typeof IntersectionObserver === "undefined") {
      setShouldLoad(true);
      return undefined;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setShouldLoad(true);
          observer.disconnect();
        }
      },
      { rootMargin: "480px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [shouldLoad]);

  useEffect(() => {
    // Agent 保存/测试/启停期间，父页面的“刷新设置”不能抢占当前操作的
    // request sequence；否则响应会被视为旧请求，busy 状态也无法收尾。
    // 操作结束后 busy 变化会再次触发这里，补取一次最新快照。
    if (!shouldLoad || busy) return undefined;
    const controller = new AbortController();
    // 未过期缓存直接复用；父页面的显式刷新会先清缓存并递增
    // refreshRequest，因此这里仍会读取最新 Agent 配置。
    void refresh(controller.signal);
    return () => controller.abort();
  }, [busy, refresh, refreshRequest, shouldLoad]);

  useEffect(() => {
    if (!message || messageKind !== "success") return undefined;
    const timer = window.setTimeout(() => setMessage(""), 4000);
    return () => window.clearTimeout(timer);
  }, [message, messageKind]);

  function updateDraft(agent: ReviewAgent, field: keyof AgentDraft, value: string | boolean) {
    setDrafts((current) => {
      const previousDraft = current[agent];
      if (!previousDraft) return current;
      const nextDraft = { ...previousDraft, [field]: value };
      const saved = appliedSettingsRef.current?.agents.find(
        (item) => item.agent === agent,
      );
      if (saved && agentDraftDirty(saved, nextDraft)) {
        if (draftRevisionRef.current[agent] === undefined) {
          draftRevisionRef.current[agent] =
            parentRevisionRef.current
              ?? appliedSettingsRef.current?.revision
              ?? 0;
        }
      } else {
        delete draftRevisionRef.current[agent];
      }
      return { ...current, [agent]: nextDraft };
    });
  }

  async function save(agent: ReviewAgent) {
    const item = settings?.agents.find((candidate) => candidate.agent === agent);
    const draft = drafts[agent];
    if (!settings || !item || !draft) return;
    const revision = draftRevisionRef.current[agent]
      ?? Math.max(
        settings.revision,
        parentRevisionRef.current ?? settings.revision,
      );
    const sequence = ++refreshSequence.current;
    setBusy("save-" + agent);
    setMessage("");
    setMessageAgent(agent);
    try {
      const response = await api.updateAgent(agent, {
        expected_revision: revision,
        provider: draft.provider,
        model: draft.model.trim(),
        use_shared_connection: draft.useSharedConnection,
        model_override: draft.modelOverride.trim() || null,
        api_protocol: draft.apiProtocol,
        api_base_url: draft.apiBaseUrl.trim() || null,
        api_key: draft.apiKey.trim() || null,
        clear_api_key: draft.clearApiKey,
        reasoning_effort: draft.reasoningEffort,
        context_window_tokens: agentNumber(draft.contextWindowTokens, "模型总容量", true),
        max_output_tokens: agentNumber(draft.maxOutputTokens, "回答上限", true),
        max_batch_input_tokens: agentNumber(draft.maxBatchInputTokens, "每批代码量", true),
        connect_timeout_seconds: agentNumber(draft.connectTimeoutSeconds, "连接超时"),
        read_timeout_seconds: agentNumber(draft.readTimeoutSeconds, "回答超时"),
        write_timeout_seconds: agentNumber(draft.writeTimeoutSeconds, "发送超时"),
        pool_timeout_seconds: agentNumber(draft.poolTimeoutSeconds, "连接排队超时"),
        max_retries: agentNumber(draft.maxRetries, "重试次数", true),
        input_usd_per_million: draft.inputPrice.trim() || null,
        output_usd_per_million: draft.outputPrice.trim() || null,
        cache_read_usd_per_million: draft.cacheReadPrice.trim() || null,
        cache_write_usd_per_million: draft.cacheWritePrice.trim() || null,
      });
      if (sequence !== refreshSequence.current) return;
      const applied = apply(response, agent);
      if (!applied) {
        clearSettingsCache();
        const refreshSequenceBefore = refreshSequence.current;
        await refresh(undefined, true);
        if (refreshSequence.current === refreshSequenceBefore + 1) setBusy("");
        return;
      }
      setMessageKind("success");
      setMessage(agentLabels[agent].title + "配置已保存");
    } catch (error) {
      if (sequence !== refreshSequence.current) return;
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setMessageKind("error");
      setMessage(errorMessage(error));
    } finally {
      if (sequence === refreshSequence.current) setBusy("");
    }
  }

  async function test(agent: ReviewAgent) {
    if (!settings) return;
    const sequence = ++refreshSequence.current;
    setBusy("test-" + agent);
    setMessage("");
    setMessageAgent(agent);
    try {
      const response = await api.testAgent(
        agent,
        Math.max(settings.revision, parentRevisionRef.current ?? settings.revision),
      );
      if (sequence !== refreshSequence.current) return;
      const applied = apply(response);
      if (!applied) {
        clearSettingsCache();
        const refreshSequenceBefore = refreshSequence.current;
        await refresh(undefined, true);
        if (refreshSequence.current === refreshSequenceBefore + 1) setBusy("");
        return;
      }
      setMessageKind("success");
      setMessage(agentLabels[agent].title + "连接测试通过");
    } catch (error) {
      if (sequence !== refreshSequence.current) return;
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setMessageKind("error");
      setMessage(errorMessage(error));
      clearSettingsCache();
      const refreshSequenceBefore = refreshSequence.current;
      await refresh();
      // refresh() 会为新快照占用一个序列号；只有它没有被其他操作抢占时，
      // 才能结束本次测试的 busy 状态。
      if (refreshSequence.current === refreshSequenceBefore + 1) setBusy("");
    } finally {
      if (sequence === refreshSequence.current) setBusy("");
    }
  }

  async function setEnabled(agent: ReviewAgent, enabled: boolean) {
    if (!settings) return;
    const sequence = ++refreshSequence.current;
    setBusy("enabled-" + agent);
    setMessage("");
    setMessageAgent(agent);
    try {
      const response = await api.setAgentEnabled(
        agent,
        enabled,
        Math.max(settings.revision, parentRevisionRef.current ?? settings.revision),
      );
      if (sequence !== refreshSequence.current) return;
      const applied = apply(response);
      if (!applied) {
        clearSettingsCache();
        const refreshSequenceBefore = refreshSequence.current;
        await refresh(undefined, true);
        if (refreshSequence.current === refreshSequenceBefore + 1) setBusy("");
        return;
      }
      setMessageKind("success");
      setMessage(agentLabels[agent].title + (enabled ? "已启用" : "已停用"));
    } catch (error) {
      if (sequence !== refreshSequence.current) return;
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setMessageKind("error");
      setMessage(errorMessage(error));
    } finally {
      if (sequence === refreshSequence.current) setBusy("");
    }
  }

  if (!settings) {
    return (
      <section ref={sectionRef} className="agent-settings-section">
        <div className="settings-section-heading"><div><span className="settings-eyebrow">固定审查 DAG</span><h2>独立 Agent 配置</h2></div></div>
        {message && <div className={"settings-message is-" + messageKind}>{message}</div>}
        {!message && <div className="settings-loading">正在读取 Agent 配置...</div>}
      </section>
    );
  }

  return (
    <section ref={sectionRef} className="agent-settings-section">
      <div className="settings-section-heading">
        <div><span className="settings-eyebrow">固定审查 DAG</span><h2>Agent 配置</h2><p>三路审查并行执行，可分别配置，也可共用 AI 设置页当前启用的公共连接。</p></div>
        <span className="settings-summary-value">配置版本 r{settings.revision}</span>
      </div>
      {message && messageAgent === null && <div className={"settings-message is-" + messageKind}>{message}</div>}
      <div className="agent-settings-grid">
        {agentOrder.map((agent) => {
          const item = settings.agents.find((candidate) => candidate.agent === agent);
          const draft = drafts[agent];
          if (!item || !draft) return null;
          const dirty = agentDraftDirty(item, draft);
          const sharedConnection = draft.useSharedConnection;
          const protocolOptions = draft.provider === "anthropic"
            ? [["messages", "Anthropic Messages"]] as Array<[string, string]>
            : [["chat_completions", "通用兼容 / Chat Completions"], ["responses", "OpenAI Responses"]] as Array<[string, string]>;
          return (
            <article className="agent-settings-card" key={agent}>
              <div className="agent-settings-card-heading">
                <div className="agent-card-title-row">
                  <span className={`agent-mark is-${agent}`} aria-hidden="true">{agentIcons[agent]}</span>
                  <div><strong>{agentLabels[agent].title}</strong><small>{agentLabels[agent].description}</small></div>
                </div>
                <div className="agent-settings-card-badges">
                  {dirty && <span className="settings-unsaved-badge">未保存</span>}
                  <span className={"settings-test-badge is-" + item.test_status}><span className="settings-status-dot" />{testStatusLabels[item.test_status]}</span>
                </div>
              </div>
              <div className="agent-settings-status">
                <span>{item.api_key_configured ? "密钥 " + item.api_key_mask : "未保存密钥"}</span>
                <span>{item.enabled ? "运行已启用" : "已停用"}</span>
                <span>{sharedConnection ? (item.shared_connection_ready ? "公共连接已就绪" : "公共连接待配置或测试") : "独立连接"}</span>
                <span>模型上下文：自动</span>
                <span>代码分批：自动</span>
                <span>截断恢复：自动拆批</span>
              </div>
              <div className="agent-settings-fields">
                <label className={`agent-settings-shared-toggle${sharedConnection ? " is-on" : ""}`}>
                  <input type="checkbox" checked={sharedConnection} onChange={(event) => {
                    updateDraft(agent, "useSharedConnection", event.target.checked);
                    if (event.target.checked) updateDraft(agent, "apiKey", "");
                  }} />
                  <span className="agent-shared-switch" aria-hidden="true" />
                  <span className="agent-shared-copy">
                    <strong>使用公共连接配置</strong>
                    <small>{item.shared_connection_configured ? "沿用「模型服务」中已启用的提供商、地址、协议和密钥" : "公共连接未就绪，请先在「模型服务」保存、测试并启用"}</small>
                  </span>
                </label>
                <label><span>提供商</span><select value={draft.provider} disabled={sharedConnection} onChange={(event) => {
                  const provider = event.target.value as AiAgentSettings["provider"];
                  updateDraft(agent, "provider", provider);
                  updateDraft(agent, "apiProtocol", provider === "anthropic" ? "messages" : "chat_completions");
                }}><option value="openai">OpenAI 兼容</option><option value="anthropic">Anthropic 兼容</option></select></label>
                {sharedConnection
                  ? <label><span>模型覆盖（可选）</span><input value={draft.modelOverride} maxLength={200} onChange={(event) => updateDraft(agent, "modelOverride", event.target.value)} placeholder="留空使用公共默认模型" /></label>
                  : <label><span>模型 ID</span><input value={draft.model} maxLength={200} onChange={(event) => updateDraft(agent, "model", event.target.value)} placeholder="例如 gpt-4.1-mini" /></label>}
                <label><span>Base URL（可选）</span><input value={draft.apiBaseUrl} maxLength={500} disabled={sharedConnection} onChange={(event) => updateDraft(agent, "apiBaseUrl", event.target.value)} placeholder="留空使用官方地址" /></label>
                <label><span>中转协议</span><select value={draft.apiProtocol} disabled={sharedConnection} onChange={(event) => updateDraft(agent, "apiProtocol", event.target.value)}>{protocolOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                <label className="agent-settings-key"><span>API Key</span><input type="password" value={draft.apiKey} onChange={(event) => updateDraft(agent, "apiKey", event.target.value)} placeholder={item.api_key_configured ? item.api_key_mask ?? "已保存密钥" : "粘贴 API Key"} autoComplete="new-password" disabled={sharedConnection || draft.clearApiKey} /><small>{sharedConnection ? "由公共连接统一提供；不会在此重复保存。" : "留空保留原密钥；只显示掩码。"}</small></label>
                {!sharedConnection && <label className="agent-settings-check"><input type="checkbox" checked={draft.clearApiKey} onChange={(event) => updateDraft(agent, "clearApiKey", event.target.checked)} disabled={!item.api_key_configured} /><span>保存时删除密钥</span></label>}
                <details className="agent-settings-advanced">
                  <summary>连接与重试</summary>
                  <div>
                    <label><span>推理档位</span><select value={draft.reasoningEffort} disabled={sharedConnection} onChange={(event) => updateDraft(agent, "reasoningEffort", event.target.value)}><option value="none">自动</option><option value="low">轻量</option><option value="medium">标准</option><option value="high">深入</option><option value="max">极致</option></select></label>
                    <label><span>连接超时（秒）</span><input type="number" min={0.1} max={3600} step={0.1} value={draft.connectTimeoutSeconds} disabled={sharedConnection} onChange={(event) => updateDraft(agent, "connectTimeoutSeconds", event.target.value)} /></label>
                    <label><span>回答超时（秒）</span><input type="number" min={0.1} max={3600} step={0.1} value={draft.readTimeoutSeconds} disabled={sharedConnection} onChange={(event) => updateDraft(agent, "readTimeoutSeconds", event.target.value)} /></label>
                    <label><span>发送超时（秒）</span><input type="number" min={0.1} max={3600} step={0.1} value={draft.writeTimeoutSeconds} disabled={sharedConnection} onChange={(event) => updateDraft(agent, "writeTimeoutSeconds", event.target.value)} /></label>
                    <label><span>连接排队超时（秒）</span><input type="number" min={0.1} max={3600} step={0.1} value={draft.poolTimeoutSeconds} disabled={sharedConnection} onChange={(event) => updateDraft(agent, "poolTimeoutSeconds", event.target.value)} /></label>
                    <label><span>最多重试次数</span><input type="number" min={0} max={10} step={1} value={draft.maxRetries} onChange={(event) => updateDraft(agent, "maxRetries", event.target.value)} /></label>
                  </div>
                </details>
              </div>
              <details className="agent-price-settings">
                  <summary>费用估算价格</summary>
                  <p className="settings-field-note">单独覆盖时同时填写输入和输出价格。留空仅在模型名称一致时继承公共价格；仅改价格保留原连接验证。</p>
                  <div className="agent-settings-fields">
                    <label><span>输入（美元 / 百万 Token）</span><input aria-label={`${agentLabels[agent].title}输入单价`} type="number" min={0} max={1000000} step="any" disabled={Boolean(busy)} value={draft.inputPrice} onChange={event => updateDraft(agent, "inputPrice", event.target.value)} /></label>
                    <label><span>输出（美元 / 百万 Token）</span><input aria-label={`${agentLabels[agent].title}输出单价`} type="number" min={0} max={1000000} step="any" disabled={Boolean(busy)} value={draft.outputPrice} onChange={event => updateDraft(agent, "outputPrice", event.target.value)} /></label>
                    <label><span>缓存读取（可选）</span><input aria-label={`${agentLabels[agent].title}缓存读取单价`} type="number" min={0} max={1000000} step="any" disabled={Boolean(busy)} value={draft.cacheReadPrice} onChange={event => updateDraft(agent, "cacheReadPrice", event.target.value)} /></label>
                    <label><span>缓存写入（可选）</span><input aria-label={`${agentLabels[agent].title}缓存写入单价`} type="number" min={0} max={1000000} step="any" disabled={Boolean(busy)} value={draft.cacheWritePrice} onChange={event => updateDraft(agent, "cacheWritePrice", event.target.value)} /></label>
                  </div>
              </details>
              <div className="agent-settings-actions">
                <button className="settings-primary-btn" type="button" onClick={() => void save(agent)} disabled={Boolean(busy) || !dirty}>{busy === "save-" + agent ? "保存中..." : "保存配置"}</button>
                <button className="settings-secondary-btn" type="button" onClick={() => void test(agent)} disabled={Boolean(busy) || dirty || !item.configured || !item.api_key_configured} title={dirty ? "请先保存当前修改" : "测试已保存配置"}>{busy === "test-" + agent ? "测试中..." : "测试连接"}</button>
                <button className={"settings-secondary-btn " + (item.enabled ? "" : "is-activate")} type="button" onClick={() => void setEnabled(agent, !item.enabled)} disabled={Boolean(busy) || (!item.enabled && (dirty || !item.configured || item.test_status !== "succeeded"))} title={!item.enabled && dirty ? "请先保存并重新测试当前修改" : undefined}>{busy === "enabled-" + agent ? "处理中..." : item.enabled ? "停用 Agent" : "启用 Agent"}</button>
                {message && messageAgent === agent && <div className={`settings-inline-feedback is-${messageKind}`} role="status">{message}</div>}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
