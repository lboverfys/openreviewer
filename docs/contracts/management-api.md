# 管理与实时接口 v1

当前管理界面只提供单管理员登录。除 `/healthz` 外，下列接口都通过同源 HTTPS Web 入口
访问，API 不直接暴露公网端口。

## 1. 认证

| 方法和路径 | 用途 |
| --- | --- |
| `POST /api/v1/auth/login` | 校验管理员用户名和密码，成功后设置会话 Cookie |
| `POST /api/v1/auth/logout` | 删除当前浏览器会话 Cookie |
| `GET /api/v1/auth/me` | 返回当前管理员和会话到期时间 |

密码只使用 Argon2id 哈希校验。服务器配置不得保存明文密码。会话使用 HMAC-SHA256 签名，
生产 Cookie 必须同时设置 `Secure`、`HttpOnly`、`SameSite=Strict` 和 `Path=/`，前端不得
把密码或会话 Token 写入 `localStorage`。

登录页提供“记住账号密码”选项。勾选后，前端通过浏览器 Credential Management API
（`PasswordCredential`）请求浏览器密码管理器保存并在下次打开登录页时自动填充凭据；
密码由浏览器自己的密码库负责加密和访问控制，OpenReviewer 不建立明文密码副本。浏览器、
无痕模式或安全来源策略不支持该 API 时，登录仍然可用，但需要用户手动输入，不能由应用
自行降级为 `localStorage` 保存。注销只删除 OpenReviewer 会话 Cookie，不删除浏览器密码库
中的凭据；要删除已保存密码，应使用浏览器的密码管理设置。

API 进程按客户端和账号记录 15 分钟滑动失败窗口；Nginx 还对登录路径执行 IP 级限速。
错误账号和错误密码统一返回 `401`，不能通过响应判断账号是否存在。

## 2. Dashboard 和列表

| 方法和路径 | 用途 |
| --- | --- |
| `GET /api/v1/dashboard` | 状态计数、最近任务和最新 Worker 心跳 |
| `GET /api/v1/reviews?limit=50` | 最近审查任务列表，`limit` 范围为 1 到 100 |
| `POST /api/v1/reviews` | 创建幂等审查任务，详细规则见任务接口契约 |
| `GET /api/v1/reviews/{review_run_id}` | 详情、双状态、四 Agent 进度、Finding、CI 和事件 |
| `POST /api/v1/reviews/{review_run_id}/identity/sync` | 从 GitHub 补全历史任务的 PR 作者、链接和来源/目标分支 |
| `POST /api/v1/reviews/{review_run_id}/actions` | 暂停、恢复、重试、批准、驳回或人工发布 |
| `POST /api/v1/reviews/{review_run_id}/findings/{finding_id}` | 确认或忽略单条 Finding |

响应模型不会直接暴露数据库密码、会话密钥、密码哈希或任务幂等键。任务列表和 Dashboard
返回 `last_error`、`last_error_code`、`last_error_retryable` 和安全详情；错误在 Worker 入库前
统一脱敏，读取时再次执行防御性脱敏。原始异常、Token、密码和带凭据 URL 不属于响应契约。

身份同步、动作和 Finding 写接口都要求 `Idempotency-Key` 与同源校验。身份同步只回填作者、
GitHub 链接、来源仓库/分支和目标仓库/分支，不使用 GitHub 当前标题、SHA 或文件数覆盖历史
任务的不可变版本快照；成功或已完成的重复请求返回最新详情。GitHub App 未配置时返回 `503`，
鉴权、权限、限流和上游故障使用脱敏的稳定错误契约。

动作响应同时返回旧队列
`execution_status` 和真实 `workflow_status`；服务端在提交动作后重新读取已提交详情，不用旧字段
猜测人工节点。模型结束后固定经过
`awaiting_approval -> approved -> awaiting_publish -> publishing`；`approved` 与自动开放发布门
分别写审计事件，批准和发布仍是两次独立点击。`retry_stage` 必须提供 CI、规划、Agent 批次或
汇总目标；同一幂等键不得改用不同目标。发布失败返回 `503`，数据库工作流恢复为
`awaiting_publish`，同一键可重试。

## 3. 四 Agent 设置

| 方法和路径 | 用途 |
| --- | --- |
| `GET /api/v1/settings/ai/agents` | 读取四个 Agent 的脱敏配置 |
| `PUT /api/v1/settings/ai/agents/{agent}` | 保存一个 Agent 参数和可选新 API Key |
| `POST /api/v1/settings/ai/agents/{agent}/test` | 在事务外执行真实结构化连接测试 |
| `POST /api/v1/settings/ai/agents/{agent}/enabled` | 启用或停用已测试配置 |

响应只提供是否配置密钥和末四位掩码，不返回密文或明文。所有写入携带
`expected_revision`；revision 冲突返回 `409`。详细规则见 [ai-settings.md](ai-settings.md)。

## 4. 实时事件

`GET /api/v1/reviews/stream` 使用 Server-Sent Events（SSE，即服务器保持一条单向长连接）
推送 `dashboard` 事件。每个事件包含与 Dashboard 接口相同的完整快照，前端断线后由浏览器
自动重连，并明确显示“正在重连”。

读取 Dashboard 暂时失败时，当前 API 会发送 `unavailable` 事件而不是伪造正常快照；连接
仍会继续，前端收到该事件后显示重连状态，等待后续 `dashboard` 事件恢复页面数据。

Nginx 必须关闭该路径的代理缓冲和缓存，API 返回 `X-Accel-Buffering: no`。当前 API 只在
建立 SSE 连接时验证一次会话；已经建立的连接不会在会话到期时由服务端主动断开。连接断开
后，浏览器重新连接时会再次校验会话，没有有效会话就不能建立新的数据流。后续如果要求会话
到期时立即停止推送，需要在事件循环中重新校验会话，或者为 SSE 设置不超过会话剩余时间的
最大连接时长。

## 5. 公网边界

Nginx 只代理本文列出的管理路径，其他 `/api/` 路径返回 `404`。PostgreSQL 和 Worker 没有
宿主机端口；API 在容器内监听 `0.0.0.0:18090`，Compose 只把它映射到宿主机回环地址
`127.0.0.1:18090`，因此不会直接暴露到公网。Web 入口通过 Cloudflare 域名访问。
