import { useCallback, useEffect, useState } from "react";

import { api, ApiError } from "./api";
import {
  batchInputOptions,
  contextWindowOptions,
  outputTokenOptions,
} from "./settings-drafts";
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
}
const agentLabels: Record<ReviewAgent, { title: string; description: string }> = {
  security: { title: "安全审查", description: "关注权限、注入、敏感数据和可靠性风险。" },
  convention: { title: "规范审查", description: "检查仓库约定、编码风格和接口一致性。" },
  logic: { title: "逻辑审查", description: "检查业务逻辑、边界条件和回归风险。" },
  summary: { title: "汇总审查", description: "合并前三路结果并生成最终审查报告。" },
};

const agentOrder: ReviewAgent[] = ["security", "convention", "logic", "summary"];

function agentDraft(settings: AiAgentSettings): AgentDraft {
  return {
    provider: settings.provider,
    model: settings.model,
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
  };
}

function agentDraftDirty(settings: AiAgentSettings, draft: AgentDraft): boolean {
  const saved = agentDraft(settings);
  return (
    draft.provider !== saved.provider
    || draft.model !== saved.model
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
  onSignedOut,
}: {
  refreshRequest: number;
  onSignedOut: (message?: string) => void;
}) {
  const [settings, setSettings] = useState<AiAgentSettingsResponse | null>(null);
  const [drafts, setDrafts] = useState<Partial<Record<ReviewAgent, AgentDraft>>>({});
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [messageKind, setMessageKind] = useState<"success" | "error">("success");
  const [messageAgent, setMessageAgent] = useState<ReviewAgent | null>(null);

  const apply = useCallback((next: AiAgentSettingsResponse) => {
    setSettings(next);
    setDrafts(
      Object.fromEntries(
        next.agents.map((item) => [item.agent, agentDraft(item)]),
      ) as Record<ReviewAgent, AgentDraft>,
    );
  }, []);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      apply(await api.agentSettings(signal));
    } catch (error) {
      if (signal?.aborted) return;
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
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh, refreshRequest]);

  useEffect(() => {
    if (!message || messageKind !== "success") return undefined;
    const timer = window.setTimeout(() => setMessage(""), 4000);
    return () => window.clearTimeout(timer);
  }, [message, messageKind]);

  function updateDraft(agent: ReviewAgent, field: keyof AgentDraft, value: string | boolean) {
    setDrafts((current) => ({
      ...current,
      [agent]: { ...current[agent]!, [field]: value },
    }));
  }

  async function save(agent: ReviewAgent) {
    const item = settings?.agents.find((candidate) => candidate.agent === agent);
    const draft = drafts[agent];
    if (!settings || !item || !draft) return;
    const revision = settings.revision;
    setBusy("save-" + agent);
    setMessage("");
    setMessageAgent(agent);
    try {
      const response = await api.updateAgent(agent, {
        expected_revision: revision,
        provider: draft.provider,
        model: draft.model.trim(),
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
      });
      apply(response);
      setMessageKind("success");
      setMessage(agentLabels[agent].title + "配置已保存");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setMessageKind("error");
      setMessage(errorMessage(error));
    } finally {
      setBusy("");
    }
  }

  async function test(agent: ReviewAgent) {
    if (!settings) return;
    setBusy("test-" + agent);
    setMessage("");
    setMessageAgent(agent);
    try {
      apply(await api.testAgent(agent, settings.revision));
      setMessageKind("success");
      setMessage(agentLabels[agent].title + "连接测试通过");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setMessageKind("error");
      setMessage(errorMessage(error));
      await refresh();
    } finally {
      setBusy("");
    }
  }

  async function setEnabled(agent: ReviewAgent, enabled: boolean) {
    if (!settings) return;
    setBusy("enabled-" + agent);
    setMessage("");
    setMessageAgent(agent);
    try {
      apply(await api.setAgentEnabled(agent, enabled, settings.revision));
      setMessageKind("success");
      setMessage(agentLabels[agent].title + (enabled ? "已启用" : "已停用"));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        onSignedOut("登录状态已失效，请重新登录");
        return;
      }
      setMessageKind("error");
      setMessage(errorMessage(error));
    } finally {
      setBusy("");
    }
  }

  if (!settings) {
    return (
      <section className="agent-settings-section">
        <div className="settings-section-heading"><div><span className="settings-eyebrow">固定审查 DAG</span><h2>独立 Agent 配置</h2></div></div>
        {message && <div className={"settings-message is-" + messageKind}>{message}</div>}
        {!message && <div className="settings-loading">正在读取 Agent 配置...</div>}
      </section>
    );
  }

  return (
    <section className="agent-settings-section">
      <div className="settings-section-heading">
        <div><span className="settings-eyebrow">固定审查 DAG</span><h2>独立 Agent 配置</h2><p>三路审查并行执行，汇总 Agent 单独使用自己的模型和密钥。</p></div>
        <span className="settings-summary-value">配置版本 r{settings.revision}</span>
      </div>
      {message && messageAgent === null && <div className={"settings-message is-" + messageKind}>{message}</div>}
      <div className="agent-settings-grid">
        {agentOrder.map((agent) => {
          const item = settings.agents.find((candidate) => candidate.agent === agent);
          const draft = drafts[agent];
          if (!item || !draft) return null;
          const dirty = agentDraftDirty(item, draft);
          const protocolOptions = draft.provider === "anthropic"
            ? [["messages", "Anthropic Messages"]] as Array<[string, string]>
            : [["chat_completions", "通用兼容 / Chat Completions"], ["responses", "OpenAI Responses"]] as Array<[string, string]>;
          return (
            <article className="agent-settings-card" key={agent}>
              <div className="agent-settings-card-heading">
                <div><strong>{agentLabels[agent].title}</strong><small>{agentLabels[agent].description}</small></div>
                <div className="agent-settings-card-badges">
                  {dirty && <span className="settings-unsaved-badge">未保存</span>}
                  <span className={"settings-test-badge is-" + item.test_status}><span className="settings-status-dot" />{testStatusLabels[item.test_status]}</span>
                </div>
              </div>
              <div className="agent-settings-status">
                <span>{item.api_key_configured ? "密钥 " + item.api_key_mask : "未保存密钥"}</span>
                <span>{item.enabled ? "运行已启用" : "已停用"}</span>
              </div>
              <div className="agent-settings-fields">
                <label><span>提供商</span><select value={draft.provider} onChange={(event) => {
                  const provider = event.target.value as AiAgentSettings["provider"];
                  updateDraft(agent, "provider", provider);
                  updateDraft(agent, "apiProtocol", provider === "anthropic" ? "messages" : "chat_completions");
                }}><option value="openai">OpenAI 兼容</option><option value="anthropic">Anthropic 兼容</option></select></label>
                <label><span>模型 ID</span><input value={draft.model} maxLength={200} onChange={(event) => updateDraft(agent, "model", event.target.value)} placeholder="例如 gpt-4.1-mini" /></label>
                <label><span>Base URL（可选）</span><input value={draft.apiBaseUrl} maxLength={500} onChange={(event) => updateDraft(agent, "apiBaseUrl", event.target.value)} placeholder="留空使用官方地址" /></label>
                <label><span>中转协议</span><select value={draft.apiProtocol} onChange={(event) => updateDraft(agent, "apiProtocol", event.target.value)}>{protocolOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                <label className="agent-settings-key"><span>API Key</span><input type="password" value={draft.apiKey} onChange={(event) => updateDraft(agent, "apiKey", event.target.value)} placeholder={item.api_key_configured ? item.api_key_mask ?? "已保存密钥" : "粘贴 API Key"} autoComplete="new-password" disabled={draft.clearApiKey} /><small>留空保留原密钥；只显示掩码。</small></label>
                <label className="agent-settings-check"><input type="checkbox" checked={draft.clearApiKey} onChange={(event) => updateDraft(agent, "clearApiKey", event.target.checked)} disabled={!item.api_key_configured} /><span>保存时删除密钥</span></label>
                <label><span>总上下文窗口</span><select value={draft.contextWindowTokens} onChange={(event) => updateDraft(agent, "contextWindowTokens", event.target.value)}>{contextWindowOptions(draft.contextWindowTokens).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><small>模型总容量，与每批输入分别设置。</small></label>
                <label><span>每批输入上限</span><select value={draft.maxBatchInputTokens} onChange={(event) => updateDraft(agent, "maxBatchInputTokens", event.target.value)}>{batchInputOptions(draft.contextWindowTokens, draft.maxBatchInputTokens).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><small>推荐 64K，过大提交会自动分批。</small></label>
                <label><span>回答上限</span><select value={draft.maxOutputTokens} onChange={(event) => updateDraft(agent, "maxOutputTokens", event.target.value)}>{outputTokenOptions(draft.contextWindowTokens, draft.maxOutputTokens).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                <label><span>推理档位</span><select value={draft.reasoningEffort} onChange={(event) => updateDraft(agent, "reasoningEffort", event.target.value)}><option value="none">自动</option><option value="low">轻量</option><option value="medium">标准</option><option value="high">深入</option><option value="max">极致</option></select></label>
                <details className="agent-settings-advanced">
                  <summary>超时与重试</summary>
                  <div>
                    <label><span>连接超时（秒）</span><input type="number" min={0.1} max={3600} step={0.1} value={draft.connectTimeoutSeconds} onChange={(event) => updateDraft(agent, "connectTimeoutSeconds", event.target.value)} /></label>
                    <label><span>回答超时（秒）</span><input type="number" min={0.1} max={3600} step={0.1} value={draft.readTimeoutSeconds} onChange={(event) => updateDraft(agent, "readTimeoutSeconds", event.target.value)} /></label>
                    <label><span>发送超时（秒）</span><input type="number" min={0.1} max={3600} step={0.1} value={draft.writeTimeoutSeconds} onChange={(event) => updateDraft(agent, "writeTimeoutSeconds", event.target.value)} /></label>
                    <label><span>连接排队超时（秒）</span><input type="number" min={0.1} max={3600} step={0.1} value={draft.poolTimeoutSeconds} onChange={(event) => updateDraft(agent, "poolTimeoutSeconds", event.target.value)} /></label>
                    <label><span>最多重试次数</span><input type="number" min={0} max={10} step={1} value={draft.maxRetries} onChange={(event) => updateDraft(agent, "maxRetries", event.target.value)} /></label>
                  </div>
                </details>
              </div>
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
