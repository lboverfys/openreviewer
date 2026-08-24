# 审查任务创建接口 v1

本文档固定项目持续沿用的内部任务创建接口语义。该接口不是 GitHub Webhook，
也不直接执行审查；它只负责可靠地接受请求并创建待处理任务。

## 1. 请求

```http
POST /api/v1/reviews
Cookie: <已登录管理员会话>
Idempotency-Key: <1 到 200 个字符>
Content-Type: application/json
```

请求体字段：

| 字段 | 规则 |
| --- | --- |
| `installation_id` | 正整数，GitHub App 安装 ID |
| `repository_id` | 正整数，GitHub 稳定仓库 ID |
| `repository` | `owner/name` 形式，仅用于展示和审计 |
| `pull_request_number` | 正整数 |
| `head_sha` | 40 到 64 位完整十六进制 SHA，进入领域层后转为小写 |

请求体禁止额外字段。调用方必须为一次逻辑提交生成稳定的 `Idempotency-Key`。

## 2. 持久化语义

首次接受请求时，在同一个数据库事务内创建：

1. 一个状态为 `queued`、覆盖状态为 `unknown` 的 `ReviewRun`；
2. 一个与该运行一一对应、状态为 `queued` 的 `ReviewTask`；
3. 一个类型为 `review.requested` 的 `OutboxEvent`。

只有事务整体提交成功才返回接受结果。当前 Worker 可以把任务从 `queued` 推进到
`waiting_for_ci` 或 `ready_for_review`。模型审查尚未接入，因此不能进入 `completed`。

## 3. 幂等行为

- 相同幂等键和相同规范化请求：返回原 `review_run_id` 和 `review_task_id`，不新增记录；
- 相同幂等键但请求内容不同：返回 `409 Conflict`；
- 相同审查版本使用新的幂等键：视为明确重新审查，创建新的运行和任务。

幂等唯一约束由 PostgreSQL 强制执行，不能只依赖 API 进程内判断。

## 4. 返回

成功接受时统一返回 `202 Accepted`：

```json
{
  "review_run_id": "UUID",
  "review_task_id": "UUID",
  "review_version_key": "42:128:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "execution_status": "queued",
  "accepted_at": "2026-08-18T10:00:00Z",
  "created": true
}
```

`created=false` 表示这是同一幂等请求的重试，ID 仍然有效。

首次创建时 `execution_status` 为 `queued`。相同幂等请求稍后重试时，接口读取并返回数据库中
该运行的当前状态，因此 `created=false` 的响应也可能是 `running`、`waiting_for_ci` 或其他
已经推进到的状态；`accepted_at` 仍是原请求首次创建的时间。

## 5. 错误

| HTTP 状态 | 含义 |
| --- | --- |
| `401` | 没有有效管理员会话 |
| `422` | 缺少幂等键，或请求字段不符合契约 |
| `403` | 浏览器请求携带了与当前入口不一致的 `Origin` |
| `409` | 幂等键已经被不同内容使用 |
| `503` | 管理员认证未配置，或数据库未配置、暂时不可用，不能保证任务已经持久化 |

发生 `503` 时调用方可以使用同一个幂等键安全重试。

## 6. 暴露边界

该接口属于认证后的管理 API。API 容器仍只映射宿主机 `127.0.0.1:18090`；公网 Web
入口只通过同源 Nginx 代理允许的管理路径，并依赖 Secure、HttpOnly、SameSite=Strict
会话 Cookie。后续开放 GitHub Webhook 时必须使用独立验签入口。
