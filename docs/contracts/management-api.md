# 管理与实时接口 v1

M2 管理界面只提供单管理员登录。除 `/healthz` 外，下列接口都通过同源 HTTPS Web 入口
访问，API 不直接暴露公网端口。

## 1. 认证

| 方法和路径 | 用途 |
| --- | --- |
| `POST /api/v1/auth/login` | 校验管理员用户名和密码，成功后设置会话 Cookie |
| `POST /api/v1/auth/logout` | 删除当前浏览器会话 Cookie |
| `GET /api/v1/auth/me` | 返回当前管理员和会话到期时间 |

密码只使用 Argon2id 哈希校验。服务器配置不得保存明文密码。会话使用 HMAC-SHA256 签名，
生产 Cookie 必须同时设置 `Secure`、`HttpOnly`、`SameSite=Strict` 和 `Path=/`，前端不得
把密码或会话 Token 写入 localStorage。

API 进程按客户端和账号记录 15 分钟滑动失败窗口；Nginx 还对登录路径执行 IP 级限速。
错误账号和错误密码统一返回 `401`，不能通过响应判断账号是否存在。

## 2. Dashboard 和列表

| 方法和路径 | 用途 |
| --- | --- |
| `GET /api/v1/dashboard` | 状态计数、最近任务和最新 Worker 心跳 |
| `GET /api/v1/reviews?limit=50` | 最近审查任务列表，`limit` 范围为 1 到 100 |
| `POST /api/v1/reviews` | 创建幂等审查任务，详细规则见任务接口契约 |

响应不能包含数据库密码、会话密钥、密码哈希或任务幂等键。

## 3. 实时事件

`GET /api/v1/reviews/stream` 使用 Server-Sent Events（SSE，即服务器保持一条单向长连接）
推送 `dashboard` 事件。每个事件包含与 Dashboard 接口相同的完整快照，前端断线后由浏览器
自动重连，并明确显示“正在重连”。

Nginx 必须关闭该路径的代理缓冲和缓存，API 返回 `X-Accel-Buffering: no`。会话过期后连接
会被拒绝，不能退化成未认证数据流。

## 4. 公网边界

Nginx 只代理本文列出的管理路径，其他 `/api/` 路径返回 `404`。PostgreSQL 和 Worker 没有
宿主机端口；API 仅绑定 `127.0.0.1:18090`，测试 Web 入口使用自签名 HTTPS 的独立端口。
