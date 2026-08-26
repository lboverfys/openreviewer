# 动态 AI 设置契约

## 1. 配置边界

管理员可以在设置页维护 OpenAI 与 Anthropic 的模型 ID、API Key、可选的 HTTPS API Base URL（支持
中转站的 `/v1` 前缀）、模型上下文窗口、每批输出 Token 上限、HTTP 超时、
请求/响应大小和四类 Token 单价。OpenAI 还可以选择
`responses` 或 `chat_completions`；Anthropic 固定使用 `messages`。两个供应商可以同时保存，
但同一时刻只有一个供应商处于激活状态，不做隐式故障切换。

数据库连接、管理员会话密钥、AI 配置加密主密钥、GitHub App 私钥、TLS、端口和镜像仍是进程
启动配置，不能通过管理页面修改。

## 2. 密钥保护

`OPENREVIEWER_AI_CONFIG_KEY` 或 `OPENREVIEWER_AI_CONFIG_KEY_FILE` 必须提供 32 个随机字节的
URL-safe Base64。API 与 Worker 使用相同主密钥和正整数 key version。

供应商 API Key 使用 AES-256-GCM 加密，12 字节 nonce 每次随机生成，附加认证数据绑定
供应商和 key version。数据库只保存 ciphertext、nonce 和 key version。管理 API 从不返回
明文或密文，只返回 `api_key_configured` 与末四位掩码。审计只记录 `api_key` 字段发生变更。

## 3. 保存、测试与激活

配置流程固定为“保存草稿 -> 测试连接 -> 激活”：

1. 保存模型参数、切换接口协议、修改 API Base URL 或替换/清除 API Key 会使原测试结果失效；若该供应商正在使用，
   同时取消激活。
2. 测试连接在数据库事务外发送一个最小严格结构化 Review Unit 请求，并保存成功或失败状态。
3. 只有当前完整配置指纹测试成功后才能激活，旧模型或旧密钥的测试结果不能复用。
4. 修改上下文窗口、旧版 Review Planning 兼容参数或成本统计单价不取消已激活供应商，但会产生
   新的全局 revision；这些参数不改变 API 连通性，成本单价也不会改变服务商实际计费。

所有写接口提交 `expected_revision`。数据库锁定全局单例并检查 revision；并发修改冲突返回
`409 Conflict`，调用方必须重新读取。每次有效修改生成一条 `configuration_audits`，审计只含
操作者、动作、字段名、revision 和时间。

## 4. 管理接口

- `GET /api/v1/settings/ai`
- `PUT /api/v1/settings/ai/providers/{openai|anthropic}`
- `POST /api/v1/settings/ai/providers/{provider}/test`
- `POST /api/v1/settings/ai/providers/{provider}/activate`
- `PUT /api/v1/settings/ai/review-policy`
- `GET /api/v1/settings/audits?limit=50`

全部接口要求管理员会话。PUT/POST 还要求同源请求校验。审计查询限制为 1 到 100 条，不允许
无界读取。

## 5. Worker 生效语义

Worker 每轮使用一次有索引 JOIN 读取全局设置、激活供应商参数和密钥，查询次数为 `O(1)`。
revision 不变时复用模型 HTTP Client；revision 变化时创建新快照并关闭旧 Client。正在执行的
任务继续使用领取时的快照，下一条 AI 阶段任务使用新配置。

没有激活供应商时，队列仍可领取 GitHub 上下文和 CI 阶段，但不会领取 `ready_for_review` 的
规划或模型阶段，因此不会因为尚未配置模型而消耗任务重试次数。模型调用将实际使用的 revision
写入 `model_calls.configuration_revision`。

`model_calls.api_protocol` 同时保存该次调用实际使用的协议。升级迁移会把已有 OpenAI 配置和
调用回填为 `responses`，已有 Anthropic 配置和调用回填为 `messages`。

上下文窗口表示“输入与输出合计可使用的 Token 数”，不是建议一次塞满的输入量。Worker 会先
预留每批输出上限，再保留 5%（最低 4096 Token）安全余量，并同时遵守 HTTP 请求字节上限。
超出的可审查文件自动进入后续批次；单文件仍超限时按行切片。DeepSeek 官方模型卡确认
DeepSeek V4 Flash 为 1M 上下文，迁移会把已知的该模型配置更新为 `1000000`。
