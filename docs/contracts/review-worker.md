# 审查任务 Worker 契约 v2

本文档固定“每个进程单并发”的 Worker 可靠性语义。部署可运行一个或多个副本；数据库
领取协议允许横向扩展时不重复领取同一条任务。

## 1. 领取

Worker 在一个事务内查询满足以下条件的任务：

- `execution_status = queued`、`waiting_for_ci`，或模型阶段尚未完成的 `ready_for_review`；
- `available_at <= 当前时间`；
- 按优先级、可用时间和创建时间排序。

PostgreSQL 查询使用 `FOR UPDATE SKIP LOCKED`。领取成功时，在同一事务内：

1. 任务和运行状态都改为 `running`；
2. 从 `queued` 或无计划的 `ready_for_review` 领取时 `attempt_count` 加一；已有计划的模型阶段只
   增加独立 `model_attempt_count`；从 `waiting_for_ci` 领取时只增加 `ci_poll_count`；
3. 写入 `lease_owner`、`lease_expires_at` 和 `claimed_from_status`；
4. 追加 `review.task.running` Outbox 事件。

## 2. 租约与恢复

租约表示某个 Worker 在有限时间内拥有任务。只有任务仍处于 `running`、租约尚未过期，且
Worker ID、任务 ID、运行 ID、失败尝试次数和 CI 轮询代次都与当前记录一致时，才能续租或
更新状态；旧租约或已过期租约的操作必须失败。CI 轮询代次独立于失败尝试次数，因此同一
Worker 的旧轮询租约也不能覆盖新一轮结果。

发现 `running` 任务的租约已过期时：

- 仍有尝试次数：清除租约，回到领取前的 `queued`、`waiting_for_ci` 或
  `ready_for_review`，按指数退避设置下一次 `available_at`；
- 已达到 `max_attempts`：任务和运行都改为 `failed`；
- 两种情况都保存结构化安全错误并追加 Outbox 事件。

默认退避从 5 秒开始，每次翻倍，最多 300 秒。默认最多尝试 3 次。

任务错误拆分为稳定错误码、安全说明、是否可重试和经过递归脱敏的详情。日志、数据库、
Dashboard/API 使用同一脱敏规则；未知异常也必须先转换为 `SafeError`，不能把可能包含 Token、
密码或带凭据 URL 的原始异常直接传给任务队列。不可重试错误会立即失败，可重试错误才按
剩余尝试次数退避。

过期租约恢复每批最多处理 100 条，并在一个 JOIN 查询中取回任务与运行；查询使用
`execution_status + lease_expires_at` 索引，循环内不执行数据库查询。

## 3. PR 与 CI 状态边界

Worker 已能使用短期 GitHub App installation token 获取 PR 上下文和与精确 `head_sha`
匹配的 CI。首次读取会保存变更文件和有界 diff；后续 CI 轮询只刷新 PR 身份和 CI，不重复
下载文件。正常轮询使用单独的 `ci_poll_count`，不消耗最多三次的错误重试次数。GitHub
读取期间由独立短生命周期线程刷新 `busy` 心跳，外部请求不持有数据库事务。

`waiting_for_ci` 是明确的未完成状态，不等于“审查成功”。CI 汇总为 `not_configured`（已完整
读取但没有可见检查）、`success` 或 `failure` 后，任务进入 `ready_for_review`；`unknown` 仍会
继续轮询，直到恢复完整或超时。Worker 随后一次批量读取当前 SHA 的文件快照、一次 GraphQL 读取规则，
生成确定性 Review Plan，并在短事务内原子保存规则、Unit、文件结果与
`review.plan.prepared` Outbox；随后安全、规范和逻辑三个 Agent 按各自模型的上下文窗口、单批
输入上限、输出预留和请求字节限制并行处理全部可审查 Unit，单文件仍超限时按行切片。三路成功
后先持久化 `aggregating`，再由汇总 Agent 处理有界结构化候选。固定顺序不能由模型改变。

每批规划、请求开始/完成、批次完成/失败和 Agent 结束都会用短事务保存结构化进度事件；模型
HTTP 请求发生在事务外。全部成功后，Worker 合并去重 Finding 和用量，再原子保存调用审计、
Token、耗时、成本、Finding 和 `review.model.completed`。旧 `execution_status` 写成 `completed`，
真实 `workflow_status` 停在 `awaiting_approval`。批准后必须再由管理员显式发布 GitHub PR 汇总
评论，成功才进入工作流 `completed`。保存前仍重新检查租约、计划指纹和 SHA。
旧 SHA、关闭/Draft PR 和 CI 超时分别进入 `superseded`、`cancelled` 和 `timed_out`。

## 4. 心跳

`worker_heartbeats` 记录：

- 稳定 Worker ID；
- `starting`、`idle`、`busy` 或 `stopping` 状态；
- 当前任务 ID；
- 该 `worker_id` 第一次写入心跳表的时间和最近心跳时间。

Compose 不再注入固定 Worker ID；未显式配置时使用容器主机名，因此不同副本拥有不同心跳
记录。相同副本重启时会更新状态和最近心跳时间，但不会重置首次写入时间；因此 `started_at`
表示“数据库第一次见到该 Worker ID 的时间”，不是最近一次进程启动时间。生产发布脚本通过
`OPENREVIEWER_WORKER_REPLICAS` 和 `docker compose --scale` 显式创建副本。

Dashboard 默认把 15 秒内有心跳的 Worker 视为在线。容器健康检查也读取同一心跳，其新鲜度
窗口是“15 秒”和“四个轮询周期”中的较大值；Compose 默认每 15 秒检查一次，连续 3 次失败
后会把 Worker 容器标记为不健康。
