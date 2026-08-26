# 模型审查契约 v1

本文固定 Review Plan 到 Finding 的供应商无关边界。当前固定 DAG 由安全、规范、逻辑和汇总
四个 Agent 生成候选；结果保存后进入人工批准门，批准后可显式发布 GitHub PR 汇总评论。
GitHub Check、自动证据复核和行内评论仍未实现。

## 1. 供应商协议

Worker 通过统一 `ModelReviewer` 边界支持三种官方 HTTP 协议：

| 供应商 | 接口 | 严格结构化输出 |
| --- | --- | --- |
| OpenAI Responses | `POST /v1/responses` | `text.format.type = json_schema`，并启用 `strict` |
| OpenAI Chat Completions | `POST /v1/chat/completions` | `response_format.type = json_schema`，并启用 `strict` |
| Anthropic | `POST /v1/messages` | `output_config.format.type = json_schema` |

两种 OpenAI 请求都设置 `store: false`。Responses 从 `output[].content[]` 的 `output_text`
读取，并显式拒绝 `refusal` 和 `incomplete`；Chat Completions 从
`choices[0].message.content` 读取，只接受 `finish_reason = stop`，并拒绝 `length`、
`content_filter` 和 `message.refusal`。Anthropic 请求固定 `anthropic-version: 2023-06-01`，只接受
`stop_reason = end_turn`，拒绝 `refusal`、`max_tokens` 和其他未知结束原因。

中转站兼容降级只在 HTTP `400`/`422` 且安全截断后的错误明确同时指出“不支持”和已知可选
字段时发生。允许的变化只有：移除推理参数、把 Chat Completions 的
`max_completion_tokens` 改为 `max_tokens`，或把 JSON Schema 格式降为 JSON Object/移除旧
Anthropic 格式字段。鉴权、权限、限流、普通校验错误和任意 4xx 都不会触发降级；重试体使用
稳定签名防止死循环。降级后响应仍必须通过本地 Pydantic 严格校验，不会接受额外字段或不合法
Finding。错误正文、API Key 和完整请求体不持久化。

实现所依据的官方文档：

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI Responses create](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [OpenAI Chat Completions create](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [Anthropic Structured Outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- [Anthropic Messages create](https://platform.claude.com/docs/en/api/messages/create)

## 2. 调用边界

- 一份 Review Plan 包含本次提交的全部可审查 Review Unit，不再按文件数或总字节预算静默省略。
- Worker 按模型上下文窗口、输出预留、5% 安全余量和 HTTP 请求大小确定性分批；单文件仍超限时
  按行切片，并保留原 `unit_key` 供 Finding 归属校验。
- 单批输入默认上限为 64K Token。模型标称 1M 上下文只决定总容量上界，不会让中转站一次接收
  整个大提交；管理员可按网关稳定性调低或调高单批上限。
- 每个 Agent 最多生成并持久化 3000 个批次；超过时明确终止本阶段并返回安全错误，不会静默遗漏
  后续文件或在 Worker 恢复时截断批次。
- 每批只携带本批引用的规则，Unit 通过 `rule_paths` 引用规则，避免同一批内重复上下文。
- 没有 Review Unit 时不调用外部 API，保存一条 `skipped`、零 Token、零成本的完成审计。
- Prompt 明确把仓库规则和 diff 视为不可信数据；模型不得执行代码、访问网络或改变输出协议。
- 请求和响应都有字节上限、连接/读/写/连接池超时，HTTP 不在数据库事务中执行。
- 每批规划、准备、请求发出、响应返回、完成或失败状态写入结构化 Outbox 事件，记录批次、
  文件、HTTP 状态、请求 ID、Token、耗时与可重试错误；不保存或伪造模型私密
  Chain-of-Thought。

## 3. Finding 所有权

模型只允许返回严重度、类别、Unit、可选位置、标题、证据、影响、建议、测试建议、置信度和
规则引用。以下字段只能由平台生成：

- `head_sha` 和位置的 `blob_sha`；
- 不依赖行号的稳定 `fingerprint`；
- `in_diff`；
- `verification_status`。

模型引用的 `unit_key`、文件和规则必须存在于当前计划。平台补齐身份后统一保存为
`verification_status = unverified`、`in_diff = false`。模型阶段成功保存后，旧队列兼容字段
`execution_status` 进入 `completed`，真实 `workflow_status` 进入 `awaiting_approval`。结果立即
显示在管理界面；人工确认或忽略更新候选标记，批准整份结果后还要单独点击发布。

## 4. 幂等、版本和重试

`review_plans.model_review_completed_at` 是模型阶段完成标记，`model_calls.review_plan_id` 有唯一
约束。队列只领取没有完成标记的计划。模型阶段使用独立 `model_attempt_count`，不会消耗前面
GitHub 上下文和规划阶段的重试次数；人工重试产生新的事件身份，不会被旧 Outbox 键阻断。

保存模型结果时重新锁定任务和计划，并检查租约、`review_run_id`、`plan_fingerprint`、
`review_version_key` 和 `head_sha`。发现更新 SHA 时，旧任务进入 `superseded`，调用结果和
Finding 都不写入数据库。

## 5. 用量和成本

`model_calls` 保存一条兼容汇总审计。四 Agent 路径中，供应商、实际 API 协议、精确模型名、
Prompt 版本、HTTP 状态和请求 ID 代表最终汇总 Agent；耗时、标准输入 Token、输出 Token、缓存
读取/写入 Token、推理 Token、成本和 Finding 数量为四路合计。每个 Agent/批次的真实配置、
请求 ID 和计量保存在 `model_review_batches` 与结构化事件中。没有 Review Unit 时总记录为
`skipped`、零 Token、零成本且没有 HTTP 状态。

`model_calls.configuration_revision` 保存本次调用使用的动态配置版本。价格不写死在代码中，管理
界面以“美元/百万 Token”保存，计算时使用 `Decimal`，最终保存整数微美元。未配置价格，或出现
没有对应费率的缓存 Token 时，成本保存为 `NULL`，不能伪造为 0。

新版四 Agent API Key 分别写入 `ai_agent_secrets`；未配置任何 Agent 时，旧单模型兼容路径仍
使用 `ai_provider_secrets`。两者都使用 AES-256-GCM 和供应商绑定的附加认证数据加密。加密
主密钥仍由 `OPENREVIEWER_AI_CONFIG_KEY` 或
`OPENREVIEWER_AI_CONFIG_KEY_FILE` 提供，不能放进数据库。API 响应只返回“已配置”和末四位
掩码；明文只在 API 保存请求和 Worker 当前模型请求的内存快照中短暂存在，不进入 Prompt、
Outbox、审计或日志。

## 6. 数据访问规模

Worker 每轮以固定次数的有界查询批量读取四 Agent 配置和密钥，按 revision 缓存 HTTP Client；
配置变化后关闭旧 Client。模型输入使用三次有界查询读取计划元数据、规则和 Unit，查询次数为
`O(1)`；这样避免两个独立
一对多集合 JOIN 后产生规则数乘 Unit 数的笛卡尔积。Finding 使用一次批量 INSERT。模型 HTTP
调用次数为 `O(批次数)`，不是 `O(文件数)`：不同上下文批次无法通过通用 OpenAI/Anthropic 同步
协议合成一次请求。默认单批输入上限为 64K Token，大提交会稳定拆成多批。每批进度各使用一个
短事务，远程调用不在事务内。固定 DAG、RAG 和恢复细节见
[agent-workflow.md](agent-workflow.md)。
