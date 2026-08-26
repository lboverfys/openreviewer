# 固定多 Agent 工作流契约

## 1. 固定 DAG

模型不能决定节点、并发数或外部副作用。当前工作流固定为：

```text
queued -> ci -> planning -> agent_batches
  -> security + convention + logic（最多三路并行）
  -> aggregating -> summary
  -> awaiting_approval
  -> approve -> approved -> awaiting_publish -> publish -> publishing -> completed
  -> reject -> rejected -> retry_stage(CI | planning | agent_batches | aggregating)
```

`security`、`convention` 和 `logic` 使用同一份 Review Plan，但拥有独立模型配置、批次、
请求和恢复状态。三路都成功后才进入 `aggregating`；此状态先用短事务写入数据库并产生
`review.model.aggregating_started`，随后才调用汇总 Agent。任一路失败或未启用时不运行汇总，
任务保留安全错误并按批次/任务重试规则处理。

没有可审查 Review Unit 时，三路适配器合法返回 `skipped`，工作流以零 Finding 正常完成，
不调用汇总模型；存在 Unit 时返回 `skipped` 则视为不完整结果。

## 2. 状态兼容

`workflow_status` 是固定 DAG 的真实节点。`execution_status` 暂时保留给旧队列领取协议，模型
结果写入后可能已经是 `completed`，而 `workflow_status` 仍是 `awaiting_approval`。管理 API、
前端动作和部署验收必须读取 `workflow_status`，不能用旧字段推断是否已经发布。

人工动作均经过显式状态边：

- `approve` 先记录 `awaiting_approval -> approved`，再由确定性自动边推进到
  `awaiting_publish`；两条状态边在同一事务中分别写入审计事件；
- `reject` 可从 `awaiting_approval` 或尚未发布的 `awaiting_publish` 进入 `rejected`；
- `pause` 保存来源节点并撤销当前租约，`resume` 回到保存的节点；
- `retry_stage` 只允许回到 CI、规划、Agent 批次或汇总阶段。回到 CI/规划会丢弃
  旧不可变计划；回到 Agent 批次会清除全部模型批次；回到汇总只清除汇总批次，
  已成功的安全、规范和逻辑批次会直接复用；
- `publish` 是单独的人工动作，不会因批准而自动发生。

## 3. 独立配置与恢复

四个 Agent 分别保存供应商、协议、模型、API Base URL、推理档位、上下文、单批输入上限、
输出上限、超时、重试数和独立加密 API Key。每份配置都必须经历“保存、真实连接测试、启用”。
一旦开始使用新版 Agent 配置，四个节点必须全部就绪；部分配置不会静默退回旧单模型。

`model_review_batches` 使用 `(review_plan_id, agent, batch_number)` 唯一键保存定义、尝试次数、
租约、安全错误和严格校验后的结果。成功批次在 Worker 重启后直接复用。每次模型 HTTP 发生在
数据库事务外；单批结果立即用短事务持久化，避免进程崩溃后重复调用和重复计费。

前三路候选按稳定业务身份去重，以严重度、置信度和指纹确定性排序，最多保留 200 条。汇总
Agent 只接收有界的结构化候选摘要，不能改变 DAG 或直接发布。最终 Finding 再由平台校验
Unit、文件、规则、SHA 和位置。

旧版 `model_calls` 仍为每个 Review Plan 保存一条兼容总记录：供应商、协议、模型和请求 ID
代表最终汇总 Agent；Token、成本和模型耗时是四路合计。每路真实身份与计量以批次记录和事件
为准。零 Unit 路径保存 `skipped` 总记录。

## 4. 版本化 RAG

Worker 镜像内置 `knowledge/*.md`。知识库只读取 UTF-8 Markdown 普通文件，不跟随符号链接；
默认最多 128 个文件、单文件 512 KiB、总计 5 MiB。文件按路径排序、按 Markdown 标题切块，
内容版本使用 SHA-256 前 16 位。首次读取后在当前 Worker 进程内缓存，不在 Agent 循环中重复
访问磁盘。

检索采用确定性词法匹配，不需要向量数据库。安全、规范、逻辑和汇总 Agent 使用各自职责词
与最多 32 个变更文件名查询，每路最多引用 8 条。引用以
`source#heading@version: excerpt` 进入 Prompt 和结构化事件；详情页可以据此显示来源。知识文本
与仓库规则一样是不可信输入，不能扩大权限、触发工具或绕过输出 Schema。

## 5. 可观察性与规模

每个 Agent 独立记录批次规划、请求开始/完成、批次成功/失败、Agent 完成/失败、耗时、Token、
请求 ID、安全错误和 RAG 引用。系统不保存或伪造模型私密 Chain-of-Thought。

计划元数据、规则和 Unit 使用三次有界查询读取，配置使用固定次数的批量查询，Finding 使用
批量 INSERT；这些查询次数均为 `O(1)`。模型调用和单批恢复事务为 `O(Agent 批次数)`：不同
上下文批次无法通过通用同步 OpenAI/Anthropic 协议合成一次请求，立即保存每批结果也是崩溃
恢复所需。每个 Agent 最多持久化 3000 个批次；规划器会在生成第 3001 批时明确失败，
不会先写入后在恢复时静默截断。
