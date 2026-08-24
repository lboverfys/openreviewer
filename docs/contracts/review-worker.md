# 审查任务 Worker 契约 v2

本文档固定单并发 Worker 的可靠性语义。当前只部署一个执行槽，但数据库领取协议允许
后续增加 Worker 时不重复领取同一条任务。

## 1. 领取

Worker 在一个事务内查询满足以下条件的任务：

- `execution_status = queued` 或 `waiting_for_ci`；
- `available_at <= 当前时间`；
- 按优先级、可用时间和创建时间排序。

PostgreSQL 查询使用 `FOR UPDATE SKIP LOCKED`。领取成功时，在同一事务内：

1. 任务和运行状态都改为 `running`；
2. 从 `queued` 领取时 `attempt_count` 加一，从 `waiting_for_ci` 领取时只增加
   `ci_poll_count`；
3. 写入 `lease_owner` 和 `lease_expires_at`；
4. 追加 `review.task.running` Outbox 事件。

## 2. 租约与恢复

租约表示某个 Worker 在有限时间内拥有任务。只有任务仍处于 `running`、租约尚未过期，且
Worker ID、任务 ID、运行 ID、失败尝试次数和 CI 轮询代次都与当前记录一致时，才能续租或
更新状态；旧租约或已过期租约的操作必须失败。CI 轮询代次独立于失败尝试次数，因此同一
Worker 的旧轮询租约也不能覆盖新一轮结果。

发现 `running` 任务的租约已过期时：

- 仍有尝试次数：清除租约，回到 `queued`，按指数退避设置下一次 `available_at`；
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

`waiting_for_ci` 是明确的未完成状态，不等于“审查成功”。CI 进入任意终态后，任务进入
`ready_for_review`；这个状态只表示后续模型审查可以开始。模型调用和结果发布尚未实现，
因此 Worker 仍不得写入 `completed`。旧 SHA、关闭/Draft PR 和 CI 超时分别进入
`superseded`、`cancelled` 和 `timed_out`。

## 4. 心跳

`worker_heartbeats` 记录：

- 稳定 Worker ID；
- `starting`、`idle`、`busy` 或 `stopping` 状态；
- 当前任务 ID；
- 该 `worker_id` 第一次写入心跳表的时间和最近心跳时间。

Compose 当前使用固定 Worker ID。相同 ID 的容器重启时会更新状态和最近心跳时间，但不会重置
首次写入时间；因此 `started_at` 当前表示“数据库第一次见到该 Worker ID 的时间”，不是最近
一次 Worker 进程的启动时间。

Dashboard 默认把 15 秒内有心跳的 Worker 视为在线。容器健康检查也读取同一心跳，其新鲜度
窗口是“15 秒”和“四个轮询周期”中的较大值；Compose 默认每 15 秒检查一次，连续 3 次失败
后会把 Worker 容器标记为不健康。
