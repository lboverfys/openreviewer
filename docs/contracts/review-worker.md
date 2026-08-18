# 审查任务 Worker 契约 v1

本文档固定 M2 单并发 Worker 的可靠性语义。当前只部署一个执行槽，但数据库领取协议允许
后续增加 Worker 时不重复领取同一条任务。

## 1. 领取

Worker 在一个事务内查询满足以下条件的任务：

- `execution_status = queued`；
- `available_at <= 当前时间`；
- 按优先级、可用时间和创建时间排序。

PostgreSQL 查询使用 `FOR UPDATE SKIP LOCKED`。领取成功时，在同一事务内：

1. 任务和运行状态都改为 `running`；
2. `attempt_count` 加一；
3. 写入 `lease_owner` 和 `lease_expires_at`；
4. 追加 `review.task.running` Outbox 事件。

## 2. 租约与恢复

租约表示某个 Worker 在有限时间内拥有任务。只有 Worker ID、任务 ID、运行 ID 和尝试次数
都与当前记录一致时，才能续租或更新状态；旧租约操作必须失败。

发现 `running` 任务的租约已过期时：

- 仍有尝试次数：清除租约，回到 `queued`，按指数退避设置下一次 `available_at`；
- 已达到 `max_attempts`：任务和运行都改为 `failed`；
- 两种情况都保存不含凭据的错误说明并追加 Outbox 事件。

默认退避从 5 秒开始，每次翻倍，最多 300 秒。默认最多尝试 3 次。

## 3. M2 状态边界

M2 尚未接入 GitHub PR、Actions CI 或模型。Worker 领取任务并完成当前本地准备步骤后，把任务
和运行改为 `waiting_for_ci`，清除租约并追加 `review.waiting_for_ci` 事件。

`waiting_for_ci` 是明确的未完成状态，不等于“审查成功”。在真实 CI、模型调用和结果发布
实现之前，Worker 不得写入 `completed`。

## 4. 心跳

`worker_heartbeats` 记录：

- 稳定 Worker ID；
- `starting`、`idle`、`busy` 或 `stopping` 状态；
- 当前任务 ID；
- 启动时间和最近心跳时间。

Dashboard 默认把 15 秒内有心跳的 Worker 视为在线。容器健康检查也读取同一心跳，连续
失联会把 Worker 标记为不健康。
