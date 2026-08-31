# 动态 AI 设置契约

## 1. 配置边界

管理员可以分别维护安全、规范、逻辑和汇总四个 Agent。每个 Agent 都有独立的 OpenAI 或
Anthropic 供应商、模型 ID、API Key、可选 HTTPS API Base URL（支持中转站 `/v1` 前缀）、
协议、上下文窗口、单批输入/输出 Token 上限、推理档位、HTTP 超时和重试次数。OpenAI 可选
`responses` 或 `chat_completions`；Anthropic 固定使用 `messages`。

Base URL 的路径前缀会原样保留：例如填写 `https://relay.example/api/v1`，请求会发送到
`https://relay.example/api/v1/...`，不会因地址末尾没有斜杠而丢失 `/api/v1`。

旧版供应商级设置暂时保留兼容：只有完全没有 Agent 配置时，Worker 才使用已激活的旧单模型。
一旦任意 Agent 已配置，四个 Agent 必须全部保存密钥、通过当前配置指纹的连接测试并启用；部分
配置不会静默回退旧模型，也不会让一个 Agent 代替缺失节点。

数据库连接、管理员会话密钥、AI 配置加密主密钥、GitHub App 私钥、TLS、端口和镜像仍是进程
启动配置，不能通过管理页面修改。

## 2. 密钥保护

`OPENREVIEWER_AI_CONFIG_KEY` 或 `OPENREVIEWER_AI_CONFIG_KEY_FILE` 必须提供 32 个随机字节的
URL-safe Base64。API 与 Worker 使用相同主密钥和正整数 key version。

Agent API Key 使用 AES-256-GCM 加密，12 字节 nonce 每次随机生成，附加认证数据绑定供应商和
key version，并按 Agent 分行保存到 `ai_agent_secrets`。旧兼容密钥仍在
`ai_provider_secrets`。数据库只保存 ciphertext、nonce 和 key version。管理 API 从不返回
明文或密文，只返回 `api_key_configured` 与末四位掩码。审计只记录 `api_key` 字段发生变更。

## 3. 保存、测试与激活

每个 Agent 的流程固定为“保存草稿 -> 测试连接 -> 启用”：

1. 任何参数或 API Key 变化都会使该 Agent 原测试结果失效并自动停用，不影响其他 Agent。
2. 测试连接在数据库事务外发送一个最小严格结构化 Review Unit 请求，并保存成功或失败状态。
3. 只有当前完整配置指纹测试成功后才能启用，旧模型、旧协议或旧密钥的测试结果不能复用。
4. 每次保存、测试和启停都增加同一个全局 revision；正在运行的任务继续使用已领取快照，下一条
   AI 阶段任务使用新配置。

所有写接口提交 `expected_revision`。数据库锁定全局单例并检查 revision；并发修改冲突返回
`409 Conflict`，调用方必须重新读取。每次有效修改生成一条 `configuration_audits`，审计只含
操作者、动作、字段名、revision 和时间。

## 4. 管理接口

- `GET /api/v1/settings/ai`
- `GET /api/v1/settings/ai/agents`
- `PUT /api/v1/settings/ai/agents/{security|convention|logic|summary}`
- `POST /api/v1/settings/ai/agents/{agent}/test`
- `POST /api/v1/settings/ai/agents/{agent}/enabled`
- `PUT /api/v1/settings/ai/providers/{openai|anthropic}`
- `POST /api/v1/settings/ai/providers/{provider}/test`
- `POST /api/v1/settings/ai/providers/{provider}/activate`
- `PUT /api/v1/settings/ai/review-policy`
- `GET /api/v1/settings/audits?limit=50`

全部接口要求管理员会话。PUT/POST 还要求同源请求校验。审计查询限制为 1 到 100 条，不允许
无界读取。

## 5. Worker 生效语义

Worker 每轮先读取旧兼容设置，再用两次带 `IN` 和 `LIMIT 4` 的批量查询读取 Agent 配置与密钥，
查询次数固定为 `O(1)`，没有逐 Agent 查询。revision 不变时复用四个模型 HTTP Client；revision
变化时创建新快照并关闭旧 Client。

没有可用的完整四 Agent 配置或旧激活供应商时，队列仍可领取 GitHub 上下文和 CI 阶段，但
不会领取 `ready_for_review` 的规划或模型阶段，因此不会因配置未完成消耗任务重试次数。模型
调用将实际使用的 revision 写入 `model_calls.configuration_revision`。

`model_calls.api_protocol` 同时保存该次调用实际使用的协议。升级迁移会把已有 OpenAI 配置和
调用回填为 `responses`，已有 Anthropic 配置和调用回填为 `messages`。

上下文窗口表示“输入、输出和模型内部用量合计可使用的总 Token 数”，不是中转站单次请求的
稳妥承载量。单批输入默认限制为 64K Token；Worker 还会预留每批输出上限、5%（最低 4096
Token）安全余量，并同时遵守 HTTP 请求字节上限，最终采用这些边界中的最小值。超出的可审查
文件自动进入后续批次；单文件仍超限时按行切片。

`reasoning_effort = none` 表示不发送供应商可选推理参数，是中转站兼容性最好的默认值。管理员
只有在模型和中转站明确支持时才选择 `low`、`medium`、`high` 或 `max`。Responses 使用
`reasoning.effort`，Chat Completions 使用 `reasoning_effort`，Anthropic Messages 使用
`output_config.effort`；推理档位变化会使连接测试失效，保存后必须重新测试再启用。
